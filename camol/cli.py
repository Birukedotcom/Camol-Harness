"""Command-line interface for planning, approving, running, and inspecting Camol."""

import argparse
import asyncio
import json
import sqlite3
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
from .json_contracts import load_contract
from .ssh_protocol import ALL_COMMANDS as SSH_COMMANDS, MUTATING_COMMANDS as SSH_MUTATIONS, SSHTarget, SSHTransportError
from .source_binding import SourceBindingError
from .retention import RetentionError, RetentionPolicy, inspect_retention
from .overview import fleet_overview, render_overview
from .box_inspection import BoxInspector, BoxInspectionError


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


def command_retention(args: argparse.Namespace) -> int:
    # No latest-run lookup, live store, inferred policy, or cleanup authorization.
    policy = RetentionPolicy.from_dict(load_contract(args.policy, max_bytes=65536))
    inventory = inspect_retention(state_dir=Path(args.state_dir), database=Path(args.db),
                                  run_id=args.run_id, policy=policy)
    _write_json(dict(inventory=inventory.to_dict(), inventory_digest=inventory.digest()))
    return 0


def command_overview(args: argparse.Namespace) -> int:
    if args.offset < 0:
        raise StateTransitionError("overview offset must be nonnegative")
    store = None
    try:
        store = ReadOnlyEventStore(Path(args.db))
        state = Orchestrator(store).state(args.run_id)
        if state["run_id"] != args.run_id:
            raise StateTransitionError("overview requires an existing exact run")
        report = fleet_overview(state, attention=args.attention, offset=args.offset, limit=args.limit)
    except sqlite3.Error as error:
        raise StateTransitionError("overview ledger is unreadable; snapshot unavailable") from error
    finally:
        if store is not None:
            store.close()
    if args.json:
        _write_json(report)
    else:
        print(render_overview(report))
    return 0


def command_box(args: argparse.Namespace) -> int:
    inspector = BoxInspector(Path(args.state_dir), database=Path(args.db) if args.db else None)
    if args.box_command == "list":
        report = inspector.list(args.run_id)
    elif args.box_command == "resolve":
        report = inspector.resolve(args.run_id, args.box_id)
    else:
        report = inspector.read(args.run_id, args.box_id, after_seq=args.after, limit=args.limit,
                                tail=args.tail, previews=not args.no_previews)
    _write_json(report)
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
    spec = WatchSpec.from_dict(load_contract(args.spec)) if args.spec else None
    schedule = normalize_schedule(load_contract(args.schedule)) if args.schedule else None
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
        operation_id=args.operation_id,
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


def command_preflight_status(args: argparse.Namespace) -> int:
    from .preflight_journal import PreflightJournal, PreflightJournalError
    try:
        with PreflightJournal(Path(args.state_dir), read_only=True) as journal:
            rows = journal.inventory()
    except PreflightJournalError as error:
        raise ProviderError(str(error)) from error
    held = any(row["outcome"] is None or row["outcome"]["status"] == "unknown" for row in rows)
    _write_json({"schema": "camol.provider_preflight_inventory", "schema_version": 1,
                 "held": held, "requests": rows,
                 "known_cost_usd_micros": sum(row["outcome"]["usage"]["cost_usd_micros"] or 0
                                             for row in rows if row["outcome"] is not None),
                 "unresolved_reserved_usd_cents": sum(row["intent"]["max_usd_cents"] for row in rows
                                                     if row["outcome"] is None or row["outcome"]["usage"]["cost_usd_micros"] is None),
                 "coverage": "capability preflights only; excludes worker, planning and unrelated account costs"})
    return 2 if held else 0


