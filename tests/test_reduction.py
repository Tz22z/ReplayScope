from pathlib import Path

from replayscope.cas import LocalCAS
from replayscope.models import FileEntry
from replayscope.reduction import FailureReducer, ReductionPackage


def file_entry(cas: LocalCAS, path: str, content: str) -> FileEntry:
    return FileEntry(path=path, kind="file", content=cas.put_bytes(content.encode()), mode=0o644)


def test_ddmin_reduces_inputs_returns_and_files(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    package = ReductionPackage(
        inputs={f"input-{index}": index for index in range(4)},
        tool_returns={f"tool-{index}": {"value": index} for index in range(4)},
        files=[file_entry(cas, f"file-{index}.txt", str(index)) for index in range(4)],
    )

    def oracle(candidate: ReductionPackage, workspace: Path) -> str | None:
        triggered = (
            "input-2" in candidate.inputs
            and "tool-1" in candidate.tool_returns
            and (workspace / "file-3.txt").exists()
        )
        return "ValueError:boom" if triggered else None

    result = FailureReducer(cas, oracle, "ValueError:boom", stability_runs=1).reduce(package)
    assert result.status == "succeeded"
    assert result.original_size == 12
    assert result.minimal_size == 3
    assert result.size_reduction == 0.75


def test_different_failure_signature_is_not_a_reproduction(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    package = ReductionPackage(inputs={"x": 1})
    result = FailureReducer(cas, lambda *_: "TypeError:other", "ValueError:boom").reduce(package)
    assert result.status == "inconclusive"


def test_flaky_baseline_is_inconclusive(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    outcomes = iter(["boom", None])
    result = FailureReducer(cas, lambda *_: next(outcomes), "boom", stability_runs=2).reduce(
        ReductionPackage(inputs={"x": 1})
    )
    assert result.status == "inconclusive"
