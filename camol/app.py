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

from .connections import ConnectionError, ConnectionRegistry, observation_label
from .launch_manifest import build_manifest, profiles_for_runbook, assert_preflight_clear, LaunchError
from . import draft_creation
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
    read_capability,
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
    focus_orchestrator: bool = False
    switcher: Optional[Dict[str, Any]] = None


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
    SlashCommand("/connections", "Inspect cached connection observations", run_from_palette=True),
    SlashCommand("/model", "Choose the planning model", takes_value=True),
    SlashCommand("/effort", "Set provider reasoning effort", takes_value=True),
    SlashCommand("/grill", "Turn a goal into a gated plan", takes_value=True),
    SlashCommand("/propose", "Request one reviewed no-tools model proposal", takes_value=True),
    SlashCommand("/draft", "Review and confirm a goal-creation envelope", takes_value=True),
    SlashCommand("/review", "Review exact proposed commands and oracle coverage", takes_value=True),
    SlashCommand("/revise", "Review, apply or recover an exact stopped-run amendment", takes_value=True),
    SlashCommand("/import", "Review an existing executable runbook", takes_value=True),
    SlashCommand("/plan", "Inspect the exact candidate plan", run_from_palette=True),
    SlashCommand("/approve", "Confirm the exact visible plan", takes_value=True),
    SlashCommand("/accept", "Review or accept the exact final outcome", takes_value=True),
    SlashCommand("/gate", "Review or approve a waiting state gate", takes_value=True),
    SlashCommand("/run", "Review the exact provider launch or resume a run", takes_value=True),
    SlashCommand("/status", "Inspect session and supervisor", run_from_palette=True),
    SlashCommand("/boxes", "List the N-box worker pool", run_from_palette=True),
    SlashCommand("/overview", "See boxes, dependencies and attention together", run_from_palette=True),
    SlashCommand("/switch", "Search and switch read-only box panes", takes_value=True, run_from_palette=True),
    SlashCommand("/pin", "Pin/unpin an exact box; no arguments lists pins", takes_value=True, run_from_palette=True),
    SlashCommand("/group", "Organize an exact box under a display-only group", takes_value=True, run_from_palette=True),
    SlashCommand("/inbox", "Inspect box messages and consumption receipts", takes_value=True, run_from_palette=True),
    SlashCommand("/message", "Send data to an exact box; retry a saved request explicitly", takes_value=True),
    SlashCommand("/reply", "Reply to an exact message's original worker lease", takes_value=True),
    SlashCommand("/outbox", "Inspect saved sends and uncertain outcomes", takes_value=True, run_from_palette=True),
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
  /grill --draft GOAL      guided V5 planning without hand-written JSON
  /draft [confirm DIGEST]  inspect/confirm planning boundaries, not execution
  /propose                one no-tools model draft after envelope confirmation
  /review [DIGEST]         inspect/acknowledge draft risks before exact approval
  /revise --from RUNBOOK --reason TEXT [--effects POLICY.json]
                            review a stopped-run amendment; does not execute work
  /revise [apply DIGEST|recover]
                            inspect, approve, or recover the exact linked successor
  /propose --from SEED.json GOAL
                            one disclosed no-tools invocation; unapproved V5/V6 seed refinement
  /import PATH             import an exact runbook, bound to this checkout revision
  /plan                    show the full candidate plan and exact digest
  /approve yes|DIGEST      approve only that visible plan
  /accept [DIGEST]         review final outcome, then accept its exact digest
  /gate TASK [DIGEST]      review a pending state gate, then approve its digest
  /run [--preflight-cents N] review exact provider launch; no provider calls
  /run --accept-launch DIGEST [--accept-spend] [--preflight-cents N]
                            acknowledge the manifest; hosted profiles require spend consent
  /status                  current local session and supervisor state
  /boxes                   list the arbitrary-N worker pool
  /switch [WORDS]          searchable box picker; Alt+B in the TUI
  /pin [BOX [on|off]]      persist exact-box shortcut; default on
  /group [BOX NAME]        assign a display group; BOX --clear removes it
  /inbox [BOX [OFFSET]]    inspect lease-scoped data messages
  /message BOX TEXT       observe and send; never changes task authority
  /message retry ID       explicitly resend the exact saved request
  /reply MESSAGE_ID TEXT  reply only to the original worker lease
  /outbox [REQUEST_ID]    inspect durable send/acceptance records
  /overview [--attention] [--json] [--offset N] [--limit N]
                            inspect boxes and task dependencies without starting work
  /box ID|NUMBER           inspect one box's tasks, commands, evidence, and events
  /box ID VIEW             status | context | tools | diff | evals | events | evidence | transcript
  /model SELECTION         manual | claude[:MODEL] | codex[:MODEL] | local:MODEL
  /effort LEVEL            low | medium | high | xhigh | max
  /login [claude|codex]    choose an account with arrows, or name it directly
  /connections             cached observations; no probes or task-readiness claim
  /connections refresh [all|claude|codex|local|openai]
                            explicit bounded status/catalog refresh; no model call
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
    if isinstance(value, dict) and value.get("schema_version") == 5:
        from .revision_ui import validate_revision_envelope
        return validate_revision_envelope(value)
    fields = {
        "schema", "schema_version", "proposal", "run_id", "execution_status",
        "execution_limitation", "runbook",
    }
    if isinstance(value, dict) and value.get("schema_version") in {2, 3, 4}:
        fields.add("source")
        if value["schema_version"] in {3, 4}:
            fields.add("origin")
    if not isinstance(value, dict) or set(value) != fields:
        raise InteractiveError("product plan has the wrong fields")
    if value["schema"] != "camol.product_plan" or type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2, 3, 4}:
        raise InteractiveError("product plan schema is unsupported")
    proposal = validate_proposal(value["proposal"]) if value["proposal"] is not None else None
    if proposal is None and (value["schema_version"] not in {2, 3, 4} or value["runbook"] is None):
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
    if value["schema_version"] == 4:
        from .schema import require_digest, require_identifier
        origin = value["origin"]
        if not isinstance(origin, dict) or set(origin) != {"kind", "creation_envelope", "reviewed_envelope_digest", "coverage", "request_digest", "response_digest", "planning_call_id"}:
            raise InteractiveError("creation proposal origin has missing or unknown fields")
        envelope = draft_creation.validate_creation_envelope(origin["creation_envelope"])
        if (origin["kind"] != "goal_creation_proposal" or origin["reviewed_envelope_digest"] != canonical_digest(envelope)
                or value["source"] != envelope["source"]):
            raise InteractiveError("creation proposal does not bind the exact reviewed envelope/source")
        for name in ("reviewed_envelope_digest", "request_digest", "response_digest"):
            require_digest(origin[name], name)
        require_identifier(origin["planning_call_id"], "planning_call_id")
        checked = draft_creation.parse_creation_response(json.dumps(dict(runbook=value["runbook"], coverage=origin["coverage"], unresolved_questions=[])), envelope)
        if "runbook" not in checked:
            raise InteractiveError("creation proposal has unresolved oracle coverage")
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
            "{} PLAN {}".format("LINKED REVISION" if plan["schema_version"] == 5 else "IMPORTED", digest),
            "goal: " + runbook["run"]["objective"],
            "workspace: " + plan["source"]["workspace"],
            "frozen source revision: " + plan["source"]["revision"],
            "kernel runbook digest: " + runbook_digest(runbook),
            "This exact executable contract defines agents, tasks, commands, verification, authority and budgets.",
            "limitation: " + plan["execution_limitation"],
            *( ["Model-generated candidate: all state gates remain human; origin=" + json.dumps(plan["origin"], sort_keys=True)] if plan["schema_version"] in {3, 4} else [] ),
            "canonical product plan JSON:", json.dumps(plan, indent=2, sort_keys=True),
            ("This linked successor was approved by its exact revision review. /run is a separate action; this display does not prove execution or readiness."
             if plan["schema_version"] == 5 else "Nothing has started. Review every command, grant and mapping, then /approve " + digest
             if plan["schema_version"] in {3, 4} else
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
        self._closed_event = threading.Event()
        self.connection_refresh_active = threading.Event()
        self._box_cache = {}

    def cancel_active(self) -> None:
        self._cancel_event.set()

    def close_client(self) -> None:
        self._closed_event.set()
        self.cancel_active()

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
        if self._closed_event.is_set():
            return CommandResponse(messages=("Client detached; this controller cannot start another request.",))
        if raw.strip() == "/cancel":
            self.cancel_active()
            return CommandResponse(messages=("Client request cancellation requested. Authoritative worker execution is unchanged.",))
        if not self._command_lock.acquire(blocking=False):
            return CommandResponse(messages=("A request is still running. Use /cancel, or wait before submitting another command.",))
        try:
            with self.store.transaction():
                self.session = self.store.load()
                if self._closed_event.is_set():
                    return CommandResponse(messages=("Client detached; queued request was not started.",))
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
                if draft_creation.is_creation(self.session["grill"]):
                    if self.session["grill"]["phase"] in {"questioning", "clarifying"}:
                        return self._answer_creation(text)
                else:
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
            if arguments[0] == "--draft":
                if len(arguments) < 2:
                    raise InteractiveError("usage: /grill --draft GOAL")
                require_tool_free_provider(parse_selection(self.session["model"]))
                from .debug_execution import source_identity
                goal = " ".join(arguments[1:])
                state = draft_creation.creation_state(goal, source_identity(self.workspace), getpass.getuser(),
                    self.session["model"], self.session["effort"], "run-" + uuid4().hex[:16])
                self.session = self.store.update(self.session, goal=goal, grill=state, plan=None, plan_digest=None,
                    approved_digest=None, run_id=None, selected_box=None, event_cursor=0, status="planning",
                    state_dir=str(self.store.runs_dir / ("run-" + uuid4().hex)))
                return self._respond("DRAFT GRILL 1/{} — {}\nNo model call or worker execution has started.".format(len(draft_creation.QUESTIONS), draft_creation.question(state)))
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
            if not arguments:
                return self._propose_creation(on_chunk=on_chunk)
            return self._propose(arguments, on_chunk=on_chunk)
        if command == "/draft":
            return self._draft(arguments)
        if command == "/review":
            if (self.session.get("plan") or {}).get("schema_version") == 5:
                if arguments:
                    raise InteractiveError("linked revisions are already approved through /revise apply; /review only displays them")
                return self._respond(render_plan(self.session["plan"], self.session["plan_digest"]))
            return self._review_creation(arguments)
        if command == "/revise":
            return self._revise(arguments)
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
            return self._connections(arguments, on_chunk=on_chunk)
        if command == "/skills":
            if arguments:
                raise InteractiveError("usage: /skills")
            return CommandResponse(messages=(BUILTIN_PROTOCOLS,))
        if command == "/history":
            return self._history(arguments)
        if command == "/overview":
            return self._overview(arguments)
        if command == "/switch":
            return self._switch(arguments)
        if command in {"/pin", "/group"}:
            return self._organize_panes(command, arguments)
        if command in {"/inbox", "/message", "/reply", "/outbox"}:
            from .mailbox_ui import command as mailbox_command
            return mailbox_command(self, command, arguments)
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
            return self._run(arguments, on_chunk=on_chunk)
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
        if self.session["plan"] is not None or draft_creation.is_creation(self.session.get("grill")):
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
        profiles_for_runbook(runbook)
        limitation = ("Local process adapters use the exact imported executable protocol. No hosted-model proof is claimed."
                      if kinds == {"process"} else
                      "Provider execution requires /run to review the exact launch digest, trust limitations and separate preflight/worker envelopes. "
                      "Claude profiles require fresh capability receipts. Codex quota/resolved-model/hard USD/inner-turn/egress limits remain unsupported. "
                      "Process, Claude, Codex and local OSS profiles may be combined; exact kernel readiness still gates every lease.")
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
                "execution_limitation": "Seed-assisted model proposal, not verified evidence. Every task/state gate and final acceptance requires human review. Provider launch needs its exact manifest acknowledgement; existing provider limitations and exact readiness remain in force.",
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

    def _answer_creation(self, text: str) -> CommandResponse:
        state = draft_creation.answer(self.session["grill"], text, self.workspace)
        self.session = self.store.update(self.session, grill=state)
        question = draft_creation.question(state)
        if question:
            return self._respond("DRAFT QUESTION — " + question)
        return self._respond(draft_creation.render_envelope(state["envelope"]))

    def _draft(self, arguments: Sequence[str]) -> CommandResponse:
        state = draft_creation.validate_state(self.session["grill"])
        if state["envelope"] is None:
            if arguments:
                raise InteractiveError("finish the draft questions before confirming an envelope")
            return self._respond("DRAFT QUESTION — " + draft_creation.question(state))
        if not arguments:
            return self._respond(draft_creation.render_envelope(state["envelope"]))
        digest = canonical_digest(state["envelope"])
        if arguments != ["confirm", digest] or state["phase"] not in {"review", "confirmed"}:
            raise InteractiveError("review every boundary, then /draft confirm " + digest)
        from .source_binding import assert_source
        assert_source(state["source"], self.workspace)
        if state["planning_model"] != self.session["model"] or state["effort"] != self.session["effort"]:
            raise InteractiveError("planning provider changed; start a new draft to review its exact identity")
        state.update(reviewed_digest=digest, phase="confirmed")
        self.session = self.store.update(self.session, grill=state)
        return self._respond("Creation envelope confirmed for planning only: " + digest +
            ". Nothing is approved to execute. Explicitly /propose to request one no-tools candidate; no call happens automatically.")

    def _propose_creation(self, *, on_chunk=None) -> CommandResponse:
        self._reconcile_session()
        if self.session["status"] == "running":
            raise InteractiveError("an unfinished run owns this session; drafts cannot amend it implicitly")
        if self.session["approved_digest"] is not None:
            raise InteractiveError("an approved candidate already exists; start a new /grill --draft to replace it explicitly")
        state = draft_creation.validate_state(self.session["grill"])
        if state["phase"] not in {"confirmed", "candidate"} or state["reviewed_digest"] != canonical_digest(state["envelope"]):
            raise InteractiveError("complete /grill --draft GOAL, review /draft, and confirm its exact digest before /propose")
        require_tool_free_provider(parse_selection(self.session["model"]))
        from .source_binding import assert_source
        assert_source(state["source"], self.workspace)
        if state["planning_model"] != self.session["model"] or state["effort"] != self.session["effort"]:
            raise InteractiveError("planning provider changed after creation-envelope review")
        envelope = state["envelope"]
        prompt = draft_creation.creation_prompt(envelope)
        call_id = "planning-" + uuid4().hex
        request = dict(message=prompt, history=planning_history(self.session["messages"][:-1]),
                       model=self.session["model"], effort=self.session["effort"], tool_policy="none")
        origin = dict(kind="goal_creation_proposal", creation_envelope=envelope,
                      reviewed_envelope_digest=state["reviewed_digest"], coverage=[],
                      request_digest=canonical_digest(request), response_digest=None, planning_call_id=call_id)
        notice = ("PROPOSE — one no-tools planning invocation using {}. The complete reviewed creation envelope and bounded dialogue are sent; no implicit repository upload. "
                  "120-second request deadline; internal request count/cost can be unknown. No worker launch or automatic retry. New commands remain UNAPPROVED proposals.").format(self.session["model"])
        self._persist_message("system", notice, kind="notice")
        if on_chunk:
            on_chunk(notice + "\n")
        self._persist_message("system", "CREATION REQUEST\n" + prompt, kind="notice")
        self.store.append_proposal_event(dict(type="CREATION_REQUESTED", origin=origin, request=request,
                                              observed_at=datetime.now(timezone.utc).isoformat()))
        outcome = "rejected"
        try:
            reply, _ = self._call_planning(prompt, request_kind="goal_creation", call_id=call_id,
                                           history=request["history"], no_tools=True)
            if not isinstance(reply.text, str):
                raise PlanningError("creation provider returned non-text output")
            origin["response_digest"] = "sha256:" + hashlib.sha256(reply.text.encode()).hexdigest()
            self._persist_message("orchestrator", "UNAPPROVED CREATION RESPONSE\n" + reply.text[:draft_creation.MAX_RESPONSE_CHARS], kind="notice")
            result = draft_creation.parse_creation_response(reply.text, envelope)
            assert_source(state["source"], self.workspace)
            if "questions" in result:
                outcome = "questions"
                state.update(phase="clarifying", questions=result["questions"], risk_reviewed_digest=None)
                self.session = self.store.update(self.session, grill=state)
                self._persist_message("orchestrator", "Draft questions:\n" + "\n".join(result["questions"]), kind="conversation")
                return self._respond("DRAFT QUESTIONS — no new candidate or approval. Answer each question; then review the changed envelope and explicitly /propose again.\n" + draft_creation.question(state))
            origin["coverage"] = result["coverage"]
            runbook = result["runbook"]
            kinds = {a["adapter"]["kind"] for a in runbook["agents"]}
            plan = validate_envelope(dict(schema="camol.product_plan", schema_version=4, proposal=None,
                run_id=runbook["run"]["id"], runbook=runbook, source=state["source"], origin=origin,
                execution_status="ready" if kinds == {"process"} else "preflight_required",
                execution_limitation="Goal-created UNAPPROVED V5 candidate. Oracle mappings and command risks are human-reviewed proposals, not measurements. Every state gate and final acceptance is human. Exact worker profiles and their weaker-network/isolation limits remain in force."))
            digest = canonical_digest(plan)
            state.update(phase="candidate", risk_reviewed_digest=None)
            rendered = render_plan(plan, digest)
            if len(rendered) > 200000:
                raise InteractiveError("candidate is too large for safe exact review; narrow the draft")
            self.session = self.store.update(self.session, plan=plan, plan_digest=digest, approved_digest=None,
                run_id=plan["run_id"], grill=state, state_dir=str(self.store.runs_dir / ("run-" + uuid4().hex)),
                selected_box=None, event_cursor=0, status="plan_ready")
            outcome = "candidate_ready"
            return self._respond("MODEL CANDIDATE — nothing has started. Use /review to challenge new commands, scope and oracle coverage; acknowledge its exact digest before /approve.", rendered)
        except ConversationCancelled:
            outcome = "cancelled"
            raise
        finally:
            self.store.append_proposal_event(dict(type="CREATION_FINISHED", outcome=outcome, origin=origin,
                                                  observed_at=datetime.now(timezone.utc).isoformat()))

    def _review_creation(self, arguments: Sequence[str]) -> CommandResponse:
        plan = self.session["plan"]
        if plan is None or plan.get("schema_version") != 4:
            raise InteractiveError("/review requires an unapproved goal-created candidate; use /plan for other contracts")
        plan = validate_envelope(plan)
        digest = self.session["plan_digest"]
        rendered = draft_creation.render_risk(plan, digest)
        if not arguments:
            return self._respond(rendered)
        if arguments != [digest] or self.session["status"] != "plan_ready":
            raise InteractiveError("risk review requires the exact unapproved /plan digest")
        self._verify_proposal_source(plan)
        state = draft_creation.validate_state(self.session["grill"])
        if state["phase"] != "candidate" or canonical_digest(state["envelope"]) != plan["origin"]["reviewed_envelope_digest"]:
            raise InteractiveError("draft boundaries changed; explicitly request and review a fresh candidate")
        state["risk_reviewed_digest"] = digest
        self.session = self.store.update(self.session, grill=state)
        return self._respond(rendered, "Exact command/scope/oracle review acknowledged; this is not proof of correctness or execution approval. Next /approve " + digest)

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
        if (self.session.get("plan") or {}).get("schema_version") == 5:
            raise InteractiveError("linked revisions require /revise apply with the exact review digest; /approve cannot replace revision authority")
        if self.session["plan"] is None or self.session["status"] != "plan_ready":
            raise InteractiveError("there is no unapproved plan ready for confirmation")
        if not arguments:
            if self.session["plan"]["schema_version"] in {3, 4}:
                return self._respond("Model proposal approval requires its exact digest. Review /plan, then /approve " + self.session["plan_digest"])
            return self._respond(
                "Approval required for {}. Review /plan, then type `/approve yes` or `/approve {}`.".format(
                    self.session["plan_digest"], self.session["plan_digest"]
                )
            )
        if len(arguments) != 1 or arguments[0] not in {"yes", self.session["plan_digest"]}:
            raise InteractiveError("approval must be `yes` or the exact plan digest")
        if self.session["plan"]["schema_version"] in {3, 4} and arguments[0] == "yes":
            raise InteractiveError("a model-generated proposal requires the exact /plan digest, not a bare yes")
        if self.session["plan"]["schema_version"] == 4:
            state = draft_creation.validate_state(self.session["grill"])
            if state["risk_reviewed_digest"] != self.session["plan_digest"]:
                raise InteractiveError("new command authority and oracle mappings require /review " + self.session["plan_digest"] + " before approval")
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

    def _connections(self, arguments=(), *, on_chunk=None) -> CommandResponse:
        if arguments:
            if (arguments[0] != "refresh" or len(arguments) > 2
                    or (len(arguments) == 2 and arguments[1] not in {"all", "claude", "codex", "local", "openai"})):
                raise InteractiveError("usage: /connections [refresh [all|claude|codex|local|openai]]")
            self.connection_refresh_active.set()
            notice = "Explicit connection refresh: bounded CLI version/auth status and/or loopback catalog only; no model or Docker call."
            self._persist_message("system", notice, kind="notice")
            if on_chunk:
                on_chunk(notice + "\n")
            try:
                records = self.connections.refresh(arguments[1] if len(arguments) == 2 else "all", cancel_event=self._cancel_event)
            finally:
                self.connection_refresh_active.clear()
        else:
            records = self.connections.load()
        lines = ["CONNECTION OBSERVATIONS — cached history, task readiness unverified; no filled readiness glyph without exact fresh kernel evidence."]
        for record in records:
            lines.append("{} {:<16} {:<14} {}".format(
                "!" if record["status"] in {"error", "auth_required"} else "□", record["connection_id"], observation_label(record), record["detail"]
            ))
        if not records:
            lines.append("No saved observations. Startup and this inspection do not probe connections.")
        lines.append("Use /connections refresh [TARGET] to observe again. Presence/authentication/catalog are separate from lease readiness.")
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

    def _revise(self, arguments: Sequence[str]) -> CommandResponse:
        from .json_contracts import load_contract
        from .revision_ui import RevisionUI, render_review
        service = RevisionUI(self.store)
        if not arguments:
            return replace(self._respond(render_review(service._inspect_locked(self.session))), focus_orchestrator=True)
        owner = getpass.getuser()
        if arguments[0] in {"apply", "recover"}:
            expected = 2 if arguments[0] == "apply" else 1
            if len(arguments) != expected:
                raise InteractiveError("usage: /revise apply REVIEW_DIGEST | /revise recover")
            if self._cancel_event.is_set():
                raise InteractiveError("revision client request cancelled before approval/handoff")
            if arguments[0] == "apply":
                self.session = service._apply_locked(self.session, arguments[1], owner=owner)
            else:
                self.session = service._recover_locked(self.session, owner=owner)
            response = self._respond("Revision session adopted exact run " + self.session["run_id"] +
                ". No supervisor, worker or provider request started. /run is separate.",
                render_plan(self.session["plan"], self.session["plan_digest"]))
            return replace(response, focus_orchestrator=True)
        remaining, options = list(arguments), {}
        while remaining:
            option = remaining.pop(0)
            if option not in {"--from", "--reason", "--effects"} or option in options or not remaining:
                raise InteractiveError("usage: /revise --from RUNBOOK --reason TEXT [--effects POLICY.json]")
            options[option] = remaining.pop(0)
        if not options.get("--from") or not options.get("--reason", "").strip():
            raise InteractiveError("revision requires an explicit successor runbook and reason; quote multiword reasons")
        effects = load_contract(options["--effects"], max_bytes=65536) if "--effects" in options else []
        if self._cancel_event.is_set():
            raise InteractiveError("revision review request cancelled")
        review = service._propose_locked(self.session, options["--from"],
            reason=options["--reason"], owner=owner, effect_reruns=effects)
        return replace(self._respond(render_review(review)), focus_orchestrator=True)

    def _overview(self, arguments: Sequence[str]) -> CommandResponse:
        from .overview import fleet_overview, render_overview
        options = dict(attention=False, offset=0, limit=50)
        as_json, remaining, seen = False, list(arguments), set()
        while remaining:
            option = remaining.pop(0)
            if option in seen:
                raise InteractiveError("duplicate overview option")
            seen.add(option)
            if option == "--json":
                as_json = True
            elif option == "--attention":
                options["attention"] = True
            elif option in {"--offset", "--limit"} and remaining:
                value = remaining.pop(0)
                if not value.isascii() or not value.isdecimal() or len(value) > 9:
                    raise InteractiveError("overview pagination must use bounded nonnegative integers")
                options[option[2:]] = int(value)
            else:
                raise InteractiveError("usage: /overview [--attention] [--json] [--offset N] [--limit N]")
        state, basis = self._pane_state()
        report = fleet_overview(state, basis=basis, **options)
        return CommandResponse(messages=(json.dumps(report, sort_keys=True, indent=2) if as_json else render_overview(report),))

    def _pane_state(self):
        """One exact ledger cut, or explicitly unstarted plan; no live probes."""
        from .orchestrator import Orchestrator
        from .overview import planned_state
        database = SupervisorPaths.under(Path(self.session["state_dir"])).database
        if database.exists() or database.is_symlink():
            if not self.session["run_id"]:
                raise InteractiveError("existing ledger has no exact selected session run")
            store = None
            try:
                store = ReadOnlyEventStore(database)
                state = Orchestrator(store).state(self.session["run_id"])
                if state["run_id"] != self.session["run_id"]:
                    raise InteractiveError("selected run is absent from its ledger")
                plan = self.session.get("plan")
                if plan and plan.get("runbook") and state["plan_digest"] != runbook_digest(plan["runbook"]):
                    raise InteractiveError("pane ledger differs from the selected plan")
                return state, "ledger_snapshot"
            except sqlite3.Error as error:
                raise InteractiveError("overview ledger is unreadable; snapshot unavailable") from error
            finally:
                if store is not None:
                    store.close()
        else:
            plan = self.session.get("plan")
            if not plan or not plan.get("runbook"):
                raise InteractiveError("no executable plan yet; use /grill or /import first")
            state = planned_state(plan["runbook"])
            if state["run_id"] != self.session["run_id"]:
                raise InteractiveError("pane plan differs from the selected run")
            return state, "plan_only"

    def _switch(self, arguments):
        from .pane_switcher import switch_snapshot, filter_rows, row_label, pane_scope
        from .pane_organization import load
        state, basis = self._pane_state()
        organization = load(self.store.project_dir / "pane-organization.json", pane_scope(self.session, state))
        snapshot = switch_snapshot(self.session, state, basis, organization)
        if arguments and arguments[0] == "--select":
            if len(arguments) != 4 or arguments[2] != "--scope":
                raise InteractiveError("usage: /switch --select KEY --scope DIGEST")
            if arguments[3] != snapshot["scope"]:
                raise InteractiveError("box picker scope changed; reopen /switch before selecting")
            row = next((row for row in snapshot["rows"] if row["key"] == arguments[1]), None)
            if row is None:
                raise InteractiveError("selected box no longer exists")
            target = row["box_id"]
            self.session = self.store.update(self.session, selected_box=target)
            if target is None:
                return CommandResponse(messages=("Orchestrator selected; worker execution is unchanged.",), focus_orchestrator=True)
            return CommandResponse(messages=(self.inspect_box(target, "events"),), box_id=target, box_view="events")
        query = " ".join(arguments)
        if any(word.startswith("--") for word in arguments):
            raise InteractiveError("usage: /switch [SEARCH WORDS]")
        matched = filter_rows(snapshot["rows"], query)
        snapshot["query"] = query
        lines = ["BOX SWITCHER | run={} | {} | cursor={}".format(snapshot["run_id"], basis, snapshot["event_cursor"]),
                 "Read-only navigation, not live connectivity or readiness. {} matches.".format(len(matched))]
        for row in matched[:50]:
            lines.append(row_label(row))
        if len(matched) > 50:
            lines.append("Showing first 50; refine search or use the paginated TUI picker.")
        lines.append("TUI: arrows/Enter select; Escape returns to composer. Line mode: /box EXACT_ID.")
        return CommandResponse(messages=("\n".join(lines),), switcher=snapshot)

    def _organize_panes(self, command, arguments):
        from .pane_switcher import pane_scope
        from .pane_organization import load, save, update
        state, basis = self._pane_state()
        scope = pane_scope(self.session, state)
        path = self.store.project_dir / "pane-organization.json"
        preferences = load(path, scope)
        if arguments:
            target = arguments[0]
            if target not in state["agents"]:
                raise InteractiveError("pane organization requires an exact box ID, not a number or prefix")
            if command == "/pin":
                if len(arguments) > 2 or (len(arguments) == 2 and arguments[1] not in {"on", "off"}):
                    raise InteractiveError("usage: /pin [BOX [on|off]]")
                preferences = update(preferences, target, pinned=len(arguments) == 1 or arguments[1] == "on")
            else:
                if len(arguments) < 2:
                    raise InteractiveError("usage: /group [BOX NAME|BOX --clear]")
                if arguments[1:] == ["--clear"]:
                    preferences = update(preferences, target, clear_group=True)
                elif any(part.startswith("--") for part in arguments[1:]):
                    raise InteractiveError("usage: /group [BOX NAME|BOX --clear]")
                else:
                    preferences = update(preferences, target, group=" ".join(arguments[1:]))
            # handle() holds the shared project transaction across reload/edit/save.
            save(path, preferences)
        lines = ["PANE ORGANIZATION | run={} | {} | scope={}".format(state["run_id"], basis, scope),
                 "Display only: task scope, leases, approvals and budgets are unchanged.",
                 "Pins (in shortcut order): " + (", ".join(preferences["pins"]) or "none")]
        lines.extend("{} → {}".format(box, group) for box, group in sorted(preferences["groups"].items()))
        return self._respond("\n".join(lines))

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

    def _launch_manifest(self, plan, preflight_cents=10):
        from .debug_execution import source_identity
        self._verify_proposal_source(plan)
        state_dir = Path(self.session["state_dir"])
        WorkspaceManager(self.workspace, state_dir).assert_source_ready()
        return build_manifest(plan, self.session["plan_digest"], source_identity(self.workspace),
                              state_dir, local_target_id(), preflight_cents=preflight_cents)

    def _run(self, arguments: Sequence[str], *, on_chunk=None) -> CommandResponse:
        if self.session["approved_digest"] is None or self.session["approved_digest"] != self.session["plan_digest"]:
            raise InteractiveError("run requires the exact current plan to be approved")
        plan = validate_envelope(self.session["plan"])
        if plan["runbook"] is None:
            raise InteractiveError(plan["execution_limitation"])
        kinds = {agent["adapter"]["kind"] for agent in plan["runbook"]["agents"]}
        if kinds == {"process"}:
            # Preserve the existing unpaid process workflow. It has no provider
            # request, preflight or hosted spending acknowledgement to coordinate.
            return self._run_process(arguments)
        state_dir = Path(self.session["state_dir"])
        if SupervisorPaths.under(state_dir).socket.exists():
            try:
                remote_plan, remote_status = self._control("plan"), self._control("status")
            except (SupervisorError, OSError):
                pass
            else:
                if remote_plan["plan_digest"] != runbook_digest(plan["runbook"]):
                    raise InteractiveError("another supervisor owns this session state with a different plan")
                if plan["schema_version"] in {3, 4, 5} and (remote_plan.get("source_binding") or {}).get("source") != plan["source"]:
                    raise InteractiveError("supervisor source binding differs from the exact approved proposal; reattach refused")
                self.session = self.store.update(self.session, status="terminal" if remote_status["run"]["status"] in {"completed", "blocked"} else "running")
                return self._respond("Reattached to the existing supervisor without a new provider request or preflight.",
                    "Supervisor reattached pid={}; closing this client will not stop it.".format(remote_status["pid"]))
        options = {"preflight_cents": 10, "accept_launch": None, "accept_spend": False, "draft_policy": None}
        remaining, seen = list(arguments), set()
        names = {"--accept-launch": "accept_launch", "--preflight-cents": "preflight_cents", "--accept-draft-policy": "draft_policy"}
        while remaining:
            option = remaining.pop(0)
            if option in seen:
                raise InteractiveError("duplicate launch option: " + option)
            seen.add(option)
            if option == "--accept-spend":
                options["accept_spend"] = True
            elif option in names and remaining:
                value = remaining.pop(0)
                if option == "--preflight-cents":
                    if not value.isascii() or not value.isdecimal() or len(value) > 3:
                        raise InteractiveError("--preflight-cents must be an integer between 1 and 100")
                    value = int(value)
                options[names[option]] = value
            else:
                raise InteractiveError("provider launches now require exact launch review; use /run [--preflight-cents N], then /run --accept-launch DIGEST [--accept-spend]")
        if plan["schema_version"] == 4:
            state = draft_creation.validate_state(self.session["grill"])
            if state["risk_reviewed_digest"] != self.session["plan_digest"]:
                raise InteractiveError("creation candidate needs its exact risk/scope review before launch")
        manifest = self._launch_manifest(plan, options["preflight_cents"])
        digest = canonical_digest(manifest)
        command = "/run --accept-launch " + digest
        if manifest["hosted_spend_ack_required"]:
            command += " --accept-spend"
        command += " --preflight-cents " + str(options["preflight_cents"])
        draft_digest = manifest["additional_acknowledgements"].get("draft_policy_digest")
        if draft_digest:
            command += " --accept-draft-policy " + draft_digest
        if options["accept_launch"] is None:
            if options["accept_spend"] or options["draft_policy"] is not None:
                raise InteractiveError("spend or policy acknowledgement alone cannot start work; first review /run, then accept its exact launch digest")
            rendered = json.dumps(manifest, indent=2, sort_keys=True)
            if len(rendered) > 200_000:
                raise InteractiveError("the exact launch is too large to review in this terminal")
            return self._respond("LAUNCH REVIEW — no provider probe, model call or worker started.\n" + rendered,
                                 "launch digest=" + digest + "\nAfter reviewing the separate requested preflight reservation and common worker envelope, type:\n" + command)
        if options["accept_launch"] != digest:
            raise InteractiveError("launch policy requires the exact current acknowledgement digest; review /run again")
        if options["accept_spend"] != manifest["hosted_spend_ack_required"]:
            raise InteractiveError("hosted launch requires --accept-spend; a local-only launch must not imply hosted billing approval")
        if options["draft_policy"] != draft_digest:
            raise InteractiveError("this worker tier cannot enforce hard no-egress/host-write boundaries; --accept-draft-policy must name the exact reviewed product-plan digest")
        self._persist_message("system", "Launch manifest acknowledged: " + digest, kind="notice")
        assert_preflight_clear(state_dir)
        profiles = profiles_for_runbook(plan["runbook"])
        lines = []
        for index, request in enumerate(manifest["preflights"], 1):
            if self._cancel_event.is_set():
                raise InteractiveError("launch cancelled; completed preflight observations are retained, no automatic retry")
            assert_preflight_clear(state_dir)
            profile = profiles[request["profile_digest"]]
            receipt = read_capability(state_dir, profile)
            reused = receipt is not None and receipt.valid_for(profile, request["target_id"], datetime.now(timezone.utc))[0]
            if not reused:
                if on_chunk:
                    on_chunk("Preflight {}/{}: profile={} operation={} requested ceiling={} cents. No worker has started.\n".format(
                        index, len(manifest["preflights"]), profile.profile_id, request["operation_id"], request["max_requested_usd_cents"]))
                receipt = self.preflight_fn(profile, target_id=request["target_id"], state_dir=state_dir,
                    cwd=self.workspace, accept_spend=True, spend_ceiling_cents=request["max_requested_usd_cents"],
                    operation_id=request["operation_id"], cancel_event=self._cancel_event)
                if not receipt.valid_for(profile, request["target_id"], datetime.now(timezone.utc))[0]:
                    raise InteractiveError("provider preflight returned a stale or mismatched receipt; no workers launched")
            detail = ("Fresh capability reused; additional preflight charge=0" if reused else
                      "Preflight operation observed; cost_usd_micros=" + str(receipt.cost_usd_micros))
            line = "{} profile={} target={} receipt={}; original operation/usage stays retained.".format(detail, profile.profile_id, request["target_id"], receipt.digest())
            lines.append(line)
            self._persist_message("system", line, kind="notice")
            if on_chunk:
                on_chunk(line + "\n")
        # A slow later profile cannot let an earlier expired receipt through.
        assert_preflight_clear(state_dir)
        for request in manifest["preflights"]:
            profile = profiles[request["profile_digest"]]
            receipt = read_capability(state_dir, profile)
            if receipt is None or not receipt.valid_for(profile, request["target_id"], datetime.now(timezone.utc))[0]:
                raise InteractiveError("all Claude profiles need exact fresh persisted capabilities; review stale/failed operations explicitly with camol preflight-status, no automatic renewal")
        if self._cancel_event.is_set():
            raise InteractiveError("launch cancelled; no workers launched, preflight accounting retained")
        if self._launch_manifest(plan, options["preflight_cents"]) != manifest:
            raise InteractiveError("launch source or target changed during preflight; review the exact launch again")
        self._assert_launch_not_cancelled()
        runbook_path = self.store.write_runbook(self.session, plan["runbook"])
        self._assert_launch_not_cancelled()
        started = self.spawn_fn(runbook_path, self.workspace, state_dir, approve_by=getpass.getuser(), expected_source=manifest["source"])
        self.session = self.store.update(self.session, status="running")
        final = self._respond("Launch accepted: " + digest + ". Exact kernel readiness still gates every lease; Codex/local unknown dimensions remain explicitly unverified.",
            "Supervisor {} pid={}; closing this client will not stop it.".format("reattached" if not started.get("started", True) else "detached", started["pid"]))
        return CommandResponse(messages=tuple(lines) + final.messages)

    def _assert_launch_not_cancelled(self):
        if self._cancel_event.is_set() or self._closed_event.is_set():
            raise InteractiveError("launch cancelled before supervisor dispatch; no workers launched")

    def _run_process(self, arguments: Sequence[str]) -> CommandResponse:
        if {agent["adapter"]["kind"] for agent in self.session["plan"]["runbook"]["agents"]} != {"process"}:
            raise InteractiveError("provider workers require the coordinated exact launch review")
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
                if plan["schema_version"] in {3, 4, 5} and (remote_plan.get("source_binding") or {}).get("source") != plan["source"]:
                    raise InteractiveError("supervisor source binding differs from the exact approved proposal; reattach refused")
                session_status = "terminal" if remote_status["run"]["status"] in {"completed", "blocked"} else "running"
                self.session = self.store.update(self.session, status=session_status)
                return self._respond(
                    "Reattached to the existing supervisor without a new provider request or preflight.",
                    "Supervisor reattached pid={}; closing this client will not stop it.".format(remote_status["pid"]),
                )
        if plan["schema_version"] == 4:
            state = draft_creation.validate_state(self.session["grill"])
            if state["risk_reviewed_digest"] != self.session["plan_digest"]:
                raise InteractiveError("creation candidate needs its exact risk/scope review before launch")
            scope = plan["origin"]["creation_envelope"]["scope_policy"]
            if scope["enforcement"] != "os_scoped":
                remaining = list(arguments)
                try:
                    index = remaining.index("--accept-draft-policy")
                except ValueError:
                    raise InteractiveError("this worker tier cannot enforce hard no-egress/host-write boundaries. Explicitly acknowledge /run --accept-draft-policy " + self.session["plan_digest"] + " plus the existing provider flags")
                if index + 1 >= len(remaining) or remaining[index + 1] != self.session["plan_digest"]:
                    raise InteractiveError("--accept-draft-policy must name the exact reviewed product-plan digest")
                del remaining[index:index + 2]
                arguments = remaining
        self._verify_proposal_source(plan)
        workspace = WorkspaceManager(self.workspace, state_dir)
        workspace.assert_source_ready()
        if plan.get("source") is not None and (
            plan["source"]["workspace"] != str(self.workspace)
            or plan["source"]["revision"] != workspace.head_revision()
        ):
            raise InteractiveError("source checkout changed after import; import and approve the current revision again")
        state_dir.mkdir(parents=True, exist_ok=True)
        if arguments:
            raise InteractiveError("a local process plan uses `/run` without provider-spend flags")
        readiness_line = "Run accepted for the explicitly approved local process adapter; no hosted-model proof is claimed."
        self._assert_launch_not_cancelled()
        runbook_path = self.store.write_runbook(self.session, plan["runbook"])
        self._assert_launch_not_cancelled()
        launch_options = {"expected_source": plan["source"]} if plan["schema_version"] in {3, 4, 5} else {}
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


    def _verify_proposal_source(self, plan: Mapping[str, Any]) -> None:
        if plan.get("schema_version") in {3, 4, 5}:
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
        from .pane_switcher import pane_scope
        from .pane_organization import load, PaneOrganizationError
        boxes = self._box_summaries()
        plan = self.session.get("plan")
        if not boxes or not plan or not plan.get("runbook"):
            return boxes
        scope = pane_scope(self.session, dict(plan_digest=runbook_digest(plan["runbook"]), agents={box["box_id"]: {} for box in boxes}))
        try:
            preferences = load(self.store.project_dir / "pane-organization.json", scope)
        except PaneOrganizationError:
            return [dict(box, pinned=False, custom_group="", organization_error="pane preferences unavailable; /pin reports the error") for box in boxes]
        order = {box: index for index, box in enumerate(preferences["pins"])}
        for box in boxes:
            box.update(pinned=box["box_id"] in order, custom_group=preferences["groups"].get(box["box_id"], ""))
        if order or preferences["groups"]:
            boxes.sort(key=lambda box: (order.get(box["box_id"], 1001), not bool(box["custom_group"]), box["custom_group"].casefold(), box["box_id"]))
        return boxes

    def _box_summaries(self) -> List[Dict[str, str]]:
        try:
            status = self._control("status")
        except (SupervisorError, OSError):
            from .box_inspection import BoxInspector, BoxInspectionError
            database = SupervisorPaths.under(Path(self.session["state_dir"])).database
            if database.exists() or database.is_symlink():
                try:
                    report = BoxInspector(Path(self.session["state_dir"]), database=database).list(self.session["run_id"])
                    self._check_box_subject(report)
                except (BoxInspectionError, InteractiveError, ValueError, RuntimeError, OSError):
                    return [dict(box, status="unavailable", connected="no", basis="invalid_ledger") for box in self._planned_boxes()]
                return [dict(box, task_id=box["task_id"] or "unassigned", connected="no", basis="retained_ledger")
                        for box in report["boxes"]]
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
                "runtime_wait": task.get("runtime_wait", {}).get("code"),
                "connected": "yes" if agent_id in workspace_by_box else "no",
            })
        return boxes

    def _boxes(self) -> CommandResponse:
        boxes = self.box_summaries()
        if not boxes:
            return self._respond("No boxes exist yet. Complete /grill to derive the N-box pool.")
        attention = {"blocked", "waiting"}
        lines = ["BOXES — ■ recorded workspace, □ no live connection proven, ! attention"]
        if any(box.get("organization_error") for box in boxes):
            lines.append("WARNING: pane preferences unavailable; showing unorganized recorded boxes. No preferences were replaced.")
        for index, box in enumerate(boxes, 1):
            mark = "!" if box["status"] in attention | {"unavailable"} or box.get("runtime_wait") else "■" if box.get("connected") == "yes" else "□"
            lines.append("{} {:>2} {:<18} {:<12} {}{}".format(mark, index, box["box_id"], box["status"], box["task_id"],
                " [" + box["basis"] + "]" if box.get("basis") else ""))
            if box.get("pinned") or box.get("custom_group"):
                lines.append("     {}{}".format("★ pinned " if box.get("pinned") else "", box.get("custom_group", "")))
            if box.get("runtime_wait"):
                lines.append("     runtime wait: " + box["runtime_wait"])
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
        if subview not in {"status", "context", "tools", "diff", "evals", "events", "evidence", "transcript", "inbox"}:
            raise InteractiveError("unknown box view; use status, context, tools, diff, evals, events, evidence, transcript, or inbox")
        self.session = self.store.update(self.session, selected_box=target)
        rendered = self.inspect_box(target, subview)
        return CommandResponse(messages=(rendered,), box_id=target, box_view=subview)

    def _check_box_subject(self, view):
        if self.session.get("run_id") and view.get("run_id") != self.session["run_id"]:
            raise InteractiveError("box snapshot differs from the selected run")
        plan = self.session.get("plan")
        if plan and plan.get("runbook") and view.get("plan_digest") != runbook_digest(plan["runbook"]):
            raise InteractiveError("box snapshot differs from the selected frozen plan")

    def inspect_box(self, target: str, subview: str = "events") -> str:
        """Read a box snapshot without changing authority or writing chat history."""
        if subview == "inbox":
            from .mailbox_ui import inbox_view
            try:
                result = inbox_view(self, target)
                return "BOX {} / inbox — LIVE controller snapshot; messages are data, not task success\n{}".format(target, json.dumps(result, indent=2, sort_keys=True))
            except (SupervisorError, OSError):
                from .box_inspection import BoxInspector
                try:
                    view = BoxInspector(Path(self.session["state_dir"])).read(self.session["run_id"], target, previews=False)
                    self._check_box_subject(view)
                    return "BOX {} / inbox — RETAINED event cut, not live delivery proof\n{}".format(target, json.dumps(view.get("mailbox", {"messages": []}), indent=2, sort_keys=True))
                except (ValueError, RuntimeError, OSError) as error:
                    return "BOX {} / inbox unavailable: {}. No cached or fabricated messages substituted.".format(target, error)
            except ValueError as error:
                return "BOX {} / inbox unavailable: {}. Invalid live response; no retained snapshot substituted.".format(target, error)
        stale = False
        retained = False
        scope = (self.session["session_id"], self.session["state_dir"], self.session.get("run_id"), self.session.get("plan_digest"), target)
        try:
            view = self._control(
                "box", {"box_id": target, "after_seq": 0, "limit": 200, "tail": True}
            )
            self._check_box_subject(view)
            if view.get("box_id", target) != target:
                raise InteractiveError("box snapshot differs from the exact selected box")
            self._box_cache[scope] = view
        except (SupervisorError, OSError):
            from .box_inspection import BoxInspector, BoxInspectionError
            database = SupervisorPaths.under(Path(self.session["state_dir"])).database
            view = None
            if database.exists() or database.is_symlink():
                try:
                    view = BoxInspector(Path(self.session["state_dir"]), database=database).read(
                        self.session["run_id"], target, limit=200, tail=True)
                    self._check_box_subject(view)
                    retained = True
                except (BoxInspectionError, InteractiveError, ValueError, RuntimeError, OSError):
                    return "BOX {} unavailable: exact retained ledger is invalid or mismatched; no cached evidence substituted.".format(target)
            else:
                view = self._box_cache.get(scope)
                stale = view is not None
            if view is None:
                return "BOX {} is dormant or unavailable; no live evidence snapshot is available.".format(target)
        lines = [
            "BOX {} / {} {}adapter={} tasks={} workspace={}".format(
                target, subview, "RETAINED — supervisor disconnected; " if retained else "STALE — supervisor disconnected; " if stale else "",
                view["adapter_kind"], ",".join(view["task_ids"]) or "none",
                view["workspace"]["path"] if view["workspace"] else "not prepared",
            ),
            "Read-only snapshot; messages in the composer always go to the orchestrator.",
        ]
        if view.get("observation"):
            lines.append("Ledger cursor={}; inspection does not prove live connection or task readiness.".format(view["observation"]["event_cursor"]))
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
