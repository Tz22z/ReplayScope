from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from replayscope.recorder import Recorder


class ForgeRuntime(Protocol):
    def model_call(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class RecordedForgeRuntime:
    """Thin ForgeMCP boundary adapter; framework internals remain untouched."""

    runtime: ForgeRuntime
    recorder: Recorder
    trace_id: Any
    model_name: str
    tool_versions: dict[str, str]

    def model_call(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.recorder.record_model_call(
            self.trace_id,
            "forgemcp.model",
            request,
            lambda: self.runtime.model_call(request),
            model=self.model_name,
        )

    def tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.recorder.record_tool_call(
            self.trace_id,
            name,
            arguments,
            lambda: self.runtime.tool_call(name, arguments),
            tool_version=self.tool_versions.get(name),
        )
