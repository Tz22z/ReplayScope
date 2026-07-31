from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from replayscope.bundle import export_trace, inspect_bundle
from replayscope.cas import LocalCAS
from replayscope.config import Settings
from replayscope.database import create_pool, migrate
from replayscope.integrity import verify_event_chain
from replayscope.models import ReplayMode, TraceStatus
from replayscope.recorder import Recorder
from replayscope.reduction import FailureReducer, ReductionPackage
from replayscope.replay import ReplayEngine
from replayscope.repository import PostgresTraceRepository
from replayscope.results import PostgresReplayResultStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="replayscope")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    demo = commands.add_parser("record-demo")
    demo.add_argument("--source-run", default="demo-agent-run")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("trace_id", type=UUID)
    replay = commands.add_parser("replay")
    replay.add_argument("trace_id", type=UUID)
    replay.add_argument("--mode", type=ReplayMode, default=ReplayMode.RECORDED)
    replay.add_argument("--drift", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("trace_id", type=UUID)
    export.add_argument("destination", type=Path)
    verify = commands.add_parser("verify-bundle")
    verify.add_argument("source", type=Path)
    reduce_demo = commands.add_parser("reduce-demo")
    reduce_demo.add_argument("--items", type=int, default=50)
    return root


def main() -> None:
    args = parser().parse_args()
    settings = Settings()
    pool = create_pool(settings.database_url)
    if args.command == "migrate":
        migrate(pool)
        pool.close()
        return
    if args.command == "verify-bundle":
        trace, events, blobs = inspect_bundle(args.source)
        verify_event_chain(trace, events)
        print(json.dumps({"trace_id": str(trace.id), "events": len(events), "blobs": len(blobs)}))
        pool.close()
        return
    repository = PostgresTraceRepository(pool)
    cas = LocalCAS(settings.storage_root)
    if args.command == "record-demo":
        recorder = Recorder(repository, cas)
        trace = recorder.start(args.source_run, framework="demo")
        recorder.record_model_call(
            trace.id,
            "planner",
            {"task": "add two numbers"},
            lambda: {"tool": "calculator", "arguments": {"left": 20, "right": 22}},
            model="demo-model-v1",
            cost_usd=0.002,
        )
        recorder.record_tool_call(
            trace.id,
            "calculator",
            {"left": 20, "right": 22},
            lambda: {"value": 42},
            tool_version="1",
        )
        recorder.finish(trace.id, status=TraceStatus.SUCCEEDED)
        print(json.dumps({"trace_id": str(trace.id)}))
    elif args.command == "inspect":
        trace = repository.get_trace(args.trace_id)
        events = repository.events(args.trace_id)
        verify_event_chain(trace, events)
        print(
            json.dumps(
                {
                    "trace": trace.model_dump(mode="json"),
                    "events": [event.model_dump(mode="json") for event in events],
                    "integrity": "verified",
                },
                indent=2,
            )
        )
    elif args.command == "replay":
        drift = args.drift

        def planner(_request, _context):
            return {
                "tool": "calculator",
                "arguments": {"left": 20, "right": 23 if drift else 22},
            }

        def calculator(request, _context):
            return {"value": request["left"] + request["right"]}

        engine = ReplayEngine(
            repository,
            cas,
            model_adapters={"planner": planner},
            tool_adapters={"calculator": calculator},
            result_store=PostgresReplayResultStore(pool),
        )
        print(engine.replay(args.trace_id, args.mode).model_dump_json(indent=2))
    elif args.command == "reduce-demo":
        package = ReductionPackage(inputs={f"field-{index}": index for index in range(args.items)})

        def oracle(candidate, _workspace):
            return "demo:failure" if "field-7" in candidate.inputs else None

        result = FailureReducer(cas, oracle, "demo:failure", stability_runs=1).reduce(package)
        print(result.model_dump_json(indent=2))
    elif args.command == "export":
        export_trace(args.trace_id, repository, cas, args.destination)
        print(json.dumps({"trace_id": str(args.trace_id), "destination": str(args.destination)}))
    pool.close()


if __name__ == "__main__":
    main()
