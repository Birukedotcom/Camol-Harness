"""Product-V0 command engine shared by the Textual and line-mode clients."""

import asyncio
import getpass
import hashlib
import json
import shlex
import sqlite3
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from .connections import ConnectionError, ConnectionRegistry
from .conversation import ConversationCancelled, ConversationError, converse, parse_selection, planning_history, require_tool_free_provider
from .planning import (
    GrillState,
    PlanningError,
    compile_runbook,
    effective_resource_limits,
    proposal_from_grill,
    reject_sensitive_text,
    validate_proposal,
)
from .probes import local_target_id
from .providers import (
    ProviderError,
    create_claude_capability,
    load_model_profile,
    model_profile_for_adapter,
)
from .runbook import RunbookError, load_runbook, runbook_digest, validate_runbook
from .schema import canonical_digest
from .session import EFFORTS, SessionError, SessionStore, default_state_root
from .store import ReadOnlyEventStore
from .supervisor import SupervisorError, SupervisorPaths, send_control_v2, spawn_supervisor
from .workspace import WorkspaceError, WorkspaceManager


class InteractiveError(RuntimeError):
    """A user-facing interactive transition was denied."""


@dataclass(frozen=True)
class CommandResponse:
    messages: Tuple[str, ...] = ()
    exit_client: bool = False
    login_argv: Optional[Tuple[str, ...]] = None
    login_provider: Optional[str] = None
    login_choices: Tuple[str, ...] = ()
    clear_transcript: bool = False
    box_id: Optional[str] = None
    box_view: Optional[str] = None


@dataclass(frozen=True)
class SlashCommand:
    command: str
    description: str
    takes_value: bool = False
    run_from_palette: bool = False


SLASH_COMMANDS = (
    SlashCommand("/login", "Connect or reconnect Claude/Codex", run_from_palette=True),
    SlashCommand("/skills", "Show built-in Camol protocols", run_from_palette=True),
    SlashCommand("/help", "Show command reference", run_from_palette=True),
    SlashCommand("/connections", "Refresh provider connection status", run_from_palette=True),
    SlashCommand("/model", "Choose the planning model", takes_value=True),
    SlashCommand("/effort", "Set provider reasoning effort", takes_value=True),
    SlashCommand("/grill", "Turn a goal into a gated plan", takes_value=True),
    SlashCommand("/propose", "One model proposal inside a reviewed seed", takes_value=True),
    SlashCommand("/import", "Review an existing executable runbook", takes_value=True),
    SlashCommand("/plan", "Inspect the exact candidate plan", run_from_palette=True),
    SlashCommand("/approve", "Confirm the exact visible plan", takes_value=True),
    SlashCommand("/accept", "Review or accept the exact final outcome", takes_value=True),
    SlashCommand("/gate", "Review or approve a waiting state gate", takes_value=True),
    SlashCommand("/run", "Start an approved ready run", takes_value=True),
    SlashCommand("/status", "Inspect session and supervisor", run_from_palette=True),
    SlashCommand("/boxes", "List the N-box worker pool", run_from_palette=True),
    SlashCommand("/box", "Open one read-only box view", takes_value=True),
    SlashCommand("/events", "Read new durable run events", run_from_palette=True),
    SlashCommand("/history", "Show retained conversation history", run_from_palette=True),
    SlashCommand("/usage", "Inspect recorded planning call usage", run_from_palette=True),
    SlashCommand("/debug", "Inspect durable debugger cases", run_from_palette=True),
    SlashCommand("/models", "Inspect passive local model artifacts", run_from_palette=True),
    SlashCommand("/watch", "Inspect recorded watcher state", run_from_palette=True),
    SlashCommand("/repo", "Inspect static repository relationships", run_from_palette=True),
    SlashCommand("/cancel", "Cancel the active planning request", run_from_palette=True),
    SlashCommand("/clear", "Clear only this terminal view", run_from_palette=True),
    SlashCommand("/btw", "Attach durable out-of-band context", takes_value=True),
    SlashCommand("/drain", "Pause new task admission"),
    SlashCommand("/resume", "Resume a waiting supervisor"),
    SlashCommand("/stop", "Drain and stop the supervisor"),
    SlashCommand("/quit", "Detach this terminal client"),
)


BUILTIN_PROTOCOLS = """BUILT-IN PROTOCOLS
  ■ grill       goal → constraints → invariants → task graph → evaluator → limits
  ■ debugger    observed behavior → target behavior → counterexample → regression eval
  ■ evidence    commands, tools, transcripts, diffs, usage, claims, and verification
  ■ refine      bounded retry after a red evaluator; completion still requires green proof
  ■ readiness   plan, workspace, authority, capacity, provider, evaluator, and freshness gates

These are durable Camol protocols, not hidden provider prompts. Provider-native
skills and slash commands are not imported into the planning-only orchestrator."""


HELP = """Commands
  /grill GOAL              question and freeze a candidate plan
  /propose --from SEED.json GOAL
                            one disclosed no-tools invocation; unapproved V5/V6 seed refinement
  /import PATH             import an exact runbook, bound to this checkout revision
  /plan                    show the full candidate plan and exact digest
  /approve yes|DIGEST      approve only that visible plan
  /accept [DIGEST]         review final outcome, then accept its exact digest
  /gate TASK [DIGEST]      review a pending state gate, then approve its digest
  /run --accept-spend --worker-cents N
                            acknowledge the frozen worker ceiling, prove readiness, and detach
  /run --accept-provider-policy DIGEST [--accept-spend]
                            accept an imported Codex tier's explicit weaker guarantees
  /status                  current local session and supervisor state
  /boxes                   list the arbitrary-N worker pool
  /box ID|NUMBER           inspect one box's tasks, commands, evidence, and events
  /box ID VIEW             status | context | tools | diff | evals | events | evidence | transcript
  /model SELECTION         manual | claude[:MODEL] | codex[:MODEL] | local:MODEL
  /effort LEVEL            low | medium | high | xhigh | max
  /login [claude|codex]    choose an account with arrows, or name it directly
  /connections             read-only connection discovery (not task readiness)
  /skills                  show built-in planning/debug/evidence protocols
  /history                 show retained conversation history
  /usage [run]             planning usage or the current run's accounting report
  /debug [list|inbox|show ID] inspect debugger cases or rejected-candidate inbox
  /models [list|status DIGEST] inspect passive downloads; never downloads or loads
  /watch [list|show ID]     inspect recorded watchers; never polls a remote source
  /repo [summary|impact PATH|why FROM TO|cycles]
                            inspect static repository declarations, not runtime readiness
  /cancel                  cancel the current planning request
  /clear                   clear this terminal view; durable state is preserved
  /btw NOTE                durable out-of-band note; never mutates a frozen plan
  /events                  show new append-only events since this client cursor
  /drain | /resume         pause admission or resume a waiting supervisor
  /stop                    drain and stop the supervisor (work is preserved)
  /help                    show this list
  /quit                    detach this client; work keeps running

Normal text talks to the selected planning-only orchestrator. Manual mode makes no
model calls; use /grill directly. No worker receives a lease before approval and
the kernel's exact readiness predicate."""


