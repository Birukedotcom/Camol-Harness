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
from .runner import summary
from ._version import __version__
from .doctor import DoctorOptions, run_doctor
from .runbook import RunbookError, load_runbook
from .schema import SchemaError
from .store import SQLiteEventStore, ReadOnlyEventStore
from .debugger import Debugger, DebuggerError
from .usage import UsageError, usage_report
from .watchers import WatcherError, WatchSpec
from .watch_runtime import WatchRuntime, normalize_schedule
from .repository_graph import CrawlPolicy, GraphError, GraphStore, RepositoryGraph, crawl_repository, diff_snapshots
from .workspace import WorkspaceError, WorkspaceManager
from .providers import ProviderError, create_claude_capability, load_model_profile
from .probes import local_target_id
from .supervisor import Supervisor, SupervisorError, send_control, send_control_v2, spawn_supervisor
from .connections import ConnectionError
from .conversation import ConversationError
from .session import SessionError
from .app import InteractiveError
from .api import Harness
from .revisions import RevisionError, collect_revision_lineage
from .campaign import BenchmarkCampaign, CampaignStore, validate_campaign
from .schema import canonical_digest
from .capacity import CapacityBroker, CapacityError, validate_supply
from .models import DownloadPlan, ModelStore, ModelError
from .diagnostics import profile_run, event_metadata


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
    store = ReadOnlyEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        _write_json(summary(Orchestrator(store).state(run_id)))
    finally:
        store.close()
    return 0