def command_serve(args: argparse.Namespace) -> int:
    source_options = {}
    if getattr(args, "source_binding_digest", None) is not None:
        source_options["source_binding_digest"] = args.source_binding_digest
    asyncio.run(
        Supervisor(
            Path(args.runbook), Path(args.workspace), Path(args.state_dir),
            database=Path(args.db) if args.db else None, approve_by=args.approve_by,
            **source_options,
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
    direct = BenchmarkTrial.from_dict(load_contract(args.direct))
    camol = BenchmarkTrial.from_dict(load_contract(args.camol))
    _write_json(compare_trials(direct, camol))
    return 0


def command_campaign(args: argparse.Namespace) -> int:
    if args.campaign_action == "validate":
        if not args.manifest:
            raise BenchmarkError("campaign validate requires --manifest")
        manifest = validate_campaign(load_contract(args.manifest, max_bytes=8 << 20))
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
                 images=load_contract(args.images),
                 environment=load_contract(args.environment))
        _write_json(dict(lock=lock, lock_digest=canonical_digest(lock), download_performed=False, grader_executed=False))
        return 0
    if not args.lock:
        raise BenchmarkError("swebench inspect requires --lock")
    lock = load_contract(args.lock, max_bytes=8 << 20)
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
        supply = validate_supply(load_contract(args.supply))
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
    plan = DownloadPlan.from_dict(load_contract(args.plan)) if args.plan else None
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


def command_model_host(args: argparse.Namespace) -> int:
    from .model_host import LlamaCppModelHost, ModelHostPlan, ModelHostUnload
    from .session import default_state_root

    action = args.host_action
    plan = ModelHostPlan.from_dict(load_contract(args.plan)) if args.plan else None
    if action in {"validate", "prepare"} and plan is None:
        raise ModelError("model-host validate/prepare requires --plan")
    if action == "validate":
        _write_json(dict(plan=plan.to_dict(), plan_digest=plan.digest(), starts_process=False,
                         downloads_model=False, proves_inference=False))
        return 0
    if action not in {"prepare", "inventory", "unload"} and not args.plan_digest:
        raise ModelError("this model-host operation requires --plan-digest")
    if action in {"approve", "load", "unload"} and not args.by:
        raise ModelError("this model-host operation requires --by naming the exact owner")
    if action in {"load", "propose-unload"} and not args.operation_id:
        raise ModelError("load/propose-unload requires a unique --operation-id")
    if action == "unload" and (not args.request or not args.digest):
        raise ModelError("unload requires --request and the exact reviewed request --digest")
    request = ModelHostUnload.from_dict(load_contract(args.request)) if action == "unload" else None
    if args.live and action != "status":
        raise ModelError("--live is only valid with status; other inspections do not contact a model host")
    root = Path(args.root).expanduser() if args.root else default_state_root() / "model-hosts"
    with LlamaCppModelHost(root, read_only=action in {"status", "inventory", "events", "propose-unload"}) as host:
        if action == "prepare":
            result = host.prepare(plan)
        elif action == "approve":
            result = host.approve(args.plan_digest, args.by)
        elif action == "load":
            result = host.load(args.plan_digest, args.by, operation_id=args.operation_id)
        elif action == "inventory":
            result = host.inventory()
        elif action == "events":
            result = host.events(args.plan_digest)
        elif action == "propose-unload":
            proposal = host.propose_unload(args.plan_digest, args.operation_id)
            result = dict(request=proposal.to_dict(), request_digest=proposal.digest(), stops_process=False)
        elif action == "unload":
            result = host.unload(request, args.by, approve_digest=args.digest)
        else:
            result = host.status(args.plan_digest, live=args.live)
    _write_json(result)
    # A durable operation receipt is not success by itself. Scripts must not
    # treat an uncertain/failed allocation or stop as a completed transition.
    if action == "load":
        return 0 if result.get("status") == "loaded" and result.get("loaded") == "observed" else 2
    if action == "unload":
        return 0 if result.get("status") == "unloaded" and result.get("loaded") == "no" else 2
    return 0


def command_model_inference(args: argparse.Namespace) -> int:
    from .model_inference import ModelInference, ModelInferencePlan, prompt_file_identity, validate_prompt_file
    from .session import default_state_root

    action = args.inference_action
    allowed = {
        "plan": {"validate", "prepare"}, "prompt_file": {"fingerprint", "validate", "prepare", "infer"},
        "plan_digest": {"approve", "infer", "status", "events"}, "by": {"approve", "infer"},
        "show_response": {"infer"},
    }
    for field, actions in allowed.items():
        if getattr(args, field) and action not in actions:
            raise ModelError("--{} is not valid with model-inference {}".format(field.replace("_", "-"), action))
    if action in {"fingerprint", "infer"} and not args.prompt_file:
        raise ModelError("model-inference fingerprint/infer requires --prompt-file (never inline prompt text)")
    if action == "fingerprint":
        _write_json(dict(prompt_file_identity(args.prompt_file), sends_prompt=False, stores_prompt=False))
        return 0
    if action in {"validate", "prepare"} and not args.plan:
        raise ModelError("model-inference validate/prepare requires --plan")
    plan = ModelInferencePlan.from_dict(load_contract(args.plan)) if args.plan else None
    if plan is not None and args.prompt_file:
        validate_prompt_file(args.prompt_file, plan)
    if action == "validate":
        _write_json(dict(plan=plan.to_dict(), plan_digest=plan.digest(), sends_prompt=False,
                         stores_prompt=False, proves_inference=False))
        return 0
    if action in {"approve", "infer", "status", "events"} and not args.plan_digest:
        raise ModelError("this model-inference operation requires --plan-digest")
    if action in {"approve", "infer"} and not args.by:
        raise ModelError("this model-inference operation requires --by naming the exact owner")
    root = Path(args.root).expanduser() if args.root else default_state_root() / "model-hosts"
    with ModelInference(root, read_only=action in {"status", "inventory", "events"}) as inference:
        if action == "prepare":
            result = inference.prepare(plan)
        elif action == "approve":
            result = inference.approve(args.plan_digest, args.by)
        elif action == "infer":
            result = inference.infer(args.plan_digest, args.by, prompt_path=args.prompt_file)
            # Text is only available on the original successful invocation.
            # Metadata output is safe to collect by default; opting in can put
            # sensitive model text in terminal scrollback or caller logs.
            if not args.show_response:
                result = dict(result, response_text=None)
        elif action == "inventory":
            result = inference.inventory()
        elif action == "events":
            result = inference.events(args.plan_digest)
        else:
            result = inference.status(args.plan_digest)
    _write_json(result)
    if action == "infer":
        return 0 if result.get("status") == "completed" else 2
    return 0


def command_remote(args: argparse.Namespace) -> int:
    from .ssh_bridge import bridge_identity
    from .ssh_transport import SSHControlClient

    if args.remote_action == "identity":
        _write_json(dict(identity=bridge_identity(), provenance="local host self-report, not hardware attestation",
                         contacts_remote=False))
        return 0
    if not args.target:
        raise SSHTransportError("POLICY_DENIED", "remote operation requires --target")
    target = SSHTarget.from_dict(load_contract(args.target))
    if args.remote_action == "validate":
        _write_json(dict(target=target.to_dict(), profile_digest=target.digest(), contacts_remote=False,
                         proves_worker_readiness=False))
        return 0
    if not args.state_dir:
        raise SSHTransportError("POLICY_DENIED", "remote operation requires a private --state-dir for dispatch receipts")
    if args.remote_action == "request":
        if not args.remote_command:
            raise SSHTransportError("POLICY_DENIED", "remote request requires --command")
        if args.remote_command in SSH_MUTATIONS and (not args.allow_mutation or not args.by):
            raise SSHTransportError("POLICY_DENIED", "remote mutation requires explicit --allow-mutation and --by; profile and remote policy must also permit it")
    if args.remote_action == "acknowledge-unknown" and (not args.request_id or not args.by or not args.reason):
        raise SSHTransportError("POLICY_DENIED", "acknowledge-unknown requires --request-id, --by and --reason; it does not establish success")
    params = load_contract(args.params) if args.params else {}
    client = SSHControlClient(target, state_dir=Path(args.state_dir), ssh_binary=args.ssh_binary,
                              timeout=args.timeout, read_only=args.remote_action == "receipts")
    if args.remote_action == "receipts":
        result = client.receipts()
    elif args.remote_action == "acknowledge-unknown":
        result = client.acknowledge_unknown(args.request_id, requested_by=args.by, note=args.reason)
    else:
        result = asyncio.run(client.request(args.remote_command, params=params, requested_by=args.by))
    _write_json(result)
    return 2 if isinstance(result, dict) and result.get("ok") is False else 0


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
            effects = load_contract(args.effect_reruns) if args.effect_reruns else None
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

    overview = subparsers.add_parser("overview", help="inspect exact run boxes, task dependencies and attention without starting work")
    overview.add_argument("--db", required=True)
    overview.add_argument("--run-id", required=True)
    overview.add_argument("--attention", action="store_true")
    overview.add_argument("--offset", type=int, default=0)
    overview.add_argument("--limit", type=int, choices=range(1, 201), default=50, metavar="1..200")
    overview.add_argument("--json", action="store_true")
    overview.set_defaults(handler=command_overview)

    box = subparsers.add_parser("box", help="read exact run/box snapshots without a supervisor or model call")
    box_commands = box.add_subparsers(dest="box_command", required=True)
    for name in ("list", "resolve", "read"):
        operation = box_commands.add_parser(name)
        operation.add_argument("--state-dir", required=True)
        operation.add_argument("--db", help="existing database inside state-dir; default camol.sqlite3")
        operation.add_argument("--run-id", required=True)
        if name != "list":
            operation.add_argument("box_id", help="exact box ID; not a pane index or prefix")
        if name == "read":
            operation.add_argument("--after", type=int, default=0)
            operation.add_argument("--limit", type=int, choices=range(1, 1001), default=200, metavar="1..1000")
            operation.add_argument("--tail", action="store_true")
            operation.add_argument("--no-previews", action="store_true")
        operation.set_defaults(handler=command_box)

    retention = subparsers.add_parser("retention", help="inspect bounded cold-state references; never archive or delete")
    retention.add_argument("retention_action", choices=("inspect",))
    retention.add_argument("--state-dir", required=True, help="existing absolute canonical state directory")
    retention.add_argument("--db", required=True, help="existing absolute database path inside the state directory")
    retention.add_argument("--run-id", required=True, help="exact frozen run; no latest-run fallback")
    retention.add_argument("--policy", required=True, help="strict owner-matched inspection policy JSON")
    retention.set_defaults(handler=command_retention)

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
    preflight.add_argument("--operation-id", help="explicit one-shot probe identity; repeating it cannot spend again")
    preflight.add_argument(
        "--accept-spend", action="store_true",
        help="authorize this one no-tools model request up to the profile's max_turn_usd_cents",
    )
    preflight.add_argument(
        "--max-usd-cents", type=int, default=10,
        help="maximum spend for this preflight (default 10; also capped by the profile)",
    )
    preflight.set_defaults(handler=command_provider_preflight)
    preflight_status = subparsers.add_parser("preflight-status", help="read capability-probe usage and uncertain holds without spending")
    preflight_status.add_argument("--state-dir", required=True, help="existing external Camol state directory")
    preflight_status.set_defaults(handler=command_preflight_status)

    serve = subparsers.add_parser("serve", help="run the authoritative supervisor in the foreground")
    serve.add_argument("runbook")
    serve.add_argument("--db")
    serve.add_argument("--workspace", default=".")
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--approve-by")
    serve.add_argument("--source-binding-digest", help=argparse.SUPPRESS)
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

    model_host = subparsers.add_parser("model-host", help="explicitly prepare, approve and control a finite, owned local model process")
    model_host.add_argument("host_action", choices=("validate", "prepare", "approve", "load", "status", "inventory", "events", "propose-unload", "unload"))
    model_host.add_argument("--root", help="private host lifecycle state (default: Camol state home/model-hosts)")
    model_host.add_argument("--plan", help="pinned local llama.cpp executable/model/authority contract JSON")
    model_host.add_argument("--plan-digest", help="exact host plan subject, distinct from an unload request digest")
    model_host.add_argument("--operation-id", help="explicit one-shot load or unload-request identity")
    model_host.add_argument("--by", help="exact plan owner authorizing an operation")
    model_host.add_argument("--request", help="exact frozen unload request JSON")
    model_host.add_argument("--digest", help="canonical unload request digest approved by its owner")
    model_host.add_argument("--live", action="store_true", help="explicitly read the exact owned endpoint during status; not inference")
    model_host.set_defaults(handler=command_model_host)

    inference = subparsers.add_parser("model-inference", help="explicitly approve one bounded prompt to an already owned model load; no automatic retry")
    inference.add_argument("inference_action", choices=("fingerprint", "validate", "prepare", "approve", "infer", "status", "inventory", "events"))
    inference.add_argument("--root", help="existing private model-host lifecycle state (default: Camol state home/model-hosts)")
    inference.add_argument("--plan", help="exact inference contract binding host/load, owner, prompt bytes, token request and deadline")
    inference.add_argument("--plan-digest", help="exact inference plan digest; not the host plan digest")
    inference.add_argument("--prompt-file", help="bounded regular UTF-8 prompt file; text is never placed in argv or the ledger")
    inference.add_argument("--by", help="exact plan owner approving or dispatching the one-shot request")
    inference.add_argument("--show-response", action="store_true", help="include sensitive response text in this successful invocation's output; never retained for replay")
    inference.set_defaults(handler=command_model_inference)

    remote = subparsers.add_parser("remote", help="explicit authenticated SSH control-plane connection; never worker provisioning")
    remote.add_argument("remote_action", choices=("validate", "identity", "request", "receipts", "acknowledge-unknown"))
    remote.add_argument("--target", help="strict pinned SSH target profile JSON")
    remote.add_argument("--state-dir", help="private local dispatch journal, separate from the remote run state")
    remote.add_argument("--command", dest="remote_command", choices=sorted(SSH_COMMANDS))
    remote.add_argument("--params", help="strict JSON control parameters; never shell text")
    remote.add_argument("--by", help="exact target owner")
    remote.add_argument("--allow-mutation", action="store_true", help="explicitly opt in to the selected remote control mutation")
    remote.add_argument("--request-id", help="exact unresolved dispatch to acknowledge without retry")
    remote.add_argument("--reason", help="owner acknowledgment reason; not proof that a remote effect succeeded")
    remote.add_argument("--ssh-binary", default="/usr/bin/ssh", help="explicit local OpenSSH executable")
    remote.add_argument("--timeout", type=float, default=45)
    remote.set_defaults(handler=command_remote)

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
    except SSHTransportError as error:
        _write_json(dict(ok=False, error=dict(code=error.code, message=str(error)),
                         request_id=error.request_id, outcome=error.outcome))
        return 2
    except (
        ArtifactError, RunbookError, SchemaError, StateTransitionError, SourceBindingError, DebuggerError, UsageError, WatcherError, GraphError, RevisionError, CapacityError, ModelError,
        WorkspaceError, ProviderError, SupervisorError, BenchmarkError, OSError, json.JSONDecodeError,
        ConnectionError, ConversationError, SessionError, InteractiveError, RetentionError, BoxInspectionError,
    ) as error:
        print("camol: {}".format(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("camol: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