def _envelope(proposal: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = validate_proposal(proposal)
    selection = parse_selection(proposal["execution"]["model"])
    run_id = "run-" + uuid4().hex[:16]
    runbook = None
    execution_status = "planning_only"
    limitation = "Selected orchestrator cannot execute through the V0 worker kernel."
    if selection.provider == "claude" and selection.model in {None, "fable"}:
        base_profile = load_model_profile(Path.cwd(), "@camol/claude-fable-5-1")
        limits = effective_resource_limits(proposal)
        requested_cost = limits["max_worker_cost_usd_cents"]
        if requested_cost > base_profile.max_run_usd_cents:
            raise PlanningError(
                "cost_cents exceeds the packaged profile maximum of {}".format(base_profile.max_run_usd_cents)
            )
        effective_profile = replace(
            base_profile,
            effort=proposal["execution"]["effort"],
            max_agent_turns=limits["max_turns_per_task"],
            max_turn_tokens=min(base_profile.max_turn_tokens, limits["max_total_tokens"], 8_000),
            max_turn_usd_cents=min(base_profile.max_turn_usd_cents, requested_cost),
            max_task_usd_cents=min(base_profile.max_task_usd_cents, requested_cost),
            max_run_usd_cents=requested_cost,
        )
        runbook = compile_runbook(
            proposal,
            run_id=run_id,
            adapter={
                "kind": "claude_cli",
                "profile": "@camol/claude-fable-5-1",
                "profile_snapshot": effective_profile.to_dict(),
                "timeout_seconds": limits["turn_timeout_seconds"],
            },
        )
        execution_status = "preflight_required"
        limitation = "The requested fable alias is speculative until an explicit spend-capped preflight resolves it."
    elif selection.provider == "manual":
        limitation = "Manual mode is planning-only and cannot produce live worker evidence. Select claude:fable and re-run /grill to execute."
    elif selection.provider == "codex":
        limitation = "This grill produces no Codex worker contract. Use /import with an explicitly reviewed V5+ runbook and embedded profile_snapshot."
    elif selection.provider == "local":
        limitation = "This grill produces no local worker contract. Use /import with an explicitly reviewed V5+ Codex OSS runbook and embedded profile_snapshot."
    elif selection.provider == "openai":
        limitation = "The OpenAI key reference is discoverable, but direct Platform execution is not enabled in V0."
    return {
        "schema": "camol.product_plan",
        "schema_version": 1,
        "proposal": proposal,
        "run_id": run_id,
        "execution_status": execution_status,
        "execution_limitation": limitation,
        "runbook": runbook,
    }


def validate_envelope(value: Mapping[str, Any]) -> Dict[str, Any]:
    fields = {
        "schema", "schema_version", "proposal", "run_id", "execution_status",
        "execution_limitation", "runbook",
    }
    if isinstance(value, dict) and value.get("schema_version") in {2, 3}:
        fields.add("source")
        if value["schema_version"] == 3:
            fields.add("origin")
    if not isinstance(value, dict) or set(value) != fields:
        raise InteractiveError("product plan has the wrong fields")
    if value["schema"] != "camol.product_plan" or type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2, 3}:
        raise InteractiveError("product plan schema is unsupported")
    proposal = validate_proposal(value["proposal"]) if value["proposal"] is not None else None
    if proposal is None and (value["schema_version"] not in {2, 3} or value["runbook"] is None):
        raise InteractiveError("only an imported executable runbook may omit a proposal")
    if value["schema_version"] == 2:
        source = value["source"]
        if not isinstance(source, dict) or set(source) != {"workspace", "revision"}:
            raise InteractiveError("imported plan requires exact workspace and revision")
        if not all(isinstance(source[name], str) and source[name] for name in source):
            raise InteractiveError("imported source identity must be non-empty")
        if not Path(source["workspace"]).is_absolute():
            raise InteractiveError("imported workspace must be absolute")
    if value["schema_version"] == 3:
        from .debug_execution import validate_source
        from .schema import require_digest, require_identifier
        validate_source(value["source"])
        origin = value["origin"]
        if not isinstance(origin, dict) or set(origin) != {"kind", "seed_path", "seed_digest", "seed_bytes_digest", "request_digest", "response_digest", "planning_call_id"}:
            raise InteractiveError("model proposal origin has missing or unknown fields")
        if origin["kind"] != "seed_assisted_model_proposal" or not isinstance(origin["seed_path"], str) or not Path(origin["seed_path"]).is_absolute():
            raise InteractiveError("model proposal origin is invalid")
        for name in ("seed_digest", "seed_bytes_digest", "request_digest", "response_digest"):
            require_digest(origin[name], name)
        require_identifier(origin["planning_call_id"], "planning_call_id")
    if not isinstance(value["run_id"], str) or not value["run_id"]:
        raise InteractiveError("product plan run_id is required")
    if value["execution_status"] not in {"planning_only", "preflight_required", "ready"}:
        raise InteractiveError("product plan execution status is invalid")
    if not isinstance(value["execution_limitation"], str) or not value["execution_limitation"]:
        raise InteractiveError("product plan limitation is required")
    runbook = validate_runbook(value["runbook"]) if value["runbook"] is not None else None
    if runbook is not None and runbook["run"]["id"] != value["run_id"]:
        raise InteractiveError("product plan runbook has a different run id")
    normalized = dict(value)
    normalized["proposal"] = proposal
    normalized["runbook"] = runbook
    return normalized


def render_plan(plan: Mapping[str, Any], digest: str) -> str:
    plan = validate_envelope(plan)
    proposal = plan["proposal"]
    if proposal is None:
        runbook = plan["runbook"]
        return "\n".join([
            "IMPORTED PLAN {}".format(digest),
            "goal: " + runbook["run"]["objective"],
            "workspace: " + plan["source"]["workspace"],
            "frozen source revision: " + plan["source"]["revision"],
            "kernel runbook digest: " + runbook_digest(runbook),
            "This exact imported contract defines agents, tasks, commands, verification, authority and budgets.",
            "limitation: " + plan["execution_limitation"],
            *( ["Model-generated candidate: all state gates remain human; origin=" + json.dumps(plan["origin"], sort_keys=True)] if plan["schema_version"] == 3 else [] ),
            "canonical product plan JSON:", json.dumps(plan, indent=2, sort_keys=True),
            ("Nothing has started. Review every command, grant and mapping, then /approve " + digest
             if plan["schema_version"] == 3 else
             "Nothing has started. Review every command and grant, then /approve yes or the exact plan digest."),
        ])
    lines = [
        "PLAN {}".format(digest),
        "goal: {}".format(proposal["goal"]),
        "execution: {} at effort {} ({})".format(
            proposal["execution"]["model"], proposal["execution"]["effort"], plan["execution_status"]
        ),
        "outcomes:",
    ]
    limits = proposal["resource_limits"]
    if proposal["schema_version"] == 1:
        lines.insert(3, "limits (legacy V1): concurrency={} turns/task={} total_tokens={}".format(
            limits["max_concurrency"], limits["max_turns_per_task"], limits["max_total_tokens"]
        ))
    else:
        lines.insert(3, "limits: boxes={} concurrency={} turns/task={} total_tokens={} worker_cost={}c turn_timeout={}s".format(
            limits["box_pool_size"], limits["max_concurrency"],
            limits["max_turns_per_task"], limits["max_total_tokens"],
            limits["max_worker_cost_usd_cents"], limits["turn_timeout_seconds"],
        ))
    lines.extend("  - " + item for item in proposal["outcomes"])
    lines.append("exclusions:")
    lines.extend("  - " + item for item in proposal["exclusions"])
    lines.append("invariants:")
    lines.extend("  - " + item for item in proposal["invariants"])
    lines.append("task graph:")
    for task in proposal["tasks"]:
        dependencies = ", ".join(task["depends_on"]) or "none"
        lines.append("  - {} <- [{}]: {}".format(task["id"], dependencies, task["goal"]))
    lines.append("evaluator argv: {}".format(json.dumps(proposal["verification_argv"])))
    if plan["runbook"] is not None:
        lines.append("kernel runbook digest: {}".format(runbook_digest(plan["runbook"])))
    lines.append("limitation: " + plan["execution_limitation"])
    lines.append("canonical product plan JSON:")
    lines.append(json.dumps(plan, indent=2, sort_keys=True))
    lines.append("Approval is not implicit. Run `/approve yes` only after reviewing every line above.")
    return "\n".join(lines)


