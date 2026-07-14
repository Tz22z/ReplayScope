from uuid import uuid4

import pytest

from replayscope.evaluation import (
    EvaluationCase,
    EvaluationVariant,
    MemoryEvaluationStore,
    Observation,
    RegressionEvaluator,
    ReplayCaseRunner,
    compare_reports,
)


def test_evaluator_aggregates_five_repetitions() -> None:
    cases = [EvaluationCase(trace_id=uuid4(), name=f"case-{index}") for index in range(4)]
    variant = EvaluationVariant(
        name="candidate", model="model-b", prompt_version="v2", context_policy="compact"
    )
    store = MemoryEvaluationStore()

    def runner(case, _variant, repetition):
        index = cases.index(case)
        solved = (index + repetition) % 4 != 0
        return Observation(
            trace_id=case.trace_id,
            repetition=repetition,
            solved=solved,
            score=float(solved),
            cost_usd=0.01,
            duration_ms=10 + index,
        )

    report = RegressionEvaluator(runner, store).evaluate("suite", cases, variant, repetitions=5)
    assert len(report.observations) == 20
    assert report.solve_rate == 0.75
    assert report.total_cost_usd == pytest.approx(0.2)
    assert report.max_repeat_deviation_points >= 0
    assert store.reports[report.id] == report


def test_report_comparison_returns_directional_deltas() -> None:
    case = EvaluationCase(trace_id=uuid4(), name="one")

    def runner(_case, variant, repetition):
        solved = variant.name == "candidate"
        return Observation(
            trace_id=case.trace_id,
            repetition=repetition,
            solved=solved,
            score=float(solved),
            cost_usd=0.02 if solved else 0.01,
            duration_ms=10,
        )

    evaluator = RegressionEvaluator(runner)
    baseline = evaluator.evaluate(
        "suite",
        [case],
        EvaluationVariant(name="base", model="a", prompt_version="1", context_policy="all"),
    )
    candidate = evaluator.evaluate(
        "suite",
        [case],
        EvaluationVariant(name="candidate", model="b", prompt_version="2", context_policy="all"),
    )
    delta = compare_reports(baseline, candidate)
    assert delta["solve_rate_delta"] == 1
    assert delta["cost_delta_usd"] > 0


def test_replay_case_runner_links_observation_to_replay() -> None:
    case = EvaluationCase(trace_id=uuid4(), name="trace")
    replay_id = uuid4()

    class Result:
        id = replay_id
        divergence_count = 0

    class Engine:
        def replay(self, trace_id, mode):
            assert trace_id == case.trace_id
            return Result()

    runner = ReplayCaseRunner(
        engine_factory=lambda _variant, _repeat: Engine(),
        scorer=lambda _case, _result: 1.0,
        cost_estimator=lambda _case, _variant, _result: 0.02,
    )
    observation = runner(
        case,
        EvaluationVariant(name="v", model="m", prompt_version="p", context_policy="c"),
        0,
    )
    assert observation.solved
    assert observation.replay_id == replay_id
