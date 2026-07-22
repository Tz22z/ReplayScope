import tarfile
from pathlib import Path

from replayscope.bundle import export_reduction, export_trace, inspect_bundle
from replayscope.cas import LocalCAS
from replayscope.integrity import verify_event_chain
from replayscope.models import FileEntry, TraceStatus
from replayscope.recorder import Recorder
from replayscope.reduction import FailureReducer, ReductionPackage
from replayscope.repository import MemoryTraceRepository


def test_trace_bundle_contains_verified_blobs_and_event_chain(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    cas = LocalCAS(tmp_path / "cas")
    recorder = Recorder(repository, cas)
    trace = recorder.start("bundle-run")
    recorder.record_tool_call(trace.id, "echo", {"value": 1}, lambda: {"value": 1})
    finished = recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    destination = tmp_path / "trace.tar.gz"
    export_trace(trace.id, repository, cas, destination)
    bundled_trace, bundled_events, blobs = inspect_bundle(destination)
    assert bundled_trace == finished
    verify_event_chain(bundled_trace, bundled_events)
    assert blobs


def test_workspace_bundle_includes_nested_file_content(tmp_path: Path) -> None:
    repository = MemoryTraceRepository()
    cas = LocalCAS(tmp_path / "cas")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested.txt").write_text("nested content")
    recorder = Recorder(repository, cas)
    trace = recorder.start("workspace-bundle", workspace=workspace)
    recorder.finish(trace.id, status=TraceStatus.SUCCEEDED, workspace=workspace)
    destination = tmp_path / "workspace.tar.gz"
    export_trace(trace.id, repository, cas, destination)
    _trace, _events, blobs = inspect_bundle(destination)
    assert b"nested content" in blobs.values()


def test_reduction_export_contains_minimal_files(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    required = FileEntry(
        path="trigger.txt", kind="file", content=cas.put_bytes(b"trigger"), mode=0o644
    )
    noise = FileEntry(path="noise.txt", kind="file", content=cas.put_bytes(b"noise"), mode=0o644)
    package = ReductionPackage(files=[required, noise])
    result = FailureReducer(
        cas,
        lambda _candidate, workspace: "boom" if (workspace / "trigger.txt").exists() else None,
        "boom",
        stability_runs=1,
    ).reduce(package)
    destination = tmp_path / "minimal.tar.gz"
    export_reduction(result, cas, destination)
    with tarfile.open(destination, "r:gz") as archive:
        assert "reproduction.json" in archive.getnames()
        assert len([name for name in archive.getnames() if name.startswith("blobs/")]) == 1
