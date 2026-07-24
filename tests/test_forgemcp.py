from pathlib import Path

from replayscope.cas import LocalCAS
from replayscope.integrations.forgemcp import RecordedForgeRuntime
from replayscope.models import EventKind
from replayscope.recorder import Recorder
from replayscope.repository import MemoryTraceRepository


class FakeForge:
    def model_call(self, request):
        return {"tool": "search", "query": request["task"]}

    def tool_call(self, name, arguments):
        return {"name": name, "hits": [arguments["q"]]}


def test_forge_adapter_records_both_boundaries(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    recorder = Recorder(repository, LocalCAS(tmp_path / "cas"))
    trace = recorder.start("forge-1", framework="forgemcp")
    forge = RecordedForgeRuntime(FakeForge(), recorder, trace.id, "model-a", {"search": "2"})
    assert forge.model_call({"task": "leases"})["tool"] == "search"
    assert forge.tool_call("search", {"q": "leases"})["hits"] == ["leases"]
    assert [event.kind for event in repository.events(trace.id)][-2:] == [
        EventKind.MODEL_CALL,
        EventKind.TOOL_CALL,
    ]
