"""Command-line interface for planning, approving, running, and inspecting Camol."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .orchestrator import Orchestrator, StateTransitionError
from .artifacts import ArtifactError, ArtifactStore, RunArchive
from .benchmark import BenchmarkError, BenchmarkTrial, compare_trials
from .runner import HarnessRunner, summary
from .doctor import DoctorOptions, run_doctor
from .runbook import RunbookError, load_runbook
from .schema import SchemaError
from .store import SQLiteEventStore
from .workspace import WorkspaceError, WorkspaceManager
from .providers import ProviderError, create_claude_capability, load_model_profile
from .probes import local_target_id
from .supervisor import Supervisor, SupervisorError, send_control, spawn_supervisor
from .connections import ConnectionError
from .conversation import ConversationError
from .session import SessionError
from .app import InteractiveError


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


def command_export(args: argparse.Namespace) -> int:
    store = SQLiteEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        artifacts = ArtifactStore(Path(args.state_dir))
        manifest = RunArchive.export(run_id, store.read(run_id), artifacts, Path(args.output))
        _write_json(manifest)
    finally:
        store.close()
    return 0


def command_verify_export(args: argparse.Namespace) -> int:
    manifest, _ = RunArchive.verify(Path(args.archive))
    state = RunArchive.replay(Path(args.archive))
    _write_json({"valid": True, "manifest": manifest, "run": summary(state)})
    return 0


def command_run(args: argparse.Namespace) -> int:
    runbook = load_runbook(Path(args.runbook))
    workspace = Path(args.workspace)
    state_dir = Path(args.state_dir)
    # Validate containment before creating any state path.
    WorkspaceManager(workspace, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteEventStore(Path(args.db) if args.db else state_dir / "camol.sqlite3")
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
            HarnessRunner(orchestrator, workspace, state_dir=state_dir).run_until_terminal(run_id)
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


def command_provider_preflight(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    state_dir = Path(args.state_dir).resolve()
    WorkspaceManager(workspace, state_dir)
    profile = load_model_profile(workspace, args.profile)
    receipt = create_claude_capability(
        profile,
        target_id=args.target_id or local_target_id(),
        state_dir=state_dir,
        cwd=workspace,
        accept_spend=args.accept_spend,
        spend_ceiling_cents=args.max_usd_cents,
    )
    _write_json({
        "ready": True,
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest(),
        "resolved_model": receipt.resolved_model,
        "receipt_digest": receipt.digest(),
        "expires_at": receipt.expires_at,
        "cost_usd_micros": receipt.cost_usd_micros,
    })
    return 0


def command_serve(args: argparse.Namespace) -> int:
    asyncio.run(
        Supervisor(
            Path(args.runbook), Path(args.workspace), Path(args.state_dir),
            database=Path(args.db) if args.db else None, approve_by=args.approve_by,
        ).serve()
    )
    return 0


def command_start(args: argparse.Namespace) -> int:
    _write_json(spawn_supervisor(
        Path(args.runbook), Path(args.workspace), Path(args.state_dir),
        database=Path(args.db) if args.db else None,
        approve_by=args.approve_by,
    ))
    return 0


def command_control(args: argparse.Namespace) -> int:
    response = asyncio.run(send_control(Path(args.state_dir), args.control_command, requested_by=args.by))
    _write_json(response.get("result", response))
    return 0


def command_bench_compare(args: argparse.Namespace) -> int:
    direct = BenchmarkTrial.from_dict(json.loads(Path(args.direct).read_text(encoding="utf-8")))
    camol = BenchmarkTrial.from_dict(json.loads(Path(args.camol).read_text(encoding="utf-8")))
    _write_json(compare_trials(direct, camol))
    return 0


def command_interactive(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    state_root = Path(args.state_home).expanduser().resolve() if args.state_home else None
    if not args.no_tui:
        try:
            from .tui import run_tui
        except ModuleNotFoundError as error:
            if not (error.name or "").startswith("textual"):
                raise
        else:
            return run_tui(workspace, state_root=state_root, show_boot=not args.no_boot)
    from .line_ui import run_line_ui
    return run_line_ui(workspace, state_root=state_root, show_boot=not args.no_boot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camol", description="Persistent plan-driven agent orchestration harness"
    )
    parser.add_argument("--workspace", default=".", help="workspace for the interactive client")
    parser.add_argument("--state-home", help="override the private interactive state root")
    parser.add_argument("--no-tui", action="store_true", help="use dependency-light line mode")
    parser.add_argument("--no-boot", action="store_true", help="skip branded boot art")
    parser.set_defaults(handler=command_interactive)
    subparsers = parser.add_subparsers(dest="command", required=False)

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

    export = subparsers.add_parser("export", help="export a replayable run ledger and its artifacts")
    export.add_argument("--db", required=True)
    export.add_argument("--state-dir", required=True)
    export.add_argument("--run-id")
    export.add_argument("--output", required=True)
    export.set_defaults(handler=command_export)

    verify_export = subparsers.add_parser("verify-export", help="verify and replay an exported run")
    verify_export.add_argument("archive")
    verify_export.set_defaults(handler=command_verify_export)

    run = subparsers.add_parser("run", help="run or resume the harness until terminal")
    run.add_argument("runbook")
    run.add_argument("--db", help="event database (default: STATE_DIR/camol.sqlite3)")
    run.add_argument("--workspace", default=".", help="clean source repository root")
    run.add_argument("--state-dir", required=True, help="external Camol state and isolated-worktree root")
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

    preflight = subparsers.add_parser(
        "provider-preflight",
        help="explicitly spend up to a frozen ceiling to prove provider/model capability",
    )
    preflight.add_argument("--profile", required=True, help="workspace-relative versioned model profile")
    preflight.add_argument("--workspace", required=True, help="clean source repository root")
    preflight.add_argument("--state-dir", required=True, help="external Camol state directory")
    preflight.add_argument("--target-id", help="target identity (default: local host)")
    preflight.add_argument(
        "--accept-spend", action="store_true",
        help="authorize this one no-tools model request up to the profile's max_turn_usd_cents",
    )
    preflight.add_argument(
        "--max-usd-cents", type=int, default=10,
        help="maximum spend for this preflight (default 10; also capped by the profile)",
    )
    preflight.set_defaults(handler=command_provider_preflight)

    serve = subparsers.add_parser("serve", help="run the authoritative supervisor in the foreground")
    serve.add_argument("runbook")
    serve.add_argument("--db")
    serve.add_argument("--workspace", default=".")
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--approve-by")
    serve.set_defaults(handler=command_serve)

    start = subparsers.add_parser("start", help="start a detached supervisor and return to the shell")
    start.add_argument("runbook")
    start.add_argument("--db")
    start.add_argument("--workspace", default=".")
    start.add_argument("--state-dir", required=True)
    start.add_argument("--approve-by")
    start.set_defaults(handler=command_start)

    control = subparsers.add_parser("ctl", help="inspect or control a detached supervisor")
    control.add_argument("control_command", choices=("ping", "status", "boxes", "approve", "drain", "resume", "stop", "force-stop"))
    control.add_argument("--state-dir", required=True)
    control.add_argument("--by", default="operator")
    control.set_defaults(handler=command_control)

    bench = subparsers.add_parser(
        "bench-compare", help="compare a direct-Claude and Camol trial under matched conditions"
    )
    bench.add_argument("--direct", required=True, help="claude_direct benchmark-trial JSON")
    bench.add_argument("--camol", required=True, help="camol_one or camol_adaptive benchmark-trial JSON")
    bench.set_defaults(handler=command_bench_compare)
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        ArtifactError, RunbookError, SchemaError, StateTransitionError,
        WorkspaceError, ProviderError, SupervisorError, BenchmarkError, OSError, json.JSONDecodeError,
        ConnectionError, ConversationError, SessionError, InteractiveError,
    ) as error:
        print("camol: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
