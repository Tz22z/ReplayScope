from replayscope.compare import ComparisonPolicy, compare_values
from replayscope.models import DivergenceKind


def test_structured_diff_reports_precise_paths() -> None:
    divergences = compare_values(
        {"answer": {"value": 4, "unit": "s"}},
        {"answer": {"value": 5}, "extra": True},
        sequence=3,
    )
    assert {(item.kind, item.path) for item in divergences} == {
        (DivergenceKind.VALUE, "$.answer.value"),
        (DivergenceKind.MISSING, "$.answer.unit"),
        (DivergenceKind.UNEXPECTED, "$.extra"),
    }


def test_comparison_policy_ignores_volatile_fields_and_tolerates_numbers() -> None:
    policy = ComparisonPolicy(ignored_paths=frozenset({"$.request_id"}), numeric_tolerance=0.1)
    assert not compare_values(
        {"score": 0.95, "request_id": "old"},
        {"score": 1.0, "request_id": "new"},
        sequence=0,
        policy=policy,
    )
