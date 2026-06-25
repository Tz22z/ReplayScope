from pathlib import Path

import pytest

from replayscope.cas import LocalCAS
from replayscope.integrity import verify_event_chain
from replayscope.models import EventKind, TraceStatus
from replayscope.recorder import Recorder, failure_signature, redact
from replayscope.repository import MemoryTraceRepository, TraceClosed, TraceIntegrityError


def test_recorder_captures_boundaries_and_chain(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    cas = LocalCAS(tmp_path / "cas")
    recorder = Recorder(repository, cas)
    trace = recorder.start("forge-run-1", framework="forgemcp")
    result = recorder.record_model_call(
        trace.id, "plan", {"prompt": "hi"}, lambda: {"text": "call tool"}, model="test"
    )
    assert result == {"text": "call tool"}
    recorder.record_tool_call(trace.id, "search", {"q": "x"}, lambda: {"hits": 2})
    finished = recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    events = repository.events(trace.id)
    assert [event.kind for event in events] == [
        EventKind.ENVIRONMENT,
        EventKind.MODEL_CALL,
        EventKind.TOOL_CALL,
        EventKind.RUN_FINISHED,
    ]
    verify_event_chain(finished, events)
    with pytest.raises(TraceClosed):
        repository.append_event(trace.id, EventKind.TOOL_CALL, "late")


def test_secrets_are_redacted_recursively() -> None:
    value = {"api_key": "secret", "nested": [{"Authorization": "Bearer x", "safe": 1}]}
    assert redact(value) == {
        "api_key": "[REDACTED]",
        "nested": [{"Authorization": "[REDACTED]", "safe": 1}],
    }


def test_error_is_recorded_and_reraised(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    recorder = Recorder(repository, LocalCAS(tmp_path / "cas"))
    trace = recorder.start("failed")

    def fail():
        raise ValueError("invalid item 123456")

    with pytest.raises(ValueError):
        recorder.record_tool_call(trace.id, "broken", {}, fail)
    event = repository.events(trace.id)[-1]
    assert event.error_ref is not None
    assert failure_signature(ValueError("invalid item 999999")) == failure_signature(
        ValueError("invalid item 123456")
    )


def test_hash_chain_detects_event_tampering(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    recorder = Recorder(repository, LocalCAS(tmp_path / "cas"))
    trace = recorder.start("tamper")
    finished = recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    events = repository.events(trace.id)
    events[0].name = "changed"
    with pytest.raises(TraceIntegrityError, match="invalid hash"):
        verify_event_chain(finished, events)


def test_trace_cannot_be_finished_twice(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    recorder = Recorder(repository, LocalCAS(tmp_path / "cas"))
    trace = recorder.start("closed")
    recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    with pytest.raises(TraceClosed):
        repository.finish_trace(trace.id, TraceStatus.FAILED)
