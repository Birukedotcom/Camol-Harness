"""Command-line interface for planning, approving, running, and inspecting Camol."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .orchestrator import Orchestrator, StateTransitionError
from .runner import HarnessRunner, summary
from .runbook import RunbookError, load_runbook
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
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (RunbookError, StateTransitionError, OSError, json.JSONDecodeError) as error:
        print("camol: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
