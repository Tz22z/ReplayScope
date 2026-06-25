from __future__ import annotations

import importlib.metadata
import platform
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from replayscope.cas import LocalCAS
from replayscope.models import EventKind, Trace, TraceCreate, TraceStatus
from replayscope.repository import TraceRepository
from replayscope.workspace import capture_workspace, diff_manifests, store_manifest

SENSITIVE_KEYS = re.compile(r"(api.?key|authorization|password|secret|token|cookie)", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def failure_signature(error: BaseException) -> str:
    message = re.sub(r"0x[0-9a-f]+", "0x…", str(error), flags=re.IGNORECASE)
    message = re.sub(r"\b\d{4,}\b", "#", message)
    return f"{type(error).__module__}.{type(error).__qualname__}:{message}"[:500]


class Recorder:
    def __init__(self, repository: TraceRepository, cas: LocalCAS) -> None:
        self.repository = repository
        self.cas = cas

    def start(
        self,
        source_run_id: str,
        *,
        framework: str = "generic",
        labels: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> Trace:
        trace = self.repository.create_trace(
            TraceCreate(source_run_id=source_run_id, framework=framework, labels=labels or {})
        )
        environment = self.capture_environment(workspace)
        environment_ref = self._store(environment)
        self.repository.set_workspace_refs(trace.id, environment=environment_ref)
        self.repository.append_event(
            trace.id,
            EventKind.ENVIRONMENT,
            "runtime",
            output_ref=environment_ref,
        )
        if workspace:
            manifest_ref = store_manifest(capture_workspace(workspace, self.cas), self.cas)
            self._register(manifest_ref)
            self.repository.set_workspace_refs(trace.id, initial=manifest_ref)
            self.repository.append_event(
                trace.id, EventKind.CHECKPOINT, "initial", output_ref=manifest_ref
            )
        return trace

    def record_model_call(
        self,
        trace_id,
        name: str,
        request: dict,
        call: Callable[[], dict],
        *,
        model: str,
        cost_usd: float | None = None,
    ) -> dict:
        return self._record_call(
            trace_id,
            EventKind.MODEL_CALL,
            name,
            request,
            call,
            metadata={"model": model},
            cost_usd=cost_usd,
        )

    def record_tool_call(
        self,
        trace_id,
        name: str,
        arguments: dict,
        call: Callable[[], dict],
        *,
        tool_version: str | None = None,
    ) -> dict:
        metadata = {"tool_version": tool_version} if tool_version else {}
        return self._record_call(
            trace_id, EventKind.TOOL_CALL, name, arguments, call, metadata=metadata
        )

    def capture_delta(self, trace_id, workspace: Path, previous_manifest) -> Any:
        current = capture_workspace(workspace, self.cas)
        delta = diff_manifests(previous_manifest, current)
        input_ref = self._store(previous_manifest.model_dump(mode="json"))
        output_ref = self._store(delta.model_dump(mode="json"))
        self.repository.append_event(
            trace_id,
            EventKind.WORKSPACE_DELTA,
            "workspace",
            input_ref=input_ref,
            output_ref=output_ref,
        )
        return current

    def finish(
        self,
        trace_id,
        *,
        status: TraceStatus,
        workspace: Path | None = None,
        error: BaseException | None = None,
    ) -> Trace:
        metadata = {"status": status.value}
        error_ref = self._store_error(error) if error else None
        final_ref = None
        if workspace:
            final_ref = store_manifest(capture_workspace(workspace, self.cas), self.cas)
            self._register(final_ref)
            self.repository.set_workspace_refs(trace_id, final=final_ref)
        self.repository.append_event(
            trace_id,
            EventKind.RUN_FINISHED,
            status.value,
            output_ref=final_ref,
            error_ref=error_ref,
            metadata=metadata,
        )
        signature = failure_signature(error) if error else None
        return self.repository.finish_trace(trace_id, status, signature)

    def _record_call(
        self,
        trace_id,
        kind: EventKind,
        name: str,
        request: dict,
        call: Callable[[], dict],
        *,
        metadata: dict,
        cost_usd: float | None = None,
    ) -> dict:
        input_ref = self._store(redact(request))
        started = time.perf_counter()
        try:
            result = call()
        except Exception as error:
            elapsed = (time.perf_counter() - started) * 1000
            error_ref = self._store_error(error)
            self.repository.append_event(
                trace_id,
                kind,
                name,
                input_ref=input_ref,
                error_ref=error_ref,
                metadata=metadata,
                duration_ms=elapsed,
                cost_usd=cost_usd,
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        output_ref = self._store(redact(result))
        self.repository.append_event(
            trace_id,
            kind,
            name,
            input_ref=input_ref,
            output_ref=output_ref,
            metadata=metadata,
            duration_ms=elapsed,
            cost_usd=cost_usd,
        )
        return result

    def _store(self, value: Any):
        reference = self.cas.put_json(value)
        self._register(reference)
        return reference

    def _register(self, reference) -> None:
        self.repository.register_blob(reference)

    def _store_error(self, error: BaseException):
        return self._store(
            {
                "type": f"{type(error).__module__}.{type(error).__qualname__}",
                "message": str(error),
                "signature": failure_signature(error),
                "traceback": traceback.format_exception(error),
            }
        )

    @staticmethod
    def capture_environment(workspace: Path | None = None) -> dict:
        packages = sorted(
            {
                distribution.metadata["Name"]: distribution.version
                for distribution in importlib.metadata.distributions()
            }.items()
        )
        environment = {
            "python": sys.version,
            "platform": platform.platform(),
            "packages": dict(packages),
        }
        if workspace and (workspace / ".git").exists():
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            environment["git_commit"] = (
                completed.stdout.strip() if completed.returncode == 0 else None
            )
        return environment