def command_events(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        _write_json(store.read(run_id, after_seq=args.after))
    finally:
        store.close()
    return 0


def command_usage(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        _write_json(usage_report(store.read(_run_id(store, args.run_id))))
    finally:
        store.close()
    return 0


def command_profile(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        _write_json(profile_run(store.read(_run_id(store, args.run_id))))
    finally:
        store.close()
    return 0


def command_logs(args: argparse.Namespace) -> int:
    if args.after < 0:
        raise StateTransitionError("logs --after must be a non-negative cursor")
    store = ReadOnlyEventStore(Path(args.db))
    try:
        events = store.iter_events(_run_id(store, args.run_id), after_seq=args.after, limit=args.limit)
        for event in events:
            print(json.dumps(event_metadata(event), sort_keys=True, separators=(",", ":")))
    finally:
        store.close()
    return 0


def command_debug(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        debugger = Debugger(Orchestrator(store), run_id)
        _write_json(debugger.inbox() if args.inbox else debugger.inspect(args.case_id))
    finally:
        store.close()
    return 0


def command_export(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        run_id = _run_id(store, args.run_id)
        artifacts = ArtifactStore(Path(args.state_dir))
        manifest = RunArchive.export(run_id, store.read(run_id), artifacts, Path(args.output),
                                     lineage_events=collect_revision_lineage(store, run_id))
        _write_json(manifest)
    finally:
        store.close()
    return 0


def command_watchers(args: argparse.Namespace) -> int:
    store = ReadOnlyEventStore(Path(args.db))
    try:
        watches = WatchRuntime(Orchestrator(store), _run_id(store, args.run_id)).inspect()
        if args.watcher_id and args.watcher_id not in watches:
            raise WatcherError("unknown watcher")
        _write_json(watches[args.watcher_id] if args.watcher_id else watches)
    finally:
        store.close()
    return 0


def command_watch(args: argparse.Namespace) -> int:
    spec = WatchSpec.from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8"))) if args.spec else None
    schedule = normalize_schedule(json.loads(Path(args.schedule).read_text(encoding="utf-8"))) if args.schedule else None
    if args.watch_action == "validate":
        if spec is None and schedule is None:
            raise WatcherError("watch validate requires --spec or --schedule")
        _write_json(dict(spec=spec.to_dict() if spec else None, spec_digest=canonical_digest(spec.to_dict()) if spec else None,
                         schedule=schedule, schedule_digest=canonical_digest(schedule) if schedule else None))
        return 0
    if not args.state_dir:
        raise WatcherError("watch operations require --state-dir")
    if args.watch_action == "create":
        if spec is None or args.digest != canonical_digest(spec.to_dict()) or not args.by:
            raise WatcherError("watch create requires --spec, --by and its exact --digest")
        params = dict(spec=spec.to_dict(), approval_digest=args.digest, approved_by=args.by)
    elif args.watch_action == "schedule":
        if schedule is None or args.digest != canonical_digest(schedule) or not args.by:
            raise WatcherError("watch schedule requires --schedule, --by and its exact --digest")
        params = dict(schedule=schedule, approval_digest=args.digest, approved_by=args.by)
    elif args.watch_action in {"stop", "reopen"}:
        if not args.watcher_id or not args.by or not args.reason:
            raise WatcherError("watch stop/reopen requires --watcher-id, --by and --reason")
        params = dict(watcher_id=args.watcher_id, approved_by=args.by, reason=args.reason)
        if args.watch_action == "reopen":
            params["cursor"] = args.cursor
    else:
        params = {}
    if args.live:
        if args.watch_action in {"poll", "run"}:
            raise WatcherError("a live daemon already schedules approved watches; use inspect")
        response = asyncio.run(send_control_v2(Path(args.state_dir), "watch-" + args.watch_action, requested_by=args.by or "operator", params=params))
        _write_json(response.get("result", response))
        return 0
    if args.watch_action == "inspect":
        from .supervisor import SupervisorPaths
        store = ReadOnlyEventStore(SupervisorPaths.under(Path(args.state_dir)).database)
        try:
            _write_json(WatchRuntime(Orchestrator(store), _run_id(store, None)).inspect())
        finally:
            store.close()
        return 0
    with Harness(Path(args.workspace), Path(args.state_dir)) as harness:
        runtime = harness.observers()
        if args.watch_action == "create":
            result = harness.watch(spec, approved_by=args.by).inspect()
        elif args.watch_action == "schedule":
            result = runtime.configure(schedule, approved_by=args.by, approval_digest=args.digest)
        elif args.watch_action == "stop":
            result = runtime.stop(args.watcher_id, approved_by=args.by, reason=args.reason)
        elif args.watch_action == "reopen":
            result = harness.watcher(args.watcher_id).reopen(approved_by=args.by, reason=args.reason, cursor=args.cursor)
        else:
            result = asyncio.run(runtime.run_until_settled() if args.watch_action == "run" else runtime.tick())
        _write_json(result)
    return 0


def command_repo(args: argparse.Namespace) -> int:
    store = None
    try:
        if args.repo_action == "crawl":
            if args.selectors or args.snapshot:
                raise GraphError("crawl does not accept selectors or an existing snapshot")
            snapshot = crawl_repository(Path(args.workspace), policy=CrawlPolicy(max_files=args.max_files))
            if args.db:
                store = GraphStore(Path(args.db))
                store.save(snapshot)
            graph = RepositoryGraph(snapshot)
        elif args.db:
            store = GraphStore(Path(args.db), read_only=True)
            if args.repo_action == "list":
                _write_json(store.list_snapshots())
                return 0
            if args.repo_action == "diff":
                if len(args.selectors) != 2:
                    raise GraphError("repo diff requires two snapshot IDs")
                _write_json(diff_snapshots(store.load(args.selectors[0]), store.load(args.selectors[1])))
                return 0
            graph = RepositoryGraph(store.load(args.snapshot))
        else:
            if args.snapshot or args.repo_action in {"list", "diff"}:
                raise GraphError("snapshot/list/diff requires --db with a saved graph database")
            graph = RepositoryGraph(crawl_repository(Path(args.workspace), policy=CrawlPolicy(max_files=args.max_files)))
        count = {"impact": 1, "why": 2}.get(args.repo_action, 0)
        if len(args.selectors) != count:
            raise GraphError("repo {} requires {} node selectors".format(args.repo_action, count))
        if args.repo_action == "impact":
            _write_json(graph.impact(args.selectors[0]))
        elif args.repo_action == "why":
            _write_json(graph.why(*args.selectors))
        elif args.repo_action == "cycles":
            _write_json({"snapshot_id": graph.snapshot.snapshot_id, "cycles": graph.cycles()})
        elif args.repo_action == "layers":
            _write_json({"snapshot_id": graph.snapshot.snapshot_id, "layers": graph.layers()})
        else:
            print(graph.render_text() if args.format == "text" else graph.export(args.format))
        return 0
    finally:
        if store is not None:
            store.close()


def command_verify_export(args: argparse.Namespace) -> int:
    manifest, _ = RunArchive.verify(Path(args.archive))
    state = RunArchive.replay(Path(args.archive))
    _write_json({"valid": True, "manifest": manifest, "run": summary(state)})
    return 0


def command_run(args: argparse.Namespace) -> int:
    runbook = load_runbook(Path(args.runbook))
    workspace = Path(args.workspace)
    state_dir = Path(args.state_dir)
    # Foreground execution shares exactly the embedding/daemon owner lock.
    with Harness(workspace, state_dir, database=Path(args.db) if args.db else None) as harness:
        state = harness.prepare(runbook)
        if state["status"] == "draft":
            if not args.approve_by:
                raise StateTransitionError(
                    "the plan is draft; run `python -m camol approve` or pass --approve-by"
                )
            harness.approve(by=args.approve_by, digest=state["plan_digest"])
        final_state = harness.run()
        _write_json(summary(final_state))
        return 0 if final_state["status"] == "completed" else 2


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
    if args.control_command in {"acceptance", "accept", "gate-approve"}:
        params = {}
        if args.control_command != "acceptance":
            if not args.digest:
                raise SupervisorError("this approval requires the exact --digest shown by acceptance")
            params = {"approved_by": args.by}
            if args.control_command == "accept":
                params["outcome_digest"] = args.digest
            else:
                if not args.task_id:
                    raise SupervisorError("gate-approve requires --task-id")
                params.update(task_id=args.task_id, assessment_digest=args.digest)
        response = asyncio.run(send_control_v2(Path(args.state_dir), args.control_command,
                                              requested_by=args.by, params=params))
    else:
        response = asyncio.run(send_control(Path(args.state_dir), args.control_command, requested_by=args.by))
    _write_json(response.get("result", response))
    return 0


def command_bench_compare(args: argparse.Namespace) -> int:
    direct = BenchmarkTrial.from_dict(json.loads(Path(args.direct).read_text(encoding="utf-8")))
    camol = BenchmarkTrial.from_dict(json.loads(Path(args.camol).read_text(encoding="utf-8")))
    _write_json(compare_trials(direct, camol))
    return 0


def command_campaign(args: argparse.Namespace) -> int:
    if args.campaign_action == "validate":
        if not args.manifest:
            raise BenchmarkError("campaign validate requires --manifest")
        manifest = validate_campaign(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
        _write_json(dict(valid=True, manifest_digest=canonical_digest(manifest),
                         expected_trials=len(manifest["tasks"]) * len(manifest["arms"]) * manifest["repetitions"]))
        return 0
    if not args.db or not args.campaign_id:
        raise BenchmarkError("campaign inspection requires --db and --campaign-id")
    store = CampaignStore(Path(args.db), read_only=True)
    try:
        campaign = BenchmarkCampaign(store, args.campaign_id)
        _write_json(campaign.report() if args.campaign_action == "report" else campaign.state())
    finally:
        store.close()
    return 0


def command_swebench(args: argparse.Namespace) -> int:
    from .swebench import OfflineVerifiedDataset, freeze_offline_lock
    if args.swebench_action == "freeze":
        if not args.images or not args.environment or not args.dataset_revision:
            raise BenchmarkError("swebench freeze requires --images, --environment and a full --dataset-revision")
        lock = freeze_offline_lock(Path(args.dataset), Path(args.prepared), dataset_revision=args.dataset_revision,
                 images=json.loads(Path(args.images).read_text(encoding="utf-8")),
                 environment=json.loads(Path(args.environment).read_text(encoding="utf-8")))
        _write_json(dict(lock=lock, lock_digest=canonical_digest(lock), download_performed=False, grader_executed=False))
        return 0
    if not args.lock:
        raise BenchmarkError("swebench inspect requires --lock")
    lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
    dataset = OfflineVerifiedDataset(Path(args.dataset), Path(args.prepared), lock)
    identities = [item["instance_id"] for item in dataset.lock["instances"]]
    if args.task_id:
        if args.task_id not in identities:
            raise BenchmarkError("task is absent from the frozen cohort")
        identities = [args.task_id]
    _write_json(dict(lock_digest=canonical_digest(lock), harness_commit=lock["harness_commit"],
                     task_manifests=[dataset.task(identity) for identity in identities],
                     worker_inputs=[dataset.public_task(identity) for identity in identities],
                     grader_executed=False, oracle_bodies_excluded=True))
    return 0


def command_capacity(args: argparse.Namespace) -> int:
    supply = None
    if args.capacity_action == "publish":
        if not args.supply or not args.by or not args.digest:
            raise CapacityError("publish requires --supply, --by and the exact reviewed --digest")
        supply = validate_supply(json.loads(Path(args.supply).read_text(encoding="utf-8")))
        if supply["provenance"] != "owner_declared":
            raise CapacityError("a JSON file is owner-declared supply, not an executed observer receipt")
        if supply["namespace"] != args.namespace or supply["source"] != "owner/" + args.by:
            raise CapacityError("supply must name the exact namespace and source owner/BY")
        if canonical_digest(supply) != args.digest:
            raise CapacityError("supply approval has a stale digest")
    broker = CapacityBroker(Path(args.db), read_only=args.capacity_action != "publish")
    try:
        if args.capacity_action == "publish":
            _write_json(dict(supply_digest=broker.publish(supply), approved_by=args.by,
                             claim="owner-declared scheduling capacity, not measured hardware or provider quota"))
        elif args.capacity_action == "reservations":
            _write_json(broker.reservations(namespace=args.namespace))
        else:
            _write_json(broker.inventory(args.namespace))
    finally:
        broker.close()
    return 0


def command_models(args: argparse.Namespace) -> int:
    plan = DownloadPlan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8"))) if args.plan else None
    if args.model_action in {"validate", "prepare"} and plan is None:
        raise ModelError("model validate/prepare requires --plan")
    if args.model_action == "validate":
        _write_json(dict(plan=plan.to_dict(), plan_digest=plan.digest(), starts_download=False, loads_model=False))
        return 0
    if args.model_action not in {"list", "prepare"} and not args.digest:
        raise ModelError("this model operation requires the exact --digest")
    if args.model_action in {"approve", "download", "discard-partial"} and not args.by:
        raise ModelError("model approval/download requires --by naming the plan owner")
    if args.model_action == "discard-partial" and not args.confirm_discard:
        raise ModelError("discard-partial permanently removes this plan's unverified bytes; inspect status, then pass --confirm-discard")
    from .session import default_state_root
    root = Path(args.root).expanduser() if args.root else default_state_root() / "models"
    with ModelStore(root, read_only=args.model_action in {"list", "status", "events", "artifacts"}) as store:
        if args.model_action == "prepare":
            result = store.prepare(plan)
        elif args.model_action == "approve":
            result = store.approve(args.digest, args.by)
        elif args.model_action == "download":
            result = store.download(args.digest, args.by)
        elif args.model_action == "discard-partial":
            result = store.discard_partial(args.digest, args.by)
        elif args.model_action == "list":
            result = store.list()
        elif args.model_action == "events":
            result = store.events(args.digest)
        elif args.model_action == "artifacts":
            result = store.verified_artifacts(args.digest)
        else:
            result = store.status(args.digest, verify=args.verify)
    _write_json(result)
    return 0


def command_revise(args: argparse.Namespace) -> int:
    if args.revision_action == "show":
        from .supervisor import SupervisorPaths
        store = ReadOnlyEventStore(SupervisorPaths.under(Path(args.state_dir)).database)
        try:
            state = Orchestrator(store).state(_run_id(store, args.run_id))
            _write_json(dict(run_id=state["run_id"], status=state["status"], proposals=state.get("revision_proposals", {}),
                             revision=state.get("revision"), successor=state.get("successor")))
        finally:
            store.close()
        return 0
    with Harness(Path(args.workspace), Path(args.state_dir)) as harness:
        if args.run_id and args.run_id != harness.run_id:
            raise RevisionError("only the current execution owner may amend this state directory's run")
        if args.revision_action == "propose":
            if not args.runbook or not args.reason:
                raise RevisionError("revise propose requires --runbook and --reason")
            effects = json.loads(Path(args.effect_reruns).read_text(encoding="utf-8")) if args.effect_reruns else None
            _write_json(harness.propose_revision(Path(args.runbook), reason=args.reason, effect_reruns=effects))
        else:
            if not args.by or not args.digest:
                raise RevisionError("revise apply requires --by and the exact --digest shown by propose")
            _write_json(summary(harness.apply_revision(by=args.by, proposal_digest=args.digest)))
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
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
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

    usage = subparsers.add_parser("usage", help="inspect measured, reported and unknown usage by task, box and model")
    usage.add_argument("--db", required=True)
    usage.add_argument("--run-id")
    usage.add_argument("--json", action="store_true", help="JSON is the default machine-readable output")
    usage.set_defaults(handler=command_usage)

    debug = subparsers.add_parser("debug", help="inspect durable debug cases, experiments and regression evidence")
    debug.add_argument("--db", required=True)
    debug.add_argument("--run-id")
    debug_selection = debug.add_mutually_exclusive_group()
    debug_selection.add_argument("--case-id")
    debug_selection.add_argument("--inbox", action="store_true", help="show untriaged real evaluator counterexamples")
    debug.set_defaults(handler=command_debug)

    watchers = subparsers.add_parser("watchers", help="inspect durable observation cursors, readiness and terminality")
    watchers.add_argument("--db", required=True)
    watchers.add_argument("--run-id")
    watchers.add_argument("--watcher-id")
    watchers.set_defaults(handler=command_watchers)

    profile = subparsers.add_parser("profile", help="inspect content-free cost hotspots, retry/wait counts and lifecycle envelopes")
    profile.add_argument("--db", required=True)
    profile.add_argument("--run-id")
    profile.set_defaults(handler=command_profile)

    logs = subparsers.add_parser("logs", help="emit content-free JSONL event metadata for an external logger")
    logs.add_argument("--db", required=True)
    logs.add_argument("--run-id")
    logs.add_argument("--after", type=int, default=0)
    logs.add_argument("--limit", type=int, choices=range(1, 10001), default=1000, metavar="1..10000")
    logs.set_defaults(handler=command_logs)

    watch = subparsers.add_parser("watch", help="approve and schedule bounded, durable read-only observation")
    watch.add_argument("watch_action", choices=("validate", "create", "schedule", "inspect", "poll", "run", "stop", "reopen"))
    watch.add_argument("--state-dir")
    watch.add_argument("--workspace", default=".")
    watch.add_argument("--live", action="store_true", help="use the already-running authenticated local daemon")
    watch.add_argument("--spec", help="strict WatchSpec JSON")
    watch.add_argument("--schedule", help="exact source binding and polling authorization JSON")
    watch.add_argument("--watcher-id")
    watch.add_argument("--by")
    watch.add_argument("--digest")
    watch.add_argument("--reason")
    watch.add_argument("--cursor")
    watch.set_defaults(handler=command_watch)

    repo = subparsers.add_parser("repo", help="crawl and query an evidence-linked, static repository graph")
    repo.add_argument("repo_action", choices=("crawl", "show", "impact", "why", "cycles", "layers", "export", "list", "diff"))
    repo.add_argument("selectors", nargs="*")
    repo.add_argument("--workspace", default=".")
    repo.add_argument("--db", help="optional graph snapshot database; only crawl creates/writes it")
    repo.add_argument("--snapshot", help="saved snapshot ID; otherwise the latest is selected")
    repo.add_argument("--max-files", type=int, default=10000)
    repo.add_argument("--format", choices=("text", "json", "dot", "graphml"), default="text")
    repo.set_defaults(handler=command_repo)

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
    control.add_argument("control_command", choices=("ping", "status", "boxes", "approve", "drain", "resume", "stop", "force-stop", "acceptance", "accept", "gate-approve"))
    control.add_argument("--state-dir", required=True)
    control.add_argument("--by", default="operator")
    control.add_argument("--digest", help="exact outcome or gate assessment digest being approved")
    control.add_argument("--task-id", help="task whose human gate is being approved")
    control.set_defaults(handler=command_control)

    bench = subparsers.add_parser(
        "bench-compare", help="compare a direct-Claude and Camol trial under matched conditions"
    )
    bench.add_argument("--direct", required=True, help="claude_direct benchmark-trial JSON")
    bench.add_argument("--camol", required=True, help="camol_one or camol_adaptive benchmark-trial JSON")
    bench.set_defaults(handler=command_bench_compare)

    campaign = subparsers.add_parser("campaign", help="validate pinned benchmark cohorts or inspect a durable campaign")
    campaign.add_argument("campaign_action", choices=("validate", "status", "report"))
    campaign.add_argument("--manifest")
    campaign.add_argument("--db")
    campaign.add_argument("--campaign-id")
    campaign.set_defaults(handler=command_campaign)

    swebench = subparsers.add_parser("swebench", help="freeze or inspect pinned offline public-suite data; no grader, Docker or model launch")
    swebench.add_argument("swebench_action", choices=("freeze", "inspect"))
    swebench.add_argument("--dataset", required=True, help="protected local Verified JSON/JSONL")
    swebench.add_argument("--prepared", required=True, help="protected pinned official prepared tasks")
    swebench.add_argument("--dataset-revision", help="full dataset revision for a proposed lock")
    swebench.add_argument("--images", help="reviewed per-task immutable image pins JSON")
    swebench.add_argument("--environment", help="strict grader hardware/isolation JSON")
    swebench.add_argument("--lock", help="exact frozen offline suite lock JSON")
    swebench.add_argument("--task-id")
    swebench.set_defaults(handler=command_swebench)

    capacity = subparsers.add_parser("capacity", help="inspect shared capacity or publish exact owner-declared limits")
    capacity.add_argument("capacity_action", choices=("inventory", "reservations", "publish"))
    capacity.add_argument("--db", required=True, help="shared owner-only capacity database")
    capacity.add_argument("--namespace", required=True)
    capacity.add_argument("--supply", help="strict capacity-supply JSON")
    capacity.add_argument("--by")
    capacity.add_argument("--digest", help="exact canonical digest of the owner-reviewed supply")
    capacity.set_defaults(handler=command_capacity)

    models = subparsers.add_parser("models", help="plan, approve and verify explicit downloads; never implicitly load a model")
    models.add_argument("model_action", choices=("validate", "prepare", "approve", "download", "list", "status", "events", "artifacts", "discard-partial"))
    models.add_argument("--root", help="owner-only model store (default: Camol state home/models)")
    models.add_argument("--plan", help="immutable HTTPS/file-hash/size DownloadPlan JSON")
    models.add_argument("--digest", help="exact approved download plan digest")
    models.add_argument("--by")
    models.add_argument("--verify", action="store_true", help="rehash completed artifacts during status inspection")
    models.add_argument("--confirm-discard", action="store_true", help="authorize irrecoverable removal of this plan's unverified partial bytes only")
    models.set_defaults(handler=command_models)

    revise = subparsers.add_parser("revise", help="review or apply an immutable successor plan (execution must be stopped before apply)")
    revise.add_argument("revision_action", choices=("show", "propose", "apply"))
    revise.add_argument("--workspace", default=".")
    revise.add_argument("--state-dir", required=True)
    revise.add_argument("--run-id")
    revise.add_argument("--runbook", help="explicit schema V5 successor runbook")
    revise.add_argument("--reason")
    revise.add_argument("--effect-reruns", help="JSON containing exact owner-reviewed confirmed-effect reuse policies")
    revise.add_argument("--by")
    revise.add_argument("--digest", help="exact revision proposal digest being approved")
    revise.set_defaults(handler=command_revise)
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        ArtifactError, RunbookError, SchemaError, StateTransitionError, DebuggerError, UsageError, WatcherError, GraphError, RevisionError, CapacityError, ModelError,
        WorkspaceError, ProviderError, SupervisorError, BenchmarkError, OSError, json.JSONDecodeError,
        ConnectionError, ConversationError, SessionError, InteractiveError,
    ) as error:
        print("camol: {}".format(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("camol: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
