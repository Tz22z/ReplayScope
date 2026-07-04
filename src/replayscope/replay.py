from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from replayscope.cas import LocalCAS
from replayscope.compare import ComparisonPolicy, compare_values
from replayscope.integrity import verify_event_chain
from replayscope.models import (
    Divergence,
    DivergenceKind,
    EventKind,
    ReplayMode,
    ReplayResult,
    TraceEvent,
    WorkspaceDelta,
    WorkspaceManifest,
)
from replayscope.recorder import failure_signature
from replayscope.repository import TraceRepository
from replayscope.workspace import apply_delta, capture_workspace, restore_workspace


@dataclass(frozen=True)
class ReplayContext:
    trace_id: UUID
    event: TraceEvent
    workspace: Path


BoundaryAdapter = Callable[[dict[str, Any], ReplayContext], dict[str, Any]]


class ReplayEngine:
    def __init__(
        self,
        repository: TraceRepository,
        cas: LocalCAS,
        *,
        model_adapters: Mapping[str, BoundaryAdapter] | None = None,
        tool_adapters: Mapping[str, BoundaryAdapter] | None = None,
        policy: ComparisonPolicy | None = None,
        result_store=None,
    ) -> None:
        self.repository = repository
        self.cas = cas
        self.model_adapters = model_adapters or {}
        self.tool_adapters = tool_adapters or {}
        self.policy = policy or ComparisonPolicy()
        self.result_store = result_store

    def replay(self, trace_id: UUID, mode: ReplayMode = ReplayMode.RECORDED) -> ReplayResult:
        trace = self.repository.get_trace(trace_id)
        events = self.repository.events(trace_id)
        verify_event_chain(trace, events)
        self._verify_event_blobs(events)
        started = datetime.now(UTC)
        divergences: list[Divergence] = []
        with tempfile.TemporaryDirectory(prefix="replayscope-") as directory:
            workspace = Path(directory) / "actual"
            expected_workspace = Path(directory) / "expected"
            workspace.mkdir()
            expected_workspace.mkdir()
            for event in events:
                context = ReplayContext(trace_id, event, workspace)
                if event.kind is EventKind.CHECKPOINT and event.name == "initial":
                    manifest = WorkspaceManifest.model_validate(self.cas.get_json(event.output_ref))
                    restore_workspace(manifest, workspace, self.cas)
                    restore_workspace(manifest, expected_workspace, self.cas)
                elif event.kind is EventKind.WORKSPACE_DELTA:
                    delta = WorkspaceDelta.model_validate(self.cas.get_json(event.output_ref))
                    apply_delta(delta, expected_workspace, self.cas)
                    if mode is ReplayMode.FRESH_ALL:
                        expected = capture_workspace(expected_workspace, self.cas)
                        actual = capture_workspace(workspace, self.cas)
                        if expected != actual:
                            divergences.append(
                                Divergence(
                                    event_sequence=event.sequence,
                                    kind=DivergenceKind.WORKSPACE,
                                    path="$workspace",
                                    expected=expected.model_dump(mode="json"),
                                    actual=actual.model_dump(mode="json"),
                                    message="workspace delta differs",
                                )
                            )
                    else:
                        apply_delta(delta, workspace, self.cas)
                elif event.kind is EventKind.MODEL_CALL and mode in {
                    ReplayMode.FRESH_MODEL,
                    ReplayMode.FRESH_ALL,
                }:
                    divergences.extend(self._execute_boundary(event, context, self.model_adapters))
                elif event.kind is EventKind.TOOL_CALL and mode is ReplayMode.FRESH_ALL:
                    divergences.extend(self._execute_boundary(event, context, self.tool_adapters))
                elif event.kind is EventKind.RUN_FINISHED and event.output_ref:
                    expected = WorkspaceManifest.model_validate(self.cas.get_json(event.output_ref))
                    actual = capture_workspace(workspace, self.cas)
                    if expected != actual:
                        divergences.append(
                            Divergence(
                                event_sequence=event.sequence,
                                kind=DivergenceKind.WORKSPACE,
                                path="$workspace",
                                expected=expected.model_dump(mode="json"),
                                actual=actual.model_dump(mode="json"),
                                message="final workspace differs",
                            )
                        )
        result = ReplayResult(
            trace_id=trace_id,
            mode=mode,
            status="matched" if not divergences else "diverged",
            divergence_count=len(divergences),
            divergences=divergences,
            started_at=started,
            finished_at=datetime.now(UTC),
        )
        if self.result_store:
            self.result_store.save(result)
        return result

    def _verify_event_blobs(self, events: list[TraceEvent]) -> None:
        for event in events:
            for reference in (event.input_ref, event.output_ref, event.error_ref):
                if reference:
                    self.cas.get_bytes(reference)

    def _execute_boundary(
        self,
        event: TraceEvent,
        context: ReplayContext,
        adapters: Mapping[str, BoundaryAdapter],
    ) -> list[Divergence]:
        adapter = adapters.get(event.name)
        if adapter is None:
            return [
                Divergence(
                    event_sequence=event.sequence,
                    kind=DivergenceKind.ERROR,
                    path="$adapter",
                    expected=event.name,
                    actual=None,
                    message="fresh replay adapter is not registered",
                )
            ]
        request = self.cas.get_json(event.input_ref) if event.input_ref else {}
        try:
            actual = adapter(request, context)
        except Exception as error:  # noqa: BLE001 - adapter errors are replay results
            actual_signature = failure_signature(error)
            expected = self.cas.get_json(event.error_ref) if event.error_ref else None
            expected_signature = expected.get("signature") if expected else None
            if expected_signature == actual_signature:
                return []
            return [
                Divergence(
                    event_sequence=event.sequence,
                    kind=DivergenceKind.ERROR,
                    path="$error",
                    expected=expected_signature,
                    actual=actual_signature,
                    message="error signature differs",
                )
            ]
        if event.error_ref:
            expected_error = self.cas.get_json(event.error_ref)
            return [
                Divergence(
                    event_sequence=event.sequence,
                    kind=DivergenceKind.ERROR,
                    path="$error",
                    expected=expected_error.get("signature"),
                    actual=None,
                    message="recorded error did not recur",
                )
            ]
        expected = self.cas.get_json(event.output_ref) if event.output_ref else None
        return compare_values(
            expected,
            actual,
            sequence=event.sequence,
            policy=self.policy,
        )
