import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from replayscope.cas import LocalCAS
from replayscope.database import create_pool, migrate
from replayscope.evaluation import (
    EvaluationCase,
    EvaluationVariant,
    Observation,
    RegressionEvaluator,
)
from replayscope.integrity import verify_event_chain
from replayscope.models import EventKind, ReplayMode, TraceCreate, TraceStatus
from replayscope.persistence import PostgresReductionStore
from replayscope.recorder import Recorder
from replayscope.reduction import FailureReducer, ReductionPackage
from replayscope.replay import ReplayEngine
from replayscope.repository import PostgresTraceRepository
from replayscope.results import PostgresReplayResultStore

pytestmark = pytest.mark.skipif(
    not os.getenv("REPLAYSCOPE_INTEGRATION"), reason="set REPLAYSCOPE_INTEGRATION=1"
)


@pytest.fixture
def database(tmp_path: Path):
    url = os.getenv(
        "REPLAYSCOPE_DATABASE_URL",
        "postgresql://replayscope:replayscope@localhost:5433/replayscope",
    )
    pool = create_pool(url)
    migrate(pool)
    with pool.connection() as conn:
        conn.execute(
            """TRUNCATE evaluation_observations, evaluation_runs, reduction_trials,
            reduction_jobs, divergences, replay_runs, trace_events, blobs, traces
            RESTART IDENTITY CASCADE"""
        )
    yield pool, PostgresTraceRepository(pool), LocalCAS(tmp_path / "cas")
    pool.close()


def test_record_and_replay_round_trip(database) -> None:
    pool, repository, cas = database
    recorder = Recorder(repository, cas)
    trace = recorder.start("postgres-run")
    recorder.record_model_call(
        trace.id, "planner", {"task": "x"}, lambda: {"action": "done"}, model="m1"
    )
    finished = recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
    events = repository.events(trace.id)
    verify_event_chain(finished, events)
    result = ReplayEngine(
        repository,
        cas,
        model_adapters={"planner": lambda *_: {"action": "changed"}},
        result_store=PostgresReplayResultStore(pool),
    ).replay(trace.id, ReplayMode.FRESH_MODEL)
    assert result.divergence_count == 1
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM divergences").fetchone()["n"] == 1


def test_concurrent_append_produces_contiguous_verified_chain(database) -> None:
    _pool, repository, cas = database
    trace = repository.create_trace(TraceCreate(source_run_id="parallel"))

    def append(index):
        reference = cas.put_json({"index": index})
        repository.register_blob(reference)
        return repository.append_event(
            trace.id, EventKind.TOOL_CALL, f"tool-{index}", output_ref=reference
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(append, range(50)))
    finished = repository.finish_trace(trace.id, TraceStatus.SUCCEEDED)
    events = repository.events(trace.id)
    assert [event.sequence for event in events] == list(range(50))
    verify_event_chain(finished, events)


def test_evaluation_report_persists_observations(database) -> None:
    pool, repository, _cas = database
    traces = [
        repository.create_trace(TraceCreate(source_run_id=f"case-{index}")) for index in range(4)
    ]
    cases = [EvaluationCase(trace_id=trace.id, name=trace.source_run_id) for trace in traces]

    def runner(case, _variant, repetition):
        return Observation(
            trace_id=case.trace_id,
            repetition=repetition,
            solved=True,
            score=1,
            cost_usd=0.01,
            duration_ms=2,
        )

    from replayscope.persistence import PostgresEvaluationStore

    report = RegressionEvaluator(runner, PostgresEvaluationStore(pool)).evaluate(
        "integration",
        cases,
        EvaluationVariant(name="v1", model="m1", prompt_version="p1", context_policy="full"),
        repetitions=5,
    )
    assert len(report.observations) == 20
    with pool.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM evaluation_observations").fetchone()["n"]
    assert count == 20


def test_migration_is_idempotent(database) -> None:
    pool, _repository, _cas = database
    migrate(pool)
    migrate(pool)


def test_reduction_trials_are_persisted(database) -> None:
    pool, repository, cas = database
    trace = repository.create_trace(TraceCreate(source_run_id="reduce"))
    package = ReductionPackage(inputs={f"field-{index}": index for index in range(12)})
    store = PostgresReductionStore(pool)
    job_id = store.start(trace.id, "failure:stable", package.size)

    def sink(candidate_hash, candidate, reproduces, signature, duration):
        store.record_trial(job_id, candidate_hash, candidate, reproduces, signature, duration)

    result = FailureReducer(
        cas,
        lambda candidate, _workspace: "failure:stable" if "field-3" in candidate.inputs else None,
        "failure:stable",
        trial_sink=sink,
        stability_runs=1,
    ).reduce(package)
    store.finish(job_id, result)
    with pool.connection() as conn:
        job = conn.execute("SELECT * FROM reduction_jobs WHERE id = %s", (job_id,)).fetchone()
        trials = conn.execute(
            "SELECT count(*) AS count FROM reduction_trials WHERE job_id = %s", (job_id,)
        ).fetchone()["count"]
    assert job["status"] == "succeeded"
    assert trials == result.trials
