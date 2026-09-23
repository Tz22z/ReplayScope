#!/usr/bin/env python3
"""Run a live OpenAI replay benchmark and persist every model observation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from replayscope.cas import LocalCAS
from replayscope.models import EventKind, ReplayMode, TraceCreate, TraceStatus
from replayscope.replay import ReplayEngine
from replayscope.repository import MemoryTraceRepository


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    prompt: str
    expected: str


def normalize_answer(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.lower().startswith("text"):
            text = text[4:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return " ".join(text.split()).casefold()


def build_cases(count: int) -> list[EvalCase]:
    if count < 1:
        raise ValueError("case count must be positive")
    cases: list[EvalCase] = []
    for index in range(count):
        category_index = index % 4
        if category_index == 0:
            left = 37 + index * 7
            right = 5 + index % 11
            offset = 13 + index
            expected = str(left * right - offset)
            task = f"Compute ({left} * {right}) - {offset}."
            category = "arithmetic"
        elif category_index == 1:
            token = f"trace{index:03d}scope{(index * 17) % 101:03d}"
            expected = token[::-1]
            task = f"Reverse this ASCII string exactly: {token}"
            category = "string_reverse"
        elif category_index == 2:
            values = [((index + 3) * factor * 19) % 97 for factor in (5, 2, 7, 3, 11)]
            expected = ",".join(str(value) for value in sorted(values))
            task = f"Sort these integers ascending and join them with commas: {values}"
            category = "sorting"
        else:
            token = f"replayability{index:03d}evaluation"
            expected = str(sum(character in "aeiou" for character in token))
            task = f"Count the lowercase vowels (a, e, i, o, u) in: {token}"
            category = "counting"
        cases.append(
            EvalCase(
                case_id=f"case-{index:03d}",
                category=category,
                prompt=(
                    "Solve the deterministic task below. Return only the exact final answer, "
                    f"with no explanation.\n\n{task}"
                ),
                expected=normalize_answer(expected),
            )
        )
    return cases


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


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
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def usage_value(container: Any, name: str) -> int:
    value = getattr(container, name, 0) if container is not None else 0
    return int(value or 0)


def call_openai(
    case: EvalCase,
    repetition: int,
    *,
    model: str,
    max_output_tokens: int,
    reasoning_effort: str,
    timeout_seconds: float,
    clients: threading.local,
) -> dict[str, Any]:
    client = getattr(clients, "client", None)
    if client is None:
        client = OpenAI(max_retries=3, timeout=timeout_seconds)
        clients.client = client
    started = time.perf_counter()
    response = client.responses.create(
        model=model,
        input=case.prompt,
        max_output_tokens=max_output_tokens,
        reasoning={"effort": reasoning_effort},
        store=False,
    )
    latency = time.perf_counter() - started
    usage = response.usage
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    output = response.output_text or ""
    normalized = normalize_answer(output)
    return {
        "case_id": case.case_id,
        "category": case.category,
        "repetition": repetition,
        "expected": case.expected,
        "response_id": response.id,
        "status": response.status,
        "model": response.model,
        "output": output,
        "normalized_output": normalized,
        "solved": response.status == "completed" and normalized == case.expected,
        "latency_seconds": round(latency, 6),
        "usage": {
            "input_tokens": usage_value(usage, "input_tokens"),
            "cached_input_tokens": usage_value(input_details, "cached_tokens"),
            "output_tokens": usage_value(usage, "output_tokens"),
            "reasoning_tokens": usage_value(output_details, "reasoning_tokens"),
            "total_tokens": usage_value(usage, "total_tokens"),
        },
    }


def replay_results(cases: list[EvalCase], observations: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        by_case.setdefault(observation["case_id"], []).append(observation)

    replay_rows = []
    with tempfile.TemporaryDirectory(prefix="replayscope-live-") as directory:
        cas = LocalCAS(Path(directory) / "cas")
        repository = MemoryTraceRepository()
        for case in cases:
            rows = sorted(by_case[case.case_id], key=lambda item: item["repetition"])
            baseline = rows[0]
            trace = repository.create_trace(
                TraceCreate(
                    source_run_id=f"openai:{case.case_id}",
                    framework="openai-responses",
                    labels={"category": case.category},
                )
            )
            input_ref = cas.put_json({"prompt": case.prompt, "expected": case.expected})
            output_ref = cas.put_json({"answer": baseline["normalized_output"]})
            repository.register_blob(input_ref)
            repository.register_blob(output_ref)
            repository.append_event(
                trace.id,
                EventKind.MODEL_CALL,
                "openai.responses",
                input_ref=input_ref,
                output_ref=output_ref,
                metadata={
                    "model": baseline["model"],
                    "response_id": baseline["response_id"],
                    "repetition": 0,
                },
                duration_ms=baseline["latency_seconds"] * 1000,
            )
            repository.finish_trace(trace.id, TraceStatus.SUCCEEDED)
            for row in rows[1:]:
                answer = row["normalized_output"]

                def adapter(_request, _context, *, current=answer):
                    return {"answer": current}

                replay = ReplayEngine(
                    repository,
                    cas,
                    model_adapters={"openai.responses": adapter},
                ).replay(trace.id, ReplayMode.FRESH_MODEL)
                replay_rows.append(
                    {
                        "case_id": case.case_id,
                        "repetition": row["repetition"],
                        "trace_id": str(trace.id),
                        "status": replay.status,
                        "divergence_count": replay.divergence_count,
                    }
                )
    divergent = sum(row["status"] == "diverged" for row in replay_rows)
    return {
        "recorded_traces": len(cases),
        "fresh_replays": len(replay_rows),
        "matched_replays": len(replay_rows) - divergent,
        "divergent_replays": divergent,
        "divergence_rate_percent": round(divergent / max(1, len(replay_rows)) * 100, 2),
        "rows": replay_rows,
    }


def summarize(
    cases: list[EvalCase],
    observations: list[dict[str, Any]],
    replay: dict[str, Any],
    *,
    input_price_per_million: float,
    output_price_per_million: float,
) -> dict[str, Any]:
    latencies = [row["latency_seconds"] for row in observations]
    total_input = sum(row["usage"]["input_tokens"] for row in observations)
    total_cached = sum(row["usage"]["cached_input_tokens"] for row in observations)
    total_output = sum(row["usage"]["output_tokens"] for row in observations)
    solved = sum(row["solved"] for row in observations)
    by_case = {
        case.case_id: [row for row in observations if row["case_id"] == case.case_id]
        for case in cases
    }
    stable_cases = sum(
        len({row["normalized_output"] for row in rows}) == 1 for rows in by_case.values()
    )
    estimated_cost = (
        (total_input - total_cached) * input_price_per_million
        + total_cached * input_price_per_million / 10
        + total_output * output_price_per_million
    ) / 1_000_000
    return {
        "calls": len(observations),
        "completed": sum(row["status"] == "completed" for row in observations),
        "solved": solved,
        "solve_rate_percent": round(solved / len(observations) * 100, 2),
        "stable_cases": stable_cases,
        "stable_case_rate_percent": round(stable_cases / len(cases) * 100, 2),
        "replay": {key: value for key, value in replay.items() if key != "rows"},
        "latency_seconds": {
            "median": round(statistics.median(latencies), 4),
            "p95": round(percentile(latencies, 0.95), 4),
            "p99": round(percentile(latencies, 0.99), 4),
        },
        "usage": {
            "input_tokens": total_input,
            "cached_input_tokens": total_cached,
            "output_tokens": total_output,
            "total_tokens": sum(row["usage"]["total_tokens"] for row in observations),
        },
        "pricing_usd_per_million_tokens": {
            "input": input_price_per_million,
            "cached_input": input_price_per_million / 10,
            "output": output_price_per_million,
        },
        "estimated_cost_usd": round(estimated_cost, 6),
    }


def render_markdown(report: dict[str, Any]) -> str:
    result = report["results"]
    replay = result["replay"]
    latency = result["latency_seconds"]
    return "\n".join(
        [
            "# ReplayScope live OpenAI benchmark",
            "",
            f"- Model: `{report['configuration']['model']}` (resolved to `{report['model_snapshot']}`)",
            f"- Live Responses API calls: {result['calls']} ({result['completed']} completed)",
            f"- Exact-answer solve rate: {result['solve_rate_percent']:.2f}%",
            f"- Stable cases: {result['stable_cases']}/{report['configuration']['cases']}",
            (
                f"- Replay divergence: {replay['divergent_replays']}/"
                f"{replay['fresh_replays']} ({replay['divergence_rate_percent']:.2f}%)"
            ),
            f"- Latency: median {latency['median']:.4f}s; p95 {latency['p95']:.4f}s",
            f"- Estimated API cost: ${result['estimated_cost_usd']:.6f}",
            "",
            "Raw prompts, responses, response IDs, token usage, latency, and replay decisions are in",
            "the JSON artifact. The API key is never written to either artifact.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--reasoning-effort", default="minimal")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--input-price-per-million", type=float, default=0.25)
    parser.add_argument("--output-price-per-million", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("artifacts/live-openai-100x5.json"))
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    if args.repeats < 2:
        raise SystemExit("--repeats must be at least 2")

    cases = build_cases(args.cases)
    clients = threading.local()
    observations: list[dict[str, Any]] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                call_openai,
                case,
                repetition,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                reasoning_effort=args.reasoning_effort,
                timeout_seconds=args.timeout,
                clients=clients,
            ): (case.case_id, repetition)
            for case in cases
            for repetition in range(args.repeats)
        }
        for future in as_completed(futures):
            observations.append(future.result())
    observations.sort(key=lambda row: (row["case_id"], row["repetition"]))
    replay = replay_results(cases, observations)
    results = summarize(
        cases,
        observations,
        replay,
        input_price_per_million=args.input_price_per_million,
        output_price_per_million=args.output_price_per_million,
    )
    repository = Path(__file__).resolve().parents[1]
    model_snapshots = sorted({row["model"] for row in observations})
    assertions = {
        "all_calls_completed": results["completed"] == args.cases * args.repeats,
        "all_observations_present": len(observations) == args.cases * args.repeats,
        "all_replays_present": replay["fresh_replays"] == args.cases * (args.repeats - 1),
        "single_model_snapshot": len(model_snapshots) == 1,
    }
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "ReplayScope live OpenAI response replay",
        "configuration": {
            "model": args.model,
            "cases": args.cases,
            "repeats": args.repeats,
            "concurrency": args.concurrency,
            "max_output_tokens": args.max_output_tokens,
            "reasoning_effort": args.reasoning_effort,
        },
        "model_snapshot": model_snapshots[0] if len(model_snapshots) == 1 else model_snapshots,
        "runtime": {
            "git_head": git_head(repository),
            "source_tree_sha256": tree_sha256(repository / "src" / "replayscope"),
            "runner_sha256": file_sha256(Path(__file__)),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "wall_seconds": round(time.perf_counter() - started, 4),
        },
        "results": results,
        "assertions": assertions,
        "cases": [case.__dict__ for case in cases],
        "observations": observations,
        "replays": replay["rows"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"results": results, "assertions": assertions}, indent=2))
    if not all(assertions.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