class InteractiveController:
    def __init__(
        self,
        workspace: Path,
        *,
        state_root: Optional[Path] = None,
        converse_fn: Callable[..., Any] = converse,
        spawn_fn: Callable[..., Any] = spawn_supervisor,
        preflight_fn: Callable[..., Any] = create_claude_capability,
    ):
        self.workspace = Path(workspace).resolve()
        self.store = SessionStore(self.workspace, state_root)
        with self.store.transaction():
            self.session = self.store.load()
        self.connections = ConnectionRegistry(self.store.project_dir)
        self.converse_fn = converse_fn
        self.spawn_fn = spawn_fn
        self.preflight_fn = preflight_fn
        self._command_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._box_cache = {}

    def cancel_active(self) -> None:
        self._cancel_event.set()

    def _persist_message(self, role: str, text: str, *, kind: str = "conversation") -> None:
        self.session = self.store.append_message(self.session, role, text, kind=kind)

    def _respond(
        self,
        *messages: str,
        exit_client: bool = False,
        login_argv: Optional[Sequence[str]] = None,
        login_provider: Optional[str] = None,
        login_choices: Sequence[str] = (),
        clear_transcript: bool = False,
        kind: str = "notice",
    ) -> CommandResponse:
        for message in messages:
            self._persist_message("orchestrator" if kind == "conversation" else "system", message, kind=kind)
        return CommandResponse(
            messages=tuple(messages),
            exit_client=exit_client,
            login_argv=tuple(login_argv) if login_argv else None,
            login_provider=login_provider,
            login_choices=tuple(login_choices),
            clear_transcript=clear_transcript,
        )

    def handle(self, raw: str, *, on_chunk: Optional[Callable[[str], None]] = None) -> CommandResponse:
        if raw.strip() == "/cancel":
            self.cancel_active()
            return CommandResponse(messages=("Planning cancellation requested. Authoritative worker execution is unchanged.",))
        if not self._command_lock.acquire(blocking=False):
            return CommandResponse(messages=("A request is still running. Use /cancel, or wait before submitting another command.",))
        try:
            with self.store.transaction():
                self.session = self.store.load()
                self._cancel_event.clear()
                return self._handle(raw, on_chunk=on_chunk)
        except SessionError as error:
            return CommandResponse(messages=("denied: {}".format(error),))
        finally:
            self._command_lock.release()

    def _handle(self, raw: str, *, on_chunk: Optional[Callable[[str], None]] = None) -> CommandResponse:
        text = raw.strip()
        if not text:
            return CommandResponse()
        try:
            reject_sensitive_text(text, "interactive input")
        except PlanningError as error:
            return self._respond("denied: {}".format(error), kind="error")
        self._persist_message("human", text, kind="command" if text.startswith("/") else "conversation")
        try:
            if text.startswith("/"):
                return self._command(text, on_chunk=on_chunk)
            if self.session.get("grill"):
                return self._answer_grill(text)
            reply, _ = self._call_planning(text, on_chunk=on_chunk)
            identity = "{} -> {}".format(reply.requested_model or "default", reply.resolved_model or "unreported")
            identity_message = "model identity: {} ({})".format(identity, reply.provider)
            self._persist_message("orchestrator", reply.text, kind="conversation")
            self._persist_message("system", identity_message, kind="notice")
            return CommandResponse(messages=(reply.text, identity_message))
        except (
            InteractiveError, PlanningError, ConversationError, ConnectionError, ProviderError,
            SupervisorError, SessionError, WorkspaceError, RunbookError, OSError, ValueError,
        ) as error:
            return self._respond("denied: {}".format(error), kind="error")

    def _call_planning(self, text: str, *, on_chunk=None, request_kind="conversation", call_id=None, history=None, no_tools=False):
        selection = parse_selection(self.session["model"])
        call_id = call_id or "planning-" + uuid4().hex
        started_at, started = datetime.now(timezone.utc).isoformat(), time.monotonic()
        reply, call_status = None, "failed"
        try:
            options = {"no_tools": True} if no_tools else {}
            reply = self.converse_fn(
                self.session["model"], text, self.session["messages"][:-1] if history is None else history,
                effort=self.session["effort"], workspace=self.workspace, on_chunk=on_chunk,
                cancel_event=self._cancel_event,
                **options,
            )
            if self._cancel_event.is_set():
                raise ConversationCancelled("planning request cancelled")
            call_status = "completed"
            return reply, call_id
        except ConversationCancelled:
            call_status = "cancelled"
            raise
        except ConversationError as error:
            reply = getattr(error, "reply", None)
            raise
        finally:
            if selection.provider != "manual":
                self.store.append_planning_call({
                    "schema": "camol.planning_call", "schema_version": 1, "call_id": call_id,
                    "session_id": self.session["session_id"], "provider": selection.provider,
                    "requested_model": selection.model, "resolved_model": reply.resolved_model if reply else None,
                    "effort": self.session["effort"], "started_at": started_at,
                    "duration_seconds": round(time.monotonic() - started, 6), "status": call_status,
                    "request_kind": request_kind, "input_tokens": reply.input_tokens if reply else None,
                    "output_tokens": reply.output_tokens if reply else None, "total_cost_usd": None,
                    "tool_policy": "none" if no_tools else "legacy_planning",
                    "provider_request_count": None,
                })

    def _command(self, text: str, *, on_chunk=None) -> CommandResponse:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            raise InteractiveError("command has unmatched quoting") from error
        command = parts[0].lower()
        arguments = parts[1:]
        if command == "/help":
            return CommandResponse(messages=(HELP,))
        if command == "/quit":
            return self._respond("Client detached. The supervisor, if running, was not stopped.", exit_client=True)
        if command == "/grill":
            if not arguments:
                raise InteractiveError("usage: /grill GOAL")
            self._reconcile_session()
            if self.session["status"] == "running":
                raise InteractiveError("a run is active; detach or inspect it instead of replacing its plan")
            goal = " ".join(arguments)
            grill = GrillState.start(goal)
            self.session = self.store.update(
                self.session,
                goal=goal,
                grill=grill.to_dict(),
                plan=None,
                plan_digest=None,
                approved_digest=None,
                run_id=None,
                selected_box=None,
                event_cursor=0,
                status="planning",
                state_dir=str(self.store.runs_dir / ("run-" + uuid4().hex)),
            )
            return self._respond("GRILL 1/{} — {}".format(len(grill_question_names()), grill.question()))
        if command == "/import":
            return self._import(arguments)
        if command == "/propose":
            return self._propose(arguments, on_chunk=on_chunk)
        if command == "/plan":
            if self.session["plan"] is None:
                raise InteractiveError("there is no plan; start with /grill GOAL")
            return self._respond(render_plan(self.session["plan"], self.session["plan_digest"]))
        if command == "/approve":
            return self._approve(arguments)
        if command == "/accept":
            if len(arguments) > 1:
                raise InteractiveError("usage: /accept [OUTCOME_DIGEST]")
            snapshot = self._control("acceptance")
            acceptance = snapshot.get("acceptance")
            if snapshot["status"] != "awaiting_acceptance" or not acceptance:
                raise InteractiveError("run is not awaiting final human acceptance")
            if not arguments:
                return self._respond(
                    "FINAL OUTCOME — inspect the integrated result and evidence before confirming:\n" + json.dumps(snapshot, indent=2, sort_keys=True),
                    "To accept exactly this outcome: /accept " + acceptance["outcome_digest"],
                )
            if arguments[0] != acceptance["outcome_digest"]:
                raise InteractiveError("acceptance requires the exact current outcome digest; review /accept")
            result = self._control("accept", {"approved_by": getpass.getuser(), "outcome_digest": arguments[0]})
            self.session = self.store.update(self.session, status="terminal")
            return self._respond("Final outcome accepted: " + json.dumps(result, sort_keys=True))
        if command == "/gate":
            if not 1 <= len(arguments) <= 2:
                raise InteractiveError("usage: /gate TASK_ID [ASSESSMENT_DIGEST]")
            snapshot = self._control("acceptance")
            waiting = snapshot["pending_gates"].get(arguments[0])
            if not waiting:
                raise InteractiveError("task has no pending human gate")
            if len(arguments) == 1:
                return self._respond(
                    "PENDING STATE GATE\n" + json.dumps(waiting, indent=2, sort_keys=True),
                    "To approve exactly this assessment: /gate {} {}".format(arguments[0], waiting["assessment_digest"]),
                )
            if arguments[1] != waiting["assessment_digest"]:
                raise InteractiveError("gate approval requires the exact current assessment digest")
            result = self._control("gate-approve", {"task_id": arguments[0], "approved_by": getpass.getuser(), "assessment_digest": arguments[1]})
            return self._respond("State gate approved: " + json.dumps(result, sort_keys=True))
        if command == "/model":
            if len(arguments) != 1:
                raise InteractiveError("usage: /model SELECTION")
            selection = parse_selection(arguments[0])
            self._invalidate_plan_for_setting_change()
            self.session = self.store.update(self.session, model=arguments[0])
            warning = "Manual mode makes no provider calls." if selection.provider == "manual" else (
                "Normal messages may now consume your {} account quota; worker execution still requires /approve and explicit /run preflight.".format(selection.provider)
            )
            return self._respond("model set to {}. {}".format(arguments[0], warning))
        if command == "/effort":
            if len(arguments) != 1 or arguments[0] not in EFFORTS:
                raise InteractiveError("usage: /effort low|medium|high|xhigh|max")
            self._invalidate_plan_for_setting_change()
            self.session = self.store.update(self.session, effort=arguments[0])
            return self._respond("effort set to {}".format(arguments[0]))
        if command == "/connections":
            return self._connections()
        if command == "/skills":
            if arguments:
                raise InteractiveError("usage: /skills")
            return CommandResponse(messages=(BUILTIN_PROTOCOLS,))
        if command == "/history":
            return self._history(arguments)
        if command == "/models":
            return self._models(arguments)
        if command == "/watch":
            return self._watch(arguments)
        if command == "/repo":
            return self._repo(arguments)
        if command == "/usage":
            if arguments == ["run"]:
                from .usage import usage_report
                events = self._persisted_events()
                if not events:
                    raise InteractiveError("no run ledger exists yet; /usage shows planning calls")
                return CommandResponse(messages=(json.dumps(usage_report(events), indent=2, sort_keys=True),))
            if arguments:
                raise InteractiveError("usage: /usage [run]")
            calls = self.store.planning_calls()
            known_input = sum(item["input_tokens"] for item in calls if type(item.get("input_tokens")) is int)
            known_output = sum(item["output_tokens"] for item in calls if type(item.get("output_tokens")) is int)
            unknown = sum(item.get("input_tokens") is None or item.get("output_tokens") is None for item in calls)
            return CommandResponse(messages=(
                "PLANNING USAGE — calls={} recorded_input_tokens={} recorded_output_tokens={} unknown_usage_calls={} duration={:.2f}s cost=unknown\nReceipts: {}".format(
                    len(calls), known_input, known_output, unknown,
                    sum(item.get("duration_seconds", 0) for item in calls), self.store.planning_calls_path,
                ),
            ))
        if command == "/debug":
            if arguments not in ([], ["list"], ["inbox"]) and not (len(arguments) == 2 and arguments[0] == "show"):
                raise InteractiveError("usage: /debug [list|inbox|show CASE_ID]")
            from .debugger import Debugger
            from .orchestrator import Orchestrator
            database = Path(self.session["state_dir"]) / "camol.sqlite3"
            if not database.is_file() or self.session["run_id"] is None:
                raise InteractiveError("no run ledger exists yet")
            store = ReadOnlyEventStore(database)
            try:
                debugger = Debugger(Orchestrator(store), self.session["run_id"])
                result = debugger.inbox() if arguments == ["inbox"] else debugger.inspect(
                    arguments[1] if len(arguments) == 2 else None)
            finally:
                store.close()
            return CommandResponse(messages=(json.dumps(result, indent=2, sort_keys=True),))
        if command == "/clear":
            if arguments:
                raise InteractiveError("usage: /clear")
            return CommandResponse(
                messages=("Terminal view cleared. Durable conversation, plan, run, and evidence state were preserved.",),
                clear_transcript=True,
            )
        if command == "/login":
            if not arguments:
                return self._respond(
                    "Choose a provider with ↑/↓ and Enter. Escape cancels; Camol never receives the provider credential.",
                    login_choices=("claude", "codex"),
                )
            if len(arguments) != 1:
                raise InteractiveError("usage: /login [claude|codex]")
            argv = self.connections.login_argv(arguments[0])
            return self._respond(
                "Opening {} login in the terminal/browser. Return here after the provider confirms sign-in; Camol will verify the connection without reading its credential cache.".format(
                    arguments[0].title()
                ),
                login_argv=argv,
                login_provider=arguments[0],
            )
        if command == "/run":
            return self._run(arguments)
        if command == "/status":
            return self._status()
        if command == "/boxes":
            return self._boxes()
        if command == "/box":
            return self._box(arguments)
        if command == "/events":
            return self._events(arguments)
        if command in {"/drain", "/resume", "/stop"}:
            if arguments:
                raise InteractiveError("usage: {}".format(command))
            result = self._control(command[1:])
            return self._respond("supervisor {} requested: {}".format(command[1:], json.dumps(result, sort_keys=True)))
        if command == "/btw":
            if not arguments:
                raise InteractiveError("usage: /btw NOTE")
            return self._respond(
                "BTW recorded outside the frozen plan. If it changes scope, invariants, tasks, or evaluation, amend with /grill and approve a new digest."
            )
        raise InteractiveError("unknown command {}; use /help".format(command))

    @staticmethod
    def _inspection(title: str, value: Any) -> CommandResponse:
        rendered = json.dumps(value, indent=2, sort_keys=True)
        if len(rendered) > 64000:
            rendered = rendered[:64000] + "\n… preview truncated at 64000 characters; use the corresponding kernel CLI for complete output."
        return CommandResponse(messages=(title + "\n" + rendered,))

    def _models(self, arguments: Sequence[str]) -> CommandResponse:
        if arguments not in ([], ["list"]) and not (len(arguments) == 2 and arguments[0] == "status"):
            raise InteractiveError("usage: /models [list|status PLAN_DIGEST]")
        from .models import ModelStore
        root = Path(self.store.root or default_state_root()) / "models"
        if not root.exists():
            return CommandResponse(messages=("No local model artifact catalog exists. Use `camol models` to explicitly prepare and approve a download. No download or model load was started.",))
        with ModelStore(root, read_only=True) as store:
            if len(arguments) == 2:
                result = store.status(arguments[1])
            else:
                catalog = store.list()
                result = {"total_plans": len(catalog), "plans": [{
                    "plan_digest": row["plan_digest"], "model_id": row["plan"]["model_id"],
                    "revision": row["plan"]["revision"], "status": row["status"],
                    "accounted_bytes": row["accounted_bytes"], "observed_bytes": row["observed_bytes"],
                    "loaded": row["loaded"], "inference_ready": row["inference_ready"],
                } for row in catalog[:100]], "truncated": len(catalog) > 100}
        return self._inspection("PASSIVE MODEL ARTIFACTS — recorded download state; current hashes are not rechecked by this preview. Downloaded is not loaded or inference-ready.", result)

    def _watch(self, arguments: Sequence[str]) -> CommandResponse:
        if arguments not in ([], ["list"]) and not (len(arguments) == 2 and arguments[0] == "show"):
            raise InteractiveError("usage: /watch [list|show WATCHER_ID]")
        from .state import project
        events = self._persisted_events()
        if not events:
            raise InteractiveError("no run ledger exists yet")
        watchers = project(events).get("watchers", {})
        if len(arguments) == 2:
            if arguments[1] not in watchers:
                raise InteractiveError("unknown watcher: " + arguments[1])
            result = watchers[arguments[1]]
        else:
            result = watchers
        return self._inspection("RECORDED WATCHERS — read-only ledger inspection; no observer or remote poll was invoked.", result)

    def _repo(self, arguments: Sequence[str]) -> CommandResponse:
        if (arguments not in ([], ["summary"], ["cycles"])
                and not (len(arguments) == 2 and arguments[0] == "impact")
                and not (len(arguments) == 3 and arguments[0] == "why")):
            raise InteractiveError("usage: /repo [summary|impact PATH|why FROM TO|cycles]")
        from .repository_graph import RepositoryGraph, crawl_repository
        graph = RepositoryGraph(crawl_repository(self.workspace))
        if arguments in ([], ["summary"]):
            return CommandResponse(messages=(graph.render_text(),))
        result = (graph.cycles() if arguments == ["cycles"] else graph.impact(arguments[1])
                  if arguments[0] == "impact" else graph.why(arguments[1], arguments[2]))
        return self._inspection("STATIC REPOSITORY GRAPH — source declarations only; actual runtime and deployment capability remain unverified.", result)

    def _invalidate_plan_for_setting_change(self) -> None:
        self._reconcile_session()
        if self.session["status"] == "running":
            raise InteractiveError("model and effort are frozen while a run is active")
        if self.session["plan"] is not None:
            self.session = self.store.update(
                self.session,
                plan=None,
                plan_digest=None,
                approved_digest=None,
                run_id=None,
                grill=None,
                status="new",
            )

    def _reconcile_session(self) -> None:
        if self.session["status"] != "running":
            return
        try:
            status = self._control("status")
        except (SupervisorError, OSError):
            # Read only terminal evidence. A missing socket alone never proves
            # that a crashed or drained run completed.
            if any(event["type"] in {"RUN_COMPLETED", "RUN_BLOCKED", "RUN_ACCEPTED"} for event in self._persisted_events()):
                self.session = self.store.update(self.session, status="terminal")
            return
        if status["run"]["status"] in {"completed", "blocked"}:
            self.session = self.store.update(self.session, status="terminal")

    def _persisted_events(self) -> List[Dict[str, Any]]:
        database = Path(self.session["state_dir"]) / "camol.sqlite3"
        if not database.is_file() or database.is_symlink() or self.session["run_id"] is None:
            return []
        store = None
        try:
            store = ReadOnlyEventStore(database)
            return store.read(self.session["run_id"])
        except (sqlite3.Error, ValueError, OSError):
            return []
        finally:
            if store is not None:
                store.close()

    def _import(self, arguments: Sequence[str]) -> CommandResponse:
        if len(arguments) != 1:
            raise InteractiveError("usage: /import PATH")
        self._reconcile_session()
        if self.session["status"] == "running":
            raise InteractiveError("an unfinished run owns this session; resume or inspect it before importing another plan")
        path = Path(arguments[0]).expanduser()
        path = path if path.is_absolute() else self.workspace / path
        if path.stat().st_size > 2 * 1024 * 1024:
            raise InteractiveError("runbook is too large to review")
        runbook = load_runbook(path)
        reject_sensitive_text(json.dumps(runbook), "imported runbook")
        state_dir = self.store.runs_dir / ("run-" + uuid4().hex)
        workspace = WorkspaceManager(self.workspace, state_dir)
        workspace.assert_source_ready()
        kinds = {agent["adapter"]["kind"] for agent in runbook["agents"]}
        if kinds in ({"codex_cli"}, {"codex_oss"}):
            if any("profile_snapshot" not in agent["adapter"] for agent in runbook["agents"]):
                raise InteractiveError("interactive Codex plans require every adapter's exact embedded profile_snapshot; no file-backed policy is silently frozen")
            limitation = ("Codex execution requires /run to review and acknowledge the exact weaker provider-policy digest. "
                          "Runtime/login or local catalog checks do not establish quota, resolved model or inference readiness. "
                          "No hard USD, inner-model-turn, or destination-egress guarantee is available.")
        else:
            limitation = ("Local process adapters use the exact imported executable protocol. No hosted-model proof is claimed."
                          if kinds == {"process"} else
                          "Hosted execution requires explicit preflight and frozen spend acknowledgement; mixed adapters are not supported by this terminal launch command.")
        plan = validate_envelope({
            "schema": "camol.product_plan", "schema_version": 2, "proposal": None,
            "run_id": runbook["run"]["id"], "runbook": runbook,
            "source": {"workspace": str(self.workspace), "revision": workspace.head_revision()},
            "execution_status": "ready" if kinds == {"process"} else "preflight_required",
            "execution_limitation": limitation,
        })
        digest = canonical_digest(plan)
        rendered = render_plan(plan, digest)
        if len(rendered) > 200_000:
            raise InteractiveError("the exact imported plan is too large to review in this client")
        self.session = self.store.update(
            self.session, plan=plan, plan_digest=digest, approved_digest=None,
            run_id=plan["run_id"], goal=runbook["run"]["objective"], grill=None,
            state_dir=str(state_dir), selected_box=None, event_cursor=0, status="plan_ready",
        )
        return self._respond(rendered)

    def _propose(self, arguments: Sequence[str], *, on_chunk=None) -> CommandResponse:
        from .debug_execution import source_identity
        from .proposal import MAX_SEED_BYTES, MAX_RESPONSE_CHARS, parse_response, read_seed_bytes, request_prompt, strict_json, validate_seed
        if len(arguments) < 3 or arguments[0] != "--from":
            raise InteractiveError("usage: /propose --from REVIEWED_SEED.json GOAL (one planning invocation; no worker execution)")
        self._reconcile_session()
        if self.session["status"] == "running":
            raise InteractiveError("an unfinished run owns this session; inspect it before proposing a replacement")
        selection = parse_selection(self.session["model"])
        require_tool_free_provider(selection)
        path = Path(arguments[1]).expanduser()
        path = path if path.is_absolute() else self.workspace / path
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SEED_BYTES:
            raise InteractiveError("proposal seed must be a regular non-symlink JSON file of at most 24000 bytes")
        path = path.resolve(strict=True)
        seed_bytes = read_seed_bytes(path)
        if len(seed_bytes) > MAX_SEED_BYTES:
            raise InteractiveError("proposal seed changed or exceeds its byte bound")
        seed = validate_seed(strict_json(seed_bytes.decode("utf-8")))
        source = source_identity(self.workspace)
        goal, owner = " ".join(arguments[2:]), getpass.getuser()
        prompt = request_prompt(seed, goal, source, owner)
        call_id = "planning-" + uuid4().hex
        seed_bytes_digest = "sha256:" + hashlib.sha256(seed_bytes).hexdigest()
        # This is the exact logical request, including the existing bounded
        # dialogue; no arbitrary repository contents or dependency crawl is sent.
        request = {"message": prompt, "history": planning_history(self.session["messages"][:-1]),
                   "model": self.session["model"], "effort": self.session["effort"], "tool_policy": "none"}
        origin = {"kind": "seed_assisted_model_proposal", "seed_path": str(path),
                  "seed_digest": canonical_digest(seed), "seed_bytes_digest": seed_bytes_digest,
                  "request_digest": canonical_digest(request), "response_digest": None, "planning_call_id": call_id}
        notice = ("PROPOSE — one planning-only {} invocation, 120-second request timeout; internal provider request count and cost may be unknown and are not hard-capped. "
                  "Sending the reviewed seed, goal and existing bounded dialogue, not repository file contents. "
                  "No tools, worker launch, approval, or automatic retry. Response must stay inside the seed envelope.").format(self.session["model"])
        self._persist_message("system", notice, kind="notice")
        if on_chunk:
            on_chunk(notice + "\n")
        self._persist_message("system", "PROPOSAL REQUEST\n" + prompt, kind="notice")
        self.store.append_proposal_event({"type": "PROPOSAL_REQUESTED", "origin": origin, "source": source,
                                         "request": request, "observed_at": datetime.now(timezone.utc).isoformat()})
        outcome = "rejected"
        try:
            reply, _ = self._call_planning(prompt, request_kind="seed_proposal", call_id=call_id,
                                           history=request["history"], no_tools=True)
            if not isinstance(reply.text, str):
                raise PlanningError("planning provider returned a non-text proposal")
            origin["response_digest"] = "sha256:" + hashlib.sha256(reply.text.encode("utf-8")).hexdigest()
            self._persist_message("orchestrator", "UNAPPROVED PROPOSAL RESPONSE\n" + reply.text[:MAX_RESPONSE_CHARS]
                                  + ("\n[response truncated; oversized candidate rejected]" if len(reply.text) > MAX_RESPONSE_CHARS else ""), kind="notice")
            result = parse_response(reply.text, seed, goal=goal, owner=owner)
            if source_identity(self.workspace) != source:
                raise InteractiveError("source checkout changed during planning; proposal rejected, usage retained")
            if path.is_symlink() or read_seed_bytes(path) != seed_bytes:
                raise InteractiveError("reviewed seed changed during planning; proposal rejected, usage retained")
            if "questions" in result:
                outcome = "questions"
                questions = "\n".join("{}. {}".format(index, text) for index, text in enumerate(result["questions"], 1))
                self._persist_message("orchestrator", "Questions about the proposed goal " + goal + ":\n" + questions, kind="conversation")
                return self._respond("PROPOSAL QUESTIONS — no new candidate or approval was created. Existing plan, if any, is unchanged. Answer in dialogue, revise the seed if needed, then explicitly invoke /propose again.\n" + questions)
            runbook = result["runbook"]
            kinds = {agent["adapter"]["kind"] for agent in runbook["agents"]}
            plan = validate_envelope({
                "schema": "camol.product_plan", "schema_version": 3, "proposal": None,
                "run_id": runbook["run"]["id"], "runbook": runbook, "source": source, "origin": origin,
                "execution_status": "ready" if kinds == {"process"} else "preflight_required",
                "execution_limitation": "Seed-assisted model proposal, not verified evidence. Every task/state gate and final acceptance requires human review. Existing provider limitations and exact readiness remain in force; mixed-provider terminal launches are unsupported.",
            })
            digest = canonical_digest(plan)
            rendered = render_plan(plan, digest)
            if len(rendered) > 200000:
                raise InteractiveError("model candidate is too large to review safely")
            self.session = self.store.update(self.session, plan=plan, plan_digest=digest, approved_digest=None,
                run_id=plan["run_id"], goal=goal, grill=None, state_dir=str(self.store.runs_dir / ("run-" + uuid4().hex)),
                selected_box=None, event_cursor=0, status="plan_ready")
            outcome = "candidate_ready"
            return self._respond("MODEL CANDIDATE — nothing has started. Inspect every command, scope, and proposed invariant/evaluator mapping; use /plan and approve its exact digest only after review.", rendered)
        except ConversationCancelled:
            outcome = "cancelled"
            raise
        finally:
            self.store.append_proposal_event({"type": "PROPOSAL_FINISHED", "outcome": outcome, "origin": origin,
                                             "source": source, "observed_at": datetime.now(timezone.utc).isoformat()})

    def _answer_grill(self, text: str) -> CommandResponse:
        grill = GrillState.from_dict(self.session["grill"])
        grill = grill.answer(text)
        if grill.status != "complete":
            self.session = self.store.update(self.session, grill=grill.to_dict())
            return self._respond(
                "GRILL {}/{} — {}".format(grill.question_index + 1, len(grill_question_names()), grill.question())
            )
        proposal = proposal_from_grill(
            grill, model=self.session["model"], effort=self.session["effort"]
        )
        plan = validate_envelope(_envelope(proposal))
        digest = canonical_digest(plan)
        rendered = render_plan(plan, digest)
        if len(rendered) > 200_000:
            raise PlanningError("the exact plan is too large to review safely; use smaller tasks and outcomes")
        self.session = self.store.update(
            self.session,
            grill=None,
            plan=plan,
            plan_digest=digest,
            approved_digest=None,
            run_id=plan["run_id"],
            status="plan_ready",
        )
        return self._respond(
            "Grill complete. Nothing has started.",
            rendered,
        )

    def _approve(self, arguments: Sequence[str]) -> CommandResponse:
        if self.session["plan"] is None or self.session["status"] != "plan_ready":
            raise InteractiveError("there is no unapproved plan ready for confirmation")
        if not arguments:
            if self.session["plan"]["schema_version"] == 3:
                return self._respond("Model proposal approval requires its exact digest. Review /plan, then /approve " + self.session["plan_digest"])
            return self._respond(
                "Approval required for {}. Review /plan, then type `/approve yes` or `/approve {}`.".format(
                    self.session["plan_digest"], self.session["plan_digest"]
                )
            )
        if len(arguments) != 1 or arguments[0] not in {"yes", self.session["plan_digest"]}:
            raise InteractiveError("approval must be `yes` or the exact plan digest")
        if self.session["plan"]["schema_version"] == 3 and arguments[0] == "yes":
            raise InteractiveError("a model-generated proposal requires the exact /plan digest, not a bare yes")
        self._verify_proposal_source(self.session["plan"])
        self.session = self.store.update(
            self.session,
            approved_digest=self.session["plan_digest"],
            status="approved",
        )
        return self._respond(
            "Approved {} by {}. Approval authorizes this plan only; it has not started work.".format(
                self.session["approved_digest"], getpass.getuser()
            )
        )

    def _connections(self) -> CommandResponse:
        records = self.connections.probe_all()
        glyph = {"ready": "■", "auth_required": "□", "unavailable": "□", "unknown": "?", "error": "!"}
        lines = ["CONNECTIONS — account/reachability only; not task readiness"]
        for record in records:
            lines.append("{} {:<16} {:<14} {}".format(
                glyph[record["status"]], record["connection_id"], record["status"], record["detail"]
            ))
        return self._respond("\n".join(lines))

    def confirm_provider_connection(
        self,
        provider: str,
        *,
        login_returncode: Optional[int] = None,
    ) -> CommandResponse:
        """Promote a verified login to the active planner when no plan is frozen."""
        if not self._command_lock.acquire(blocking=False):
            return CommandResponse(messages=("Login verification finished while another request is active. Inspect /connections and choose /model after that request.",))
        try:
            with self.store.transaction():
                self.session = self.store.load()
                return self._confirm_provider_connection(provider, login_returncode=login_returncode)
        except SessionError as error:
            return CommandResponse(messages=("Login selection deferred: {}. Choose /model after that request.".format(error),))
        finally:
            self._command_lock.release()

    def _confirm_provider_connection(self, provider: str, *, login_returncode: Optional[int]) -> CommandResponse:
        connection_id = {"claude": "claude-cli", "codex": "codex-cli"}.get(provider)
        if connection_id is None:
            raise InteractiveError("login confirmation supports claude or codex")
        if login_returncode != 0:
            return self._respond(
                "{} login did not complete (exit={}); the active model was not changed.".format(
                    provider.title(), login_returncode
                ),
                kind="error",
            )
        record = next(
            (item for item in self.connections.load() if item["connection_id"] == connection_id),
            None,
        )
        if record is None or record["status"] != "ready":
            detail = record["detail"] if record is not None else "provider status was not returned"
            return self._respond(
                "{} login was not confirmed (exit={}): {}".format(
                    provider.title(), login_returncode, detail
                ),
                kind="error",
            )
        selection = "claude:fable" if provider == "claude" else "codex"
        if self.session.get("plan") is not None or self.session["status"] == "running":
            return self._respond(
                "{} connection confirmed.".format(provider.title()),
                "Active model remains {} because a plan is frozen or running. Choose {} explicitly after that run.".format(
                    self.session["model"], selection
                ),
            )
        self._invalidate_plan_for_setting_change()
        self.session = self.store.update(self.session, model=selection)
        return self._respond(
            "{} connection confirmed.".format(provider.title()),
            "Active planning model set to {}.".format(selection),
        )

    def _history(self, arguments: Sequence[str]) -> CommandResponse:
        if len(arguments) > 1:
            raise InteractiveError("usage: /history [COUNT]")
        count = 20
        if arguments:
            try:
                count = int(arguments[0])
            except ValueError as error:
                raise InteractiveError("history count must be an integer from 1 to 100") from error
            if not 1 <= count <= 100:
                raise InteractiveError("history count must be an integer from 1 to 100")
        # The current /history command was persisted before dispatch. Exclude it
        # so asking to inspect history does not make itself the newest result.
        retained = self.store.history(self.session["messages"], count + 1)[:-1]
        if not retained:
            return CommandResponse(messages=("No earlier durable transcript entries.",))
        labels = {"human": "you", "orchestrator": "orchestrator", "system": "camol"}
        lines = ["DURABLE HISTORY — latest {} entr{}".format(
            len(retained), "y" if len(retained) == 1 else "ies"
        )]
        for item in retained:
            lines.append("{} > {}".format(labels[item["role"]], item["content"][:4_000]))
        return CommandResponse(messages=("\n".join(lines),))

    def _control(self, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return asyncio.run(send_control_v2(
            Path(self.session["state_dir"]), command, requested_by=getpass.getuser(), params=params or {}
        ))["result"]

    def _run(self, arguments: Sequence[str]) -> CommandResponse:
        if self.session["approved_digest"] is None or self.session["approved_digest"] != self.session["plan_digest"]:
            raise InteractiveError("run requires the exact current plan to be approved")
        plan = validate_envelope(self.session["plan"])
        if plan["runbook"] is None:
            raise InteractiveError(plan["execution_limitation"])
        state_dir = Path(self.session["state_dir"])
        paths = SupervisorPaths.under(state_dir)
        if paths.socket.exists():
            try:
                remote_plan = self._control("plan")
                remote_status = self._control("status")
            except (SupervisorError, OSError):
                # A crash can leave a stale socket. The replacement supervisor
                # acquires the leader lock before removing it.
                pass
            else:
                if remote_plan["plan_digest"] != runbook_digest(plan["runbook"]):
                    raise InteractiveError("another supervisor owns this session state with a different plan")
                if plan["schema_version"] == 3 and (remote_plan.get("source_binding") or {}).get("source") != plan["source"]:
                    raise InteractiveError("supervisor source binding differs from the exact approved proposal; reattach refused")
                session_status = "terminal" if remote_status["run"]["status"] in {"completed", "blocked"} else "running"
                self.session = self.store.update(self.session, status=session_status)
                return self._respond(
                    "Reattached to the existing supervisor without a new provider request or preflight.",
                    "Supervisor reattached pid={}; closing this client will not stop it.".format(remote_status["pid"]),
                )
        self._verify_proposal_source(plan)
        workspace = WorkspaceManager(self.workspace, state_dir)
        workspace.assert_source_ready()
        if plan.get("source") is not None and (
            plan["source"]["workspace"] != str(self.workspace)
            or plan["source"]["revision"] != workspace.head_revision()
        ):
            raise InteractiveError("source checkout changed after import; import and approve the current revision again")
        state_dir.mkdir(parents=True, exist_ok=True)
        adapter_kinds = {agent["adapter"]["kind"] for agent in plan["runbook"]["agents"]}
        if adapter_kinds == {"claude_cli"}:
            remaining = list(arguments)
            accept_spend = False
            preflight_cents = 10
            worker_cents = None
            while remaining:
                option = remaining.pop(0)
                if option == "--accept-spend" and not accept_spend:
                    accept_spend = True
                elif option == "--preflight-cents" and remaining:
                    try:
                        preflight_cents = int(remaining.pop(0))
                    except ValueError as error:
                        raise InteractiveError("--preflight-cents requires a positive integer") from error
                elif option == "--worker-cents" and remaining:
                    try:
                        worker_cents = int(remaining.pop(0))
                    except ValueError as error:
                        raise InteractiveError("--worker-cents requires a positive integer") from error
                else:
                    raise InteractiveError(
                        "usage: /run --accept-spend --worker-cents N [--preflight-cents N]"
                    )
            if preflight_cents <= 0 or preflight_cents > 100:
                raise InteractiveError("--preflight-cents must be between 1 and 100")
            if not accept_spend:
                raise InteractiveError(
                    "provider capability is speculative; explicitly acknowledge both preflight and the frozen worker ceiling"
                )
            profile = model_profile_for_adapter(self.workspace, plan["runbook"]["agents"][0]["adapter"])
            if worker_cents != profile.max_run_usd_cents:
                raise InteractiveError(
                    "--worker-cents must exactly match the approved {} cent run ceiling".format(
                        profile.max_run_usd_cents
                    )
                )
            status = self.connections.probe_claude()
            if status["status"] != "ready":
                raise InteractiveError("Claude connection is not authenticated; use /login claude")
            receipt = self.preflight_fn(
                profile,
                target_id=local_target_id(),
                state_dir=state_dir,
                cwd=self.workspace,
                accept_spend=True,
                spend_ceiling_cents=preflight_cents,
            )
            readiness_line = "Run accepted after provider preflight: requested={} resolved={} receipt={}.".format(
                receipt.requested_model, receipt.resolved_model, receipt.digest()
            )
        elif adapter_kinds == {"process"}:
            if arguments:
                raise InteractiveError("a local process plan uses `/run` without provider-spend flags")
            readiness_line = "Run accepted for the explicitly approved local process adapter; no hosted-model proof is claimed."
        elif adapter_kinds in ({"codex_cli"}, {"codex_oss"}):
            policy = self._codex_launch_policy(plan)
            digest = canonical_digest(policy)
            hosted = adapter_kinds == {"codex_cli"}
            expected = " /run --accept-provider-policy " + digest + (" --accept-spend" if hosted else "")
            if not arguments:
                return self._respond(
                    "PROVIDER POLICY ACKNOWLEDGEMENT — no worker or paid preflight started.\n" + json.dumps(policy, indent=2, sort_keys=True),
                    "After reviewing these limitations, type:" + expected,
                )
            remaining = list(arguments)
            accepted_digest, accept_spend = None, False
            while remaining:
                option = remaining.pop(0)
                if option == "--accept-provider-policy" and remaining and accepted_digest is None:
                    accepted_digest = remaining.pop(0)
                elif option == "--accept-spend" and not accept_spend:
                    accept_spend = True
                else:
                    raise InteractiveError("this provider has no hard spend-capped preflight; use" + expected)
            if accepted_digest != digest:
                raise InteractiveError("provider policy requires the exact current acknowledgement digest; review /run")
            if hosted != accept_spend:
                raise InteractiveError("hosted Codex requires --accept-spend; local Codex must not imply hosted billing approval")
            readiness_line = ("Explicit provider limitations acknowledged: " + digest + ". "
                              "No paid capability probe ran; model identity is requested-only and quota remains unknown. "
                              "Kernel runtime/login/catalog and exact admission checks still gate all leases. "
                              + ("Dollar values are reservations, not a hard spend cap; unknown paid usage stops further paid launches."
                                 if hosted else "Existing catalog presence is not inference, weights-identity, resource-fit, or airgap proof; no model is downloaded or loaded."))
        else:
            raise InteractiveError("Product V0 cannot execute a mixed or unsupported adapter plan")
        runbook_path = self.store.write_runbook(self.session, plan["runbook"])
        launch_options = {"expected_source": plan["source"]} if plan["schema_version"] == 3 else {}
        started = self.spawn_fn(
            runbook_path,
            self.workspace,
            state_dir,
            approve_by=getpass.getuser(),
            **launch_options,
        )
        self.session = self.store.update(self.session, status="running")
        return self._respond(
            readiness_line,
            "Supervisor {} pid={}; closing this client will not stop it. No box leases until exact admission is green.".format(
                "reattached" if not started.get("started", True) else "detached", started["pid"]
            ),
        )

    def _codex_launch_policy(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        if plan.get("source") is None or plan["runbook"]["schema_version"] < 5:
            raise InteractiveError("Codex launch requires an imported V5+ source-bound runbook")
        agents = []
        for agent in sorted(plan["runbook"]["agents"], key=lambda item: item["id"]):
            if "profile_snapshot" not in agent["adapter"]:
                raise InteractiveError("Codex launch requires an exact embedded profile_snapshot for every worker")
            profile = model_profile_for_adapter(self.workspace, agent["adapter"])
            agents.append({"agent_id": agent["id"], "profile_digest": profile.digest(),
                           "profile": profile.to_dict()})
        return {"schema": "camol.interactive_provider_ack", "schema_version": 1,
                "product_plan_digest": self.session["plan_digest"], "runbook_digest": runbook_digest(plan["runbook"]),
                "source": dict(plan["source"]), "agents": agents,
                "limitations": {"resolved_model": "unverified", "quota_available": "unknown",
                                "hard_usd_cap": "unsupported; configured dollars reserve accounting authority only",
                                "inner_model_turn_cap": "unsupported", "restricted_egress": "unsupported; ambient network explicitly granted",
                                "capability_preflight": "none; runtime/login or catalog observation only",
                                "local_model_load_or_download": "never implicit"}}

    def _verify_proposal_source(self, plan: Mapping[str, Any]) -> None:
        if plan.get("schema_version") == 3:
            from .debug_execution import source_identity
            if source_identity(self.workspace) != plan["source"]:
                raise InteractiveError("source checkout changed since the model proposal; request and review a fresh candidate")

    def _status(self) -> CommandResponse:
        self._reconcile_session()
        lines = [
            "session={} status={} model={} effort={}".format(
                self.session["session_id"], self.session["status"], self.session["model"], self.session["effort"]
            ),
            "plan={} approved={}".format(self.session["plan_digest"] or "none", self.session["approved_digest"] or "none"),
            "state_dir={}".format(self.session["state_dir"]),
        ]
        try:
            status = self._control("status")
        except SupervisorError:
            lines.append("supervisor=detached/not-running")
        else:
            run = status["run"]
            lines.append("supervisor={} pid={} run={} total_tokens={}".format(
                status["mode"], status["pid"], run["status"], run["total_tokens"]
            ))
            if run["status"] in {"completed", "blocked"}:
                self.session = self.store.update(self.session, status="terminal")
        return self._respond("\n".join(lines))

    def _planned_boxes(self) -> List[Dict[str, str]]:
        plan = self.session.get("plan")
        if not plan or not plan.get("runbook"):
            return []
        return [
            {"box_id": agent["id"], "status": "dormant", "task_id": "unassigned"}
            for agent in plan["runbook"]["agents"]
        ]

    def box_summaries(self) -> List[Dict[str, str]]:
        try:
            status = self._control("status")
        except SupervisorError:
            return self._planned_boxes()
        run = status["run"]
        workspace_by_box = {item.get("box_id"): item for item in status.get("boxes", [])}
        boxes = []
        for agent_id, agent in run["agents"].items():
            task_id = agent.get("task_id") or "unassigned"
            task = run["tasks"].get(task_id, {})
            boxes.append({
                "box_id": agent_id,
                "status": task.get("status", agent.get("status", "idle")),
                "task_id": task_id,
                "connected": "yes" if agent_id in workspace_by_box else "no",
            })
        return boxes

    def _boxes(self) -> CommandResponse:
        boxes = self.box_summaries()
        if not boxes:
            return self._respond("No boxes exist yet. Complete /grill to derive the N-box pool.")
        attention = {"blocked", "waiting"}
        lines = ["BOXES — ■ workspace connected, □ dormant/unprepared, ! attention"]
        for index, box in enumerate(boxes, 1):
            mark = "!" if box["status"] in attention else "■" if box.get("connected") == "yes" else "□"
            lines.append("{} {:>2} {:<18} {:<12} {}".format(mark, index, box["box_id"], box["status"], box["task_id"]))
        return self._respond("\n".join(lines))

    def _box(self, arguments: Sequence[str]) -> CommandResponse:
        if not 1 <= len(arguments) <= 2:
            raise InteractiveError("usage: /box ID|NUMBER [status|context|tools|diff|evals|events|evidence|transcript]")
        boxes = self.box_summaries()
        target = arguments[0]
        if target in {"next", "previous"} and boxes:
            ids = [item["box_id"] for item in boxes]
            current = ids.index(self.session["selected_box"]) if self.session["selected_box"] in ids else -1
            target = ids[(current + (1 if target == "next" else -1)) % len(ids)]
        if target.isdigit() and 1 <= int(target) <= len(boxes):
            target = boxes[int(target) - 1]["box_id"]
        if target not in {item["box_id"] for item in boxes}:
            raise InteractiveError("unknown box")
        subview = arguments[1] if len(arguments) == 2 else "events"
        if subview not in {"status", "context", "tools", "diff", "evals", "events", "evidence", "transcript"}:
            raise InteractiveError("unknown box view; use status, context, tools, diff, evals, events, evidence, or transcript")
        self.session = self.store.update(self.session, selected_box=target)
        rendered = self.inspect_box(target, subview)
        return CommandResponse(messages=(rendered,), box_id=target, box_view=subview)

    def inspect_box(self, target: str, subview: str = "events") -> str:
        """Read a box snapshot without changing authority or writing chat history."""
        stale = False
        try:
            view = self._control(
                "box", {"box_id": target, "after_seq": 0, "limit": 200, "tail": True}
            )
            self._box_cache[target] = view
        except (SupervisorError, OSError):
            view = self._box_cache.get(target)
            if view is None:
                return "BOX {} is dormant or unavailable; no live evidence snapshot is available.".format(target)
            stale = True
        lines = [
            "BOX {} / {} {}adapter={} tasks={} workspace={}".format(
                target, subview, "STALE — supervisor disconnected; " if stale else "",
                view["adapter_kind"], ",".join(view["task_ids"]) or "none",
                view["workspace"]["path"] if view["workspace"] else "not prepared",
            ),
            "Read-only snapshot; messages in the composer always go to the orchestrator.",
        ]
        if subview == "status":
            lines.append(json.dumps(view.get("task_states", {}), indent=2, sort_keys=True))
        elif subview == "context":
            lines.append(json.dumps(view.get("task_contracts", []), indent=2, sort_keys=True))
        channels = {
            "context": {"context-packet"},
            "diff": {"workspace-diff", "diff", "candidate-patch"},
            "transcript": {"stdout", "stderr", "agent-result", "provider-stdout", "provider-stderr"},
        }
        matched = 0
        for retained in view.get("artifacts", {}).values():
            reference = retained["reference"]
            channel = reference.get("producer", {}).get("channel", "")
            selected = (
                subview == "evidence"
                or channel in channels.get(subview, set())
                or (subview == "diff" and "diff" in channel)
                or (subview == "tools" and any(word in channel for word in ("tool", "invocation", "command")))
                or (subview == "evals" and any(word in channel for word in ("verif", "eval", "test")))
            )
            if selected:
                matched += 1
                lines.append("ARTIFACT {} {}{}".format(
                    channel, reference["digest"], " [preview truncated]" if retained.get("preview_truncated") else "",
                ))
                lines.append(retained.get("preview", "unavailable: " + retained.get("error", "unknown")))
        event_types = {
            "tools": {"EVIDENCE_RECORDED", "AGENT_TURN_RECORDED"},
            "evals": {"TASK_VERIFICATION_RECORDED", "COUNTEREXAMPLE_RECORDED", "DEBUG_CASE_OPENED", "DEBUG_CASE_VERIFIED", "EVAL_PROMOTED"},
            "diff": {"CANDIDATE_CAPTURED", "INTEGRATION_ACCEPTED"},
        }
        events = view["events"] if subview in {"events", "evidence"} else [
            event for event in view["events"] if event["type"] in event_types.get(subview, set())
        ]
        for event in events:
            payload = json.dumps(event["payload"], sort_keys=True)
            lines.append("#{} {} {}{}".format(
                event["seq"], event["type"], payload[:8000],
                " [display truncated; full payload is in /events export]" if len(payload) > 8000 else "",
            ))
        if not matched and not events and subview not in {"status", "context"}:
            lines.append("No recorded {} in this snapshot.".format(subview))
        lines.append("Snapshot includes the latest 200 events and 16 artifact previews; full retained content is available through camol export.")
        return "\n".join(lines)

    def _events(self, arguments: Sequence[str]) -> CommandResponse:
        if arguments:
            raise InteractiveError("usage: /events")
        result = self._control("events", {
            "after_seq": self.session["event_cursor"], "limit": 100, "wait_ms": 0,
        })
        self.session = self.store.update(self.session, event_cursor=result["next_seq"])
        if not result["events"]:
            return self._respond("No new events after sequence {}.".format(result["next_seq"]))
        return self._respond("\n".join(
            "#{:<4} {:<30} actor={} {}".format(
                event["seq"], event["type"], event["actor_id"],
                json.dumps(event["payload"], sort_keys=True)[:500],
            )
            for event in result["events"]
        ))


def grill_question_names() -> Tuple[str, ...]:
    # Keeps the controller insulated from the question text while making progress visible.
    from .planning import QUESTIONS
    return tuple(name for name, _ in QUESTIONS)
