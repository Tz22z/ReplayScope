#!/usr/bin/env python3
"""Reproduce the scale claims reported by ReplayScope."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_head(repository: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def replay_experiment(cas: LocalCAS, count: int = 240) -> dict[str, Any]:
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
    cases = []
    for index, result in enumerate(results):
        injected = index in drift_cases
        observed = result.status == "diverged"
        cases.append(
            {
                "case": index,
                "injected_drift": injected,
                "observed_divergence": observed,
                "divergence_count": result.divergence_count,
            }
        )
    true_positive = sum(row["injected_drift"] and row["observed_divergence"] for row in cases)
    false_positive = sum(not row["injected_drift"] and row["observed_divergence"] for row in cases)
    false_negative = sum(row["injected_drift"] and not row["observed_divergence"] for row in cases)
    true_negative = count - true_positive - false_positive - false_negative
    divergent = true_positive + false_positive
    return {
        "traces": count,
        "injected_drifts": len(drift_cases),
        "matched": count - divergent,
        "divergent": divergent,
        "divergence_rate_percent": round(divergent / count * 100, 2),
        "confusion_matrix": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "precision": round(true_positive / max(1, true_positive + false_positive), 6),
        "recall": round(true_positive / max(1, true_positive + false_negative), 6),
        "duration_seconds": round(time.perf_counter() - started, 4),
        "cases": cases,
    }


def reduction_experiment(cas: LocalCAS, count: int = 30, stable_faults: int = 26) -> dict[str, Any]:
    results = []
    cases = []
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

        def oracle(candidate, workspace, reproducible=index < stable_faults):
            triggered = (
                reproducible
                and "input-2" in candidate.inputs
                and "tool-4" in candidate.tool_returns
                and (workspace / "file-5.txt").exists()
            )
            return "InjectedFailure:stable" if triggered else None

        result = FailureReducer(cas, oracle, "InjectedFailure:stable", stability_runs=2).reduce(
            package
        )
        results.append(result)
        cases.append(
            {
                "case": index,
                "stable_fault": index < stable_faults,
                "status": result.status,
                "original_size": result.original_size,
                "minimal_size": result.minimal_size,
                "size_reduction_percent": round(result.size_reduction * 100, 2),
                "trials": result.trials,
                "cache_hits": result.cache_hits,
            }
        )
    reproduced = [result for result in results if result.status == "succeeded"]
    false_positives = sum(not row["stable_fault"] and row["status"] == "succeeded" for row in cases)
    false_negatives = sum(row["stable_fault"] and row["status"] != "succeeded" for row in cases)
    return {
        "faults": count,
        "stable_faults": stable_faults,
        "unstable_or_absent_faults": count - stable_faults,
        "reproduced": len(reproduced),
        "reproduction_rate_percent": round(len(reproduced) / count * 100, 2),
        "average_size_reduction_percent": round(
            statistics.fmean(result.size_reduction for result in reproduced) * 100, 2
        ),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "total_trials": sum(result.trials for result in results),
        "duration_seconds": round(time.perf_counter() - started, 4),
        "cases": cases,
    }


def evaluation_experiment(case_count: int = 100) -> dict[str, Any]:
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
    repeat_solved = [
        sum(item.solved for item in report.observations if item.repetition == repetition)
        for repetition in range(report.repetitions)
    ]
    return {
        "cases": case_count,
        "repetitions": report.repetitions,
        "observations": len(report.observations),
        "solve_rate_percent": round(report.solve_rate * 100, 2),
        "repeat_solved": repeat_solved,
        "repeat_solve_rates_percent": [
            round(value / case_count * 100, 2) for value in repeat_solved
        ],
        "score_stddev_points": round(report.score_stddev_points, 2),
        "max_repeat_deviation_points": round(report.max_repeat_deviation_points, 2),
        "variant": variant.model_dump(mode="json"),
        "total_cost_usd": round(report.total_cost_usd, 4),
        "p95_duration_ms": report.p95_duration_ms,
    }


def render_markdown(output: dict[str, Any]) -> str:
    replay = output["replay"]
    reduction = output["reduction"]
    evaluation = output["evaluation"]
    checks = output["assertions"]
    return "\n".join(
        [
            "# ReplayScope formal validation",
            "",
            "Deterministic, ground-truth-injected validation; no external model calls.",
            "",
            "| Experiment | Result |",
            "| --- | --- |",
            (
                f"| Replay drift detection | {replay['traces']} traces; "
                f"{replay['injected_drifts']} injected; precision {replay['precision']:.1%}; "
                f"recall {replay['recall']:.1%} |"
            ),
            (
                f"| Failure reduction | {reduction['reproduced']}/{reduction['faults']} "
                f"stable faults reproduced; {reduction['average_size_reduction_percent']:.2f}% "
                "mean reduction; 0 false results |"
            ),
            (
                f"| Regression aggregation | {evaluation['cases']} cases x "
                f"{evaluation['repetitions']} repeats; {evaluation['solve_rate_percent']:.2f}% "
                f"solve rate; {evaluation['score_stddev_points']:.2f}-point stddev |"
            ),
            "",
            "## Integrity",
            "",
            f"- Source tree SHA-256: `{output['runtime']['source_tree_sha256']}`",
            f"- Runner SHA-256: `{output['runtime']['runner_sha256']}`",
            f"- All assertions passed: `{all(checks.values())}`",
            "",
            "## Limitations",
            "",
            "- The suite validates replay, reduction, and aggregation mechanics with injected ground truth.",
            "- It does not estimate natural production drift or live-model quality.",
            "- Timing is local-machine diagnostic data, not a service-level objective.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    cas = LocalCAS(args.output_dir / ".experiment-cas")
    replay = replay_experiment(cas)
    reduction = reduction_experiment(cas)
    evaluation = evaluation_experiment()
    assertions = {
        "replay_no_false_positives": replay["confusion_matrix"]["false_positive"] == 0,
        "replay_no_false_negatives": replay["confusion_matrix"]["false_negative"] == 0,
        "reduction_expected_stable_faults": reduction["reproduced"] == 26,
        "reduction_no_false_positives": reduction["false_positives"] == 0,
        "reduction_no_false_negatives": reduction["false_negatives"] == 0,
        "evaluation_observation_count": evaluation["observations"] == 500,
    }
    output = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "methodology": {
            "kind": "deterministic synthetic ground-truth validation",
            "external_model_calls": False,
            "replay_injection_rule": "every 16th trace",
            "reduction_stability_runs": 2,
        },
        "runtime": {
            "git_head": git_head(repository),
            "source_tree_sha256": tree_sha256(repository / "src" / "replayscope"),
            "runner_sha256": file_sha256(Path(__file__)),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "replay": replay,
        "reduction": reduction,
        "evaluation": evaluation,
        "assertions": assertions,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "validation.json"
    destination.write_text(json.dumps(output, indent=2) + "\n")
    (args.output_dir / "validation.md").write_text(render_markdown(output))
    print(json.dumps(output, indent=2))
    if not all(assertions.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
