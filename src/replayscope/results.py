from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID

from replayscope.models import ReplayResult


@dataclass
class MemoryReplayResultStore:
    results: dict[UUID, ReplayResult] = field(default_factory=dict)

    def save(self, result: ReplayResult) -> None:
        self.results[result.id] = result.model_copy(deep=True)

    def get(self, result_id: UUID) -> ReplayResult:
        return self.results[result_id].model_copy(deep=True)


class PostgresReplayResultStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    def save(self, result: ReplayResult) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO replay_runs
                (id, trace_id, mode, status, divergence_count, summary, started_at, finished_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    result.id,
                    result.trace_id,
                    result.mode.value,
                    result.status,
                    result.divergence_count,
                    json.dumps({}),
                    result.started_at,
                    result.finished_at,
                ),
            )
            for divergence in result.divergences:
                conn.execute(
                    """INSERT INTO divergences
                    (replay_id, event_sequence, kind, path, expected, actual, message)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        result.id,
                        divergence.event_sequence,
                        divergence.kind.value,
                        divergence.path,
                        json.dumps(divergence.expected),
                        json.dumps(divergence.actual),
                        divergence.message,
                    ),
                )
