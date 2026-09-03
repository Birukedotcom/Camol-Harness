"""Command-line interface for planning, approving, running, and inspecting Camol."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .orchestrator import Orchestrator, StateTransitionError
from .runner import HarnessRunner, summary
from .doctor import DoctorOptions, run_doctor
from .runbook import RunbookError, load_runbook
from .schema import SchemaError
from .store import SQLiteEventStore


def _write_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _run_id(store: SQLiteEventStore, requested: str) -> str:
    run_id = requested or store.latest_run_id()
    if not run_id:
        raise StateTransitionError("the event store contains no run")
    return run_id


def command_validate(args: argparse.Namespace) -> int:
    runbook = load_runbook(Path(args.runbook))
    _write_json(
        {
            "valid": True,
            "run_id": runbook["run"]["id"],
            "max_concurrency": runbook["run"].get(
                "max_concurrency", runbook["run"].get("max_agents")
            ),
            "agents": [agent["id"] for agent in runbook["agents"]],
            "tasks": [task["id"] for task in runbook["tasks"]],
        }
    )
    return 0


def command_init(args: argparse.Namespace) -> int:
    runbook = load_runbook(Path(args.runbook))
    store = SQLiteEventStore(Path(args.db))
    try:
        state = Orchestrator(store).initialize(runbook)
        _write_json(summary(state))
    finally:
        store.close()
    return 0


def command_approve(args: argparse.Namespace) -> int:
    store = SQLiteEventStore(Path(args.db))
    try:
        orchestrator = Orchestrator(store)
        run_id = _run_id(store, args.run_id)
        state = orchestrator.state(run_id)
        orchestrator.approve_plan(run_id, args.by, state["plan_digest"])
        _write_json(summary(orchestrator.state(run_id)))
    finally:
        store.close()
    return 0


def command_status(args: argparse.Namespace) -> int:
    store = SQLiteEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        _write_json(summary(Orchestrator(store).state(run_id)))
    finally:
        store.close()
    return 0


def command_events(args: argparse.Namespace) -> int:
    store = SQLiteEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        _write_json(store.read(run_id, after_seq=args.after))
    finally:
        store.close()
    return 0


def command_run(args: argparse.Namespace) -> int:
    runbook = load_runbook(Path(args.runbook))
    store = SQLiteEventStore(Path(args.db))
    try:
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(runbook)
        run_id = state["run_id"]
        if state["status"] == "draft":
            if not args.approve_by:
                raise StateTransitionError(
                    "the plan is draft; run `python -m camol approve` or pass --approve-by"
                )
            orchestrator.approve_plan(run_id, args.approve_by, state["plan_digest"])
        final_state = asyncio.run(
            HarnessRunner(orchestrator, Path(args.workspace)).run_until_terminal(run_id)
        )
        _write_json(summary(final_state))
        return 0 if final_state["status"] == "completed" else 2
    finally:
        store.close()


def command_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        DoctorOptions(
            runbook=Path(args.runbook),
            workspace=Path(args.workspace),
            state_dir=Path(args.state_dir),
            json_output=args.json,
            now=args.now,
            receipt_ttl_seconds=args.receipt_ttl_seconds,
            min_free_bytes=args.min_free_bytes,
            services=tuple(args.require_service or ()),
            target_id=args.target_id,
        )
    )
    return report.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camol", description="Persistent plan-driven agent orchestration harness"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate an executable runbook")
    validate.add_argument("runbook")
    validate.set_defaults(handler=command_validate)

    initialize = subparsers.add_parser("init", help="freeze a draft runbook in the event store")
    initialize.add_argument("runbook")
    initialize.add_argument("--db", default=".camol/camol.sqlite3")
    initialize.set_defaults(handler=command_init)

    approve = subparsers.add_parser("approve", help="approve the exact frozen plan digest")
    approve.add_argument("--db", default=".camol/camol.sqlite3")
    approve.add_argument("--run-id")
    approve.add_argument("--by", required=True)
    approve.set_defaults(handler=command_approve)

    status = subparsers.add_parser("status", help="show the current replayed run projection")
    status.add_argument("--db", default=".camol/camol.sqlite3")
    status.add_argument("--run-id")
    status.set_defaults(handler=command_status)

    events = subparsers.add_parser("events", help="read the append-only event stream")
    events.add_argument("--db", default=".camol/camol.sqlite3")
    events.add_argument("--run-id")
    events.add_argument("--after", type=int, default=0)
    events.set_defaults(handler=command_events)

    run = subparsers.add_parser("run", help="run or resume the harness until terminal")
    run.add_argument("runbook")
    run.add_argument("--db", default=".camol/camol.sqlite3")
    run.add_argument("--workspace", default=".")
    run.add_argument("--approve-by")
    run.set_defaults(handler=command_run)

    doctor = subparsers.add_parser(
        "doctor",
        help="prove task-specific readiness with read-only probes; never prepares, launches, or spends",
    )
    doctor.add_argument("runbook")
    doctor.add_argument("--workspace", required=True, help="source repository root (read only)")
    doctor.add_argument("--state-dir", required=True, help="external state directory; must be outside the workspace")
    doctor.add_argument("--json", action="store_true", help="emit the full report as JSON")
    doctor.add_argument(
        "--now",
        help="ISO-8601 observation instant for deterministic fixtures; a receipt produced with a synthetic clock is not evidence (default: current UTC time)",
    )
    doctor.add_argument(
        "--receipt-ttl-seconds",
        type=int,
        help="receipt validity for schema v1 runbooks (default 300); v2 runbooks freeze this in run.readiness_policy and reject a conflicting value",
    )
    doctor.add_argument("--min-free-bytes", type=int, default=1 << 30, help="required free disk at the state directory")
    doctor.add_argument("--require-service", action="append", metavar="HOST:PORT", help="read-only TCP reachability check; repeatable")
    doctor.add_argument("--target-id", help="override the local target identity (default: local:<hostname>)")
    doctor.set_defaults(handler=command_doctor)
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (RunbookError, SchemaError, StateTransitionError, OSError, json.JSONDecodeError) as error:
        print("camol: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
