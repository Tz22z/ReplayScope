from pathlib import Path

import pytest

from replayscope.cas import ContentCorruption, LocalCAS
from replayscope.models import ReplayMode, TraceStatus
from replayscope.recorder import Recorder
from replayscope.replay import ReplayEngine
from replayscope.repository import MemoryTraceRepository
from replayscope.results import MemoryReplayResultStore
from replayscope.workspace import capture_workspace


def recorded_trace(tmp_path: Path):
    cas = LocalCAS(tmp_path / "cas")
    repository = MemoryTraceRepository()
    recorder = Recorder(repository, cas)
    trace = recorder.start("agent-1")
    recorder.record_model_call(
        trace.id, "planner", {"prompt": "go"}, lambda: {"action": "search"}, model="old"
    )
    recorder.record_tool_call(trace.id, "search", {"query": "go"}, lambda: {"documents": [1, 2]})
    recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    return trace.id, cas, repository


def test_recorded_mode_makes_no_external_calls(tmp_path: Path) -> None:
    trace_id, cas, repository = recorded_trace(tmp_path)

    def forbidden(*_args):
        raise AssertionError("adapter should not run")

    result = ReplayEngine(
        repository, cas, model_adapters={"planner": forbidden}, tool_adapters={"search": forbidden}
    ).replay(trace_id, ReplayMode.RECORDED)
    assert result.status == "matched"


def test_fresh_model_reports_first_value_divergence(tmp_path: Path) -> None:
    trace_id, cas, repository = recorded_trace(tmp_path)
    store = MemoryReplayResultStore()
    engine = ReplayEngine(
        repository,
        cas,
        model_adapters={"planner": lambda _request, _context: {"action": "finish"}},
        result_store=store,
    )
    result = engine.replay(trace_id, ReplayMode.FRESH_MODEL)
    assert result.status == "diverged"
    assert result.divergences[0].path == "$.action"
    assert store.get(result.id) == result


def test_fresh_all_reexecutes_tools(tmp_path: Path) -> None:
    trace_id, cas, repository = recorded_trace(tmp_path)
    calls = []
    engine = ReplayEngine(
        repository,
        cas,
        model_adapters={"planner": lambda _request, _context: {"action": "search"}},
        tool_adapters={
            "search": lambda request, _context: calls.append(request) or {"documents": [1, 2]}
        },
    )
    result = engine.replay(trace_id, ReplayMode.FRESH_ALL)
    assert result.status == "matched"
    assert calls == [{"query": "go"}]


def test_fresh_all_reports_workspace_drift(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    repository = MemoryTraceRepository()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "state.txt").write_text("before")
    recorder = Recorder(repository, cas)
    trace = recorder.start("workspace", workspace=workspace)
    before = capture_workspace(workspace, cas)

    def original_tool():
        (workspace / "state.txt").write_text("expected")
        return {"ok": True}

    recorder.record_tool_call(trace.id, "writer", {}, original_tool)
    recorder.capture_delta(trace.id, workspace, before)
    recorder.finish(trace.id, status=TraceStatus.SUCCEEDED, workspace=workspace)

    def drifted_tool(_request, context):
        (context.workspace / "state.txt").write_text("actual")
        return {"ok": True}

    result = ReplayEngine(repository, cas, tool_adapters={"writer": drifted_tool}).replay(
        trace.id, ReplayMode.FRESH_ALL
    )
    assert any(item.kind.value == "workspace" for item in result.divergences)


def test_replay_rejects_corrupted_recorded_payload(tmp_path: Path) -> None:
    trace_id, cas, repository = recorded_trace(tmp_path)
    reference = repository.events(trace_id)[1].output_ref
    assert reference
    cas._path(reference.digest).write_bytes(b"corrupt")
    with pytest.raises(ContentCorruption):
        ReplayEngine(repository, cas).replay(trace_id)
