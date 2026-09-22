from pathlib import Path

from replayscope.cas import LocalCAS
from scripts.validate_claims import reduction_experiment, replay_experiment


def test_replay_experiment_reports_ground_truth_confusion_matrix(tmp_path: Path) -> None:
    result = replay_experiment(LocalCAS(tmp_path / "cas"), count=32)

    assert result["injected_drifts"] == 2
    assert result["confusion_matrix"] == {
        "true_positive": 2,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 30,
    }
    assert len(result["cases"]) == 32


def test_reduction_experiment_distinguishes_stable_and_absent_faults(tmp_path: Path) -> None:
    result = reduction_experiment(LocalCAS(tmp_path / "cas"), count=6, stable_faults=4)

    assert result["reproduced"] == 4
    assert result["false_positives"] == 0
    assert result["false_negatives"] == 0
    assert {row["minimal_size"] for row in result["cases"][:4]} == {3}
