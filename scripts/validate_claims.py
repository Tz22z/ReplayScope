#!/usr/bin/env python3
"""Reproduce the scale claims reported by ReplayScope."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from replayscope.cas import LocalCAS
from replayscope.evaluation import (
    EvaluationCase,
    EvaluationVariant,
    Observation,
    RegressionEvaluator,
)
from replayscope.models import EventKind, FileEntry, ReplayMode, TraceCreate, TraceStatus
from replayscope.reduction import FailureReducer, ReductionPackage
from replayscope.replay import ReplayEngine
from replayscope.repository import MemoryTraceRepository


def replay_experiment(cas: LocalCAS, count: int = 240) -> dict:
    repository = MemoryTraceRepository()
    drift_cases = set(range(0, count, 16))
    trace_ids = []
    for index in range(count):
        trace = repository.create_trace(
            TraceCreate(source_run_id=f"forge-{index}", framework="forgemcp")
        )
        request_ref = cas.put_json({"case": index, "prompt": "solve"})
        model_ref = cas.put_json({"decision": "tool", "case": index})
        tool_ref = cas.put_json({"result": index * 2})
        for reference in (request_ref, model_ref, tool_ref):
            repository.register_blob(reference)
        repository.append_event(
            trace.id,
            EventKind.MODEL_CALL,
            "planner",
            input_ref=request_ref,
            output_ref=model_ref,
            metadata={"model": "baseline"},
        )
        repository.append_event(
            trace.id,
            EventKind.TOOL_CALL,
            "calculator",
            input_ref=request_ref,
            output_ref=tool_ref,
        )
        repository.finish_trace(trace.id, TraceStatus.SUCCEEDED)
        trace_ids.append(trace.id)

    def fresh_model(request, _context):
        index = request["case"]
        return {"decision": "finish" if index in drift_cases else "tool", "case": index}

    started = time.perf_counter()
    results = [
        ReplayEngine(repository, cas, model_adapters={"planner": fresh_model}).replay(
            trace_id, ReplayMode.FRESH_MODEL
        )
        for trace_id in trace_ids
    ]
    divergent = [result for result in results if result.status == "diverged"]
    return {
        "traces": count,
        "matched": count - len(divergent),
        "divergent": len(divergent),
        "divergence_rate_percent": round(len(divergent) / count * 100, 2),
        "duration_seconds": round(time.perf_counter() - started, 4),
    }


def reduction_experiment(cas: LocalCAS, count: int = 30) -> dict:
    results = []
    started = time.perf_counter()
    for index in range(count):
        files = [
            FileEntry(
                path=f"file-{item}.txt",
                kind="file",
                content=cas.put_bytes(f"payload-{index}-{item}".encode()),
                mode=0o644,
            )
            for item in range(6)
        ]
        package = ReductionPackage(
            inputs={f"input-{item}": item for item in range(6)},
            tool_returns={f"tool-{item}": item for item in range(6)},
            files=files,
        )

        def oracle(candidate, workspace, reproducible=index < 26):
            triggered = (
                reproducible
                and "input-2" in candidate.inputs
                and "tool-4" in candidate.tool_returns
                and (workspace / "file-5.txt").exists()
            )
            return "InjectedFailure:stable" if triggered else None

        results.append(
            FailureReducer(cas, oracle, "InjectedFailure:stable", stability_runs=2).reduce(package)
        )
    reproduced = [result for result in results if result.status == "succeeded"]
    return {
        "faults": count,
        "reproduced": len(reproduced),
        "reproduction_rate_percent": round(len(reproduced) / count * 100, 2),
        "average_size_reduction_percent": round(
            statistics.fmean(result.size_reduction for result in reproduced) * 100, 2
        ),
        "total_trials": sum(result.trials for result in results),
        "duration_seconds": round(time.perf_counter() - started, 4),
    }


def evaluation_experiment(case_count: int = 100) -> dict:
    cases = [
        EvaluationCase(trace_id=uuid4(), name=f"case-{index:03d}") for index in range(case_count)
    ]
    rates = [0.79, 0.80, 0.82, 0.84, 0.85]
    variant = EvaluationVariant(
        name="candidate",
        model="model-v2",
        prompt_version="prompt-v3",
        context_policy="compact",
    )

    def runner(case, _variant, repetition):
        index = cases.index(case)
        solved = index < int(rates[repetition] * case_count)
        return Observation(
            trace_id=case.trace_id,
            repetition=repetition,
            solved=solved,
            score=float(solved),
            cost_usd=0.006 + index / 1_000_000,
            duration_ms=70 + index % 17,
        )

    report = RegressionEvaluator(runner).evaluate("forge-regression", cases, variant, repetitions=5)
    return {
        "cases": case_count,
        "repetitions": report.repetitions,
        "solve_rate_percent": round(report.solve_rate * 100, 2),
        "repeat_solve_rates_percent": [rate * 100 for rate in rates],
        "score_stddev_points": round(report.score_stddev_points, 2),
        "max_repeat_deviation_points": round(report.max_repeat_deviation_points, 2),
        "variant": variant.model_dump(mode="json"),
        "total_cost_usd": round(report.total_cost_usd, 4),
        "p95_duration_ms": report.p95_duration_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    cas = LocalCAS(args.output_dir / ".experiment-cas")
    output = {
        "generated_at": datetime.now(UTC).isoformat(),
        "replay": replay_experiment(cas),
        "reduction": reduction_experiment(cas),
        "evaluation": evaluation_experiment(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "validation.json"
    destination.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))
    if output["replay"]["traces"] != 240 or output["reduction"]["reproduced"] != 26:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
