from __future__ import annotations

import json
from uuid import UUID, uuid4

from replayscope.evaluation import EvaluationReport
from replayscope.models import ReplayMode
from replayscope.reduction import ReductionPackage, ReductionResult


class PostgresEvaluationStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    def save(self, report: EvaluationReport) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO evaluation_runs
                (id, suite_name, variant, repetitions, case_count, solve_rate,
                 score_stddev_points, total_cost_usd, summary, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    report.id,
                    report.suite_name,
                    json.dumps(report.variant.model_dump(mode="json")),
                    report.repetitions,
                    report.case_count,
                    report.solve_rate,
                    report.score_stddev_points,
                    report.total_cost_usd,
                    json.dumps(
                        {
                            "mean_score": report.mean_score,
                            "max_repeat_deviation_points": report.max_repeat_deviation_points,
                            "mean_cost_usd": report.mean_cost_usd,
                            "p95_duration_ms": report.p95_duration_ms,
                        }
                    ),
                    report.created_at,
                ),
            )
            for item in report.observations:
                conn.execute(
                    """INSERT INTO evaluation_observations
                    (evaluation_id, trace_id, repetition, solved, score, cost_usd,
                     duration_ms, replay_id, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        report.id,
                        item.trace_id,
                        item.repetition,
                        item.solved,
                        item.score,
                        item.cost_usd,
                        item.duration_ms,
                        item.replay_id,
                        json.dumps(item.metadata),
                    ),
                )


class PostgresReductionStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    def start(
        self,
        trace_id: UUID,
        signature: str,
        original_size: int,
        mode: ReplayMode = ReplayMode.RECORDED,
    ) -> UUID:
        job_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO reduction_jobs
                (id, trace_id, mode, status, baseline_signature, original_event_count)
                VALUES (%s,%s,%s,'running',%s,%s)""",
                (job_id, trace_id, mode.value, signature, original_size),
            )
        return job_id

    def record_trial(
        self,
        job_id: UUID,
        candidate_hash: str,
        package: ReductionPackage,
        reproduces: bool,
        signature: str | None,
        duration_ms: float,
    ) -> None:
        sequences = package.model_dump(mode="json")
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO reduction_trials
                (job_id, candidate_hash, sequences, reproduces, observed_signature, duration_ms)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id, candidate_hash) DO NOTHING""",
                (job_id, candidate_hash, json.dumps(sequences), reproduces, signature, duration_ms),
            )

    def finish(self, job_id: UUID, result: ReductionResult) -> None:
        package = result.package.model_dump(mode="json") if result.package else None
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE reduction_jobs SET status = %s, minimal_sequences = %s,
                trials = %s, cache_hits = %s, finished_at = now() WHERE id = %s""",
                (result.status, json.dumps(package), result.trials, result.cache_hits, job_id),
            )
