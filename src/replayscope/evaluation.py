from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from replayscope.models import ContentRef


class EvaluationVariant(BaseModel):
    name: str
    model: str
    prompt_version: str
    context_policy: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationCase(BaseModel):
    trace_id: UUID
    name: str
    weight: float = Field(default=1.0, gt=0)
    final_workspace_ref: ContentRef | None = None


class Observation(BaseModel):
    trace_id: UUID
    repetition: int
    solved: bool
    score: float = Field(ge=0, le=1)
    cost_usd: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    replay_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    suite_name: str
    variant: EvaluationVariant
    repetitions: int
    case_count: int
    solve_rate: float
    mean_score: float
    score_stddev_points: float
    max_repeat_deviation_points: float
    total_cost_usd: float
    mean_cost_usd: float
    p95_duration_ms: float
    observations: list[Observation]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


CaseRunner = Callable[[EvaluationCase, EvaluationVariant, int], Observation]


@dataclass
class ReplayCaseRunner:
    engine_factory: Callable[[EvaluationVariant, int], Any]
    scorer: Callable[[EvaluationCase, Any], float]
    cost_estimator: Callable[[EvaluationCase, EvaluationVariant, Any], float]
    solved_threshold: float = 1.0

    def __call__(
        self, case: EvaluationCase, variant: EvaluationVariant, repetition: int
    ) -> Observation:
        from replayscope.models import ReplayMode

        started = time.perf_counter()
        result = self.engine_factory(variant, repetition).replay(
            case.trace_id, ReplayMode.FRESH_MODEL
        )
        score = self.scorer(case, result)
        return Observation(
            trace_id=case.trace_id,
            repetition=repetition,
            solved=score >= self.solved_threshold,
            score=score,
            cost_usd=self.cost_estimator(case, variant, result),
            duration_ms=(time.perf_counter() - started) * 1000,
            replay_id=result.id,
            metadata={"divergence_count": result.divergence_count},
        )


@dataclass
class RegressionEvaluator:
    runner: CaseRunner
    report_store: Any | None = None

    def evaluate(
        self,
        suite_name: str,
        cases: list[EvaluationCase],
        variant: EvaluationVariant,
        repetitions: int = 5,
    ) -> EvaluationReport:
        if not cases:
            raise ValueError("evaluation suite cannot be empty")
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        observations = [
            self.runner(case, variant, repetition)
            for repetition in range(repetitions)
            for case in cases
        ]
        repeat_scores = []
        total_weight = sum(case.weight for case in cases)
        weights = {case.trace_id: case.weight for case in cases}
        for repetition in range(repetitions):
            current = [item for item in observations if item.repetition == repetition]
            repeat_scores.append(
                sum(item.score * weights[item.trace_id] for item in current) / total_weight
            )
        costs = [item.cost_usd for item in observations]
        durations = sorted(item.duration_ms for item in observations)
        report = EvaluationReport(
            suite_name=suite_name,
            variant=variant,
            repetitions=repetitions,
            case_count=len(cases),
            solve_rate=sum(item.solved for item in observations) / len(observations),
            mean_score=statistics.fmean(item.score for item in observations),
            score_stddev_points=statistics.pstdev(repeat_scores) * 100,
            max_repeat_deviation_points=max(
                abs(score - statistics.fmean(repeat_scores)) for score in repeat_scores
            )
            * 100,
            total_cost_usd=sum(costs),
            mean_cost_usd=statistics.fmean(costs),
            p95_duration_ms=durations[
                max(0, min(len(durations) - 1, int(len(durations) * 0.95 + 0.999) - 1))
            ],
            observations=observations,
        )
        if self.report_store:
            self.report_store.save(report)
        return report


@dataclass
class MemoryEvaluationStore:
    reports: dict[UUID, EvaluationReport] = field(default_factory=dict)

    def save(self, report: EvaluationReport) -> None:
        self.reports[report.id] = report.model_copy(deep=True)


def compare_reports(baseline: EvaluationReport, candidate: EvaluationReport) -> dict[str, float]:
    return {
        "solve_rate_delta": candidate.solve_rate - baseline.solve_rate,
        "mean_score_delta": candidate.mean_score - baseline.mean_score,
        "cost_delta_usd": candidate.total_cost_usd - baseline.total_cost_usd,
        "p95_duration_delta_ms": candidate.p95_duration_ms - baseline.p95_duration_ms,
    }
