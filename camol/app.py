"""Product-V0 command engine shared by the Textual and line-mode clients."""

import asyncio
import getpass
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from .connections import ConnectionError, ConnectionRegistry
from .conversation import ConversationError, converse, parse_selection
from .planning import (
    GrillState,
    PlanningError,
    compile_runbook,
    proposal_from_grill,
    validate_proposal,
)
from .probes import local_target_id
from .providers import ProviderError, create_claude_capability, load_model_profile
from .runbook import RunbookError, runbook_digest, validate_runbook
from .schema import canonical_digest
from .session import EFFORTS, SessionError, SessionStore
from .supervisor import SupervisorError, SupervisorPaths, send_control_v2, spawn_supervisor
from .workspace import WorkspaceError, WorkspaceManager


class InteractiveError(RuntimeError):
    """A user-facing interactive transition was denied."""


@dataclass(frozen=True)
class CommandResponse:
    messages: Tuple[str, ...] = ()
    exit_client: bool = False
    login_argv: Optional[Tuple[str, ...]] = None


HELP = """Commands
  /grill GOAL              question and freeze a candidate plan
  /plan                    show the full candidate plan and exact digest
  /approve yes|DIGEST      approve only that visible plan
  /run --accept-spend      prove readiness and detach the approved run
  /status                  current local session and supervisor state
  /boxes                   list the arbitrary-N worker pool
  /box ID|NUMBER           inspect one box's tasks, commands, evidence, and events
  /model SELECTION         manual | claude[:MODEL] | codex[:MODEL] | local:MODEL
  /effort LEVEL            low | medium | high | xhigh | max
  /login claude|codex      hand authentication to the provider's own CLI
  /connections             read-only connection discovery (not task readiness)
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
        runbook = compile_runbook(
            proposal,
            run_id=run_id,
            adapter={
                "kind": "claude_cli",
                "profile": "@camol/claude-fable-5-1",
                "timeout_seconds": 1800,
            },
        )
        execution_status = "preflight_required"
        limitation = "The requested fable alias is speculative until an explicit spend-capped preflight resolves it."
    elif selection.provider == "manual":
        limitation = "Manual mode is planning-only and cannot produce live worker evidence. Select claude:fable and re-run /grill to execute."
    elif selection.provider == "codex":
        limitation = "Codex is available for planning in V0; a fenced Codex worker adapter is not yet implemented."
    elif selection.provider == "local":
        limitation = "Local models are available for planning in V0; a fenced local worker adapter is not yet implemented."
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
    if not isinstance(value, dict) or set(value) != fields:
        raise InteractiveError("product plan has the wrong fields")
    if value["schema"] != "camol.product_plan" or value["schema_version"] != 1:
        raise InteractiveError("product plan schema is unsupported")
    proposal = validate_proposal(value["proposal"])
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
    lines = [
        "PLAN {}".format(digest),
        "goal: {}".format(proposal["goal"]),
        "execution: {} at effort {} ({})".format(
            proposal["execution"]["model"], proposal["execution"]["effort"], plan["execution_status"]
        ),
        "limits: boxes={} turns/task={} total_tokens={}".format(
            proposal["resource_limits"]["max_concurrency"],
            proposal["resource_limits"]["max_turns_per_task"],
            proposal["resource_limits"]["max_total_tokens"],
        ),
        "outcomes:",
    ]
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
        self.session = self.store.load()
        self.connections = ConnectionRegistry(self.store.project_dir)
        self.converse_fn = converse_fn
        self.spawn_fn = spawn_fn
        self.preflight_fn = preflight_fn

    def _persist_message(self, role: str, text: str, *, kind: str = "conversation") -> None:
        self.session = self.store.append_message(self.session, role, text, kind=kind)

    def _respond(
        self,
        *messages: str,
        exit_client: bool = False,
        login_argv: Optional[Sequence[str]] = None,
        kind: str = "notice",
    ) -> CommandResponse:
        for message in messages:
            self._persist_message("orchestrator" if kind == "conversation" else "system", message, kind=kind)
        return CommandResponse(tuple(messages), exit_client, tuple(login_argv) if login_argv else None)

    def handle(self, raw: str, *, on_chunk: Optional[Callable[[str], None]] = None) -> CommandResponse:
        text = raw.strip()
        if not text:
            return CommandResponse()
        self._persist_message("human", text, kind="command" if text.startswith("/") else "conversation")
        try:
            if text.startswith("/"):
                return self._command(text)
            if self.session.get("grill"):
                return self._answer_grill(text)
            reply = self.converse_fn(
                self.session["model"], text, self.session["messages"][:-1],
                effort=self.session["effort"], workspace=self.workspace, on_chunk=on_chunk,
            )
            identity = "{} -> {}".format(reply.requested_model or "default", reply.resolved_model or "unreported")
            return self._respond(reply.text, "model identity: {} ({})".format(identity, reply.provider), kind="conversation")
        except (
            InteractiveError, PlanningError, ConversationError, ConnectionError, ProviderError,
            SupervisorError, SessionError, WorkspaceError, RunbookError, OSError, ValueError,
        ) as error:
            return self._respond("denied: {}".format(error), kind="error")

    def _command(self, text: str) -> CommandResponse:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            raise InteractiveError("command has unmatched quoting") from error
        command = parts[0].lower()
        arguments = parts[1:]
        if command == "/help":
            return self._respond(HELP)
        if command == "/quit":
            return self._respond("Client detached. The supervisor, if running, was not stopped.", exit_client=True)
        if command == "/grill":
            if not arguments:
                raise InteractiveError("usage: /grill GOAL")
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
            )
            return self._respond("GRILL 1/{} — {}".format(len(grill_question_names()), grill.question()))
        if command == "/plan":
            if self.session["plan"] is None:
                raise InteractiveError("there is no plan; start with /grill GOAL")
            return self._respond(render_plan(self.session["plan"], self.session["plan_digest"]))
        if command == "/approve":
            return self._approve(arguments)
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
        if command == "/login":
            if len(arguments) != 1:
                raise InteractiveError("usage: /login claude|codex")
            argv = self.connections.login_argv(arguments[0])
            return self._respond(
                "Suspending Camol and handing login to the provider CLI. Camol will not read or copy its credential cache.",
                login_argv=argv,
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

    def _invalidate_plan_for_setting_change(self) -> None:
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
            render_plan(plan, digest),
        )

    def _approve(self, arguments: Sequence[str]) -> CommandResponse:
        if self.session["plan"] is None or self.session["status"] != "plan_ready":
            raise InteractiveError("there is no unapproved plan ready for confirmation")
        if not arguments:
            return self._respond(
                "Approval required for {}. Review /plan, then type `/approve yes` or `/approve {}`.".format(
                    self.session["plan_digest"], self.session["plan_digest"]
                )
            )
        if len(arguments) != 1 or arguments[0] not in {"yes", self.session["plan_digest"]}:
            raise InteractiveError("approval must be `yes` or the exact plan digest")
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
        WorkspaceManager(self.workspace, state_dir).assert_source_ready()
        state_dir.mkdir(parents=True, exist_ok=True)
        adapter_kinds = {agent["adapter"]["kind"] for agent in plan["runbook"]["agents"]}
        if adapter_kinds == {"claude_cli"}:
            remaining = list(arguments)
            accept_spend = False
            maximum = 10
            while remaining:
                option = remaining.pop(0)
                if option == "--accept-spend" and not accept_spend:
                    accept_spend = True
                elif option == "--max-cents" and remaining:
                    try:
                        maximum = int(remaining.pop(0))
                    except ValueError as error:
                        raise InteractiveError("--max-cents requires a positive integer") from error
                else:
                    raise InteractiveError("usage: /run --accept-spend [--max-cents N]")
            if maximum <= 0 or maximum > 100:
                raise InteractiveError("--max-cents must be between 1 and 100")
            if not accept_spend:
                raise InteractiveError(
                    "provider capability is still speculative; use `/run --accept-spend` for one preflight capped at 10 cents"
                )
            status = self.connections.probe_claude()
            if status["status"] != "ready":
                raise InteractiveError("Claude connection is not authenticated; use /login claude")
            profile = load_model_profile(self.workspace, "@camol/claude-fable-5-1")
            receipt = self.preflight_fn(
                profile,
                target_id=local_target_id(),
                state_dir=state_dir,
                cwd=self.workspace,
                accept_spend=True,
                spend_ceiling_cents=maximum,
            )
            readiness_line = "Run accepted after provider preflight: requested={} resolved={} receipt={}.".format(
                receipt.requested_model, receipt.resolved_model, receipt.digest()
            )
        elif adapter_kinds == {"process"}:
            if arguments:
                raise InteractiveError("a local process plan uses `/run` without provider-spend flags")
            readiness_line = "Run accepted for the explicitly approved local process adapter; no hosted-model proof is claimed."
        else:
            raise InteractiveError("Product V0 cannot execute a mixed or unsupported adapter plan")
        runbook_path = self.store.write_runbook(self.session, plan["runbook"])
        paths = SupervisorPaths.under(state_dir)
        if paths.socket.exists():
            remote_plan = self._control("plan")
            if remote_plan["plan_digest"] != runbook_digest(plan["runbook"]):
                raise InteractiveError("another supervisor owns this session state with a different plan")
            started = {"pid": self._control("status")["pid"], "started": False}
        else:
            started = self.spawn_fn(
                runbook_path,
                self.workspace,
                state_dir,
                approve_by=getpass.getuser(),
            )
        self.session = self.store.update(self.session, status="running")
        return self._respond(
            readiness_line,
            "Supervisor {} pid={}; closing this client will not stop it. No box leases until exact admission is green.".format(
                "reattached" if not started.get("started", True) else "detached", started["pid"]
            ),
        )

    def _status(self) -> CommandResponse:
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
        if len(arguments) != 1:
            raise InteractiveError("usage: /box ID|NUMBER")
        boxes = self.box_summaries()
        target = arguments[0]
        if target.isdigit() and 1 <= int(target) <= len(boxes):
            target = boxes[int(target) - 1]["box_id"]
        if target not in {item["box_id"] for item in boxes}:
            raise InteractiveError("unknown box")
        self.session = self.store.update(self.session, selected_box=target)
        try:
            view = self._control("box", {"box_id": target, "after_seq": 0, "limit": 100})
        except SupervisorError:
            return self._respond("BOX {} is dormant; no workspace, lease, commands, or evidence exist yet.".format(target))
        lines = [
            "BOX {} adapter={} tasks={} workspace={}".format(
                target, view["adapter_kind"], ",".join(view["task_ids"]) or "none",
                view["workspace"]["path"] if view["workspace"] else "not prepared",
            )
        ]
        for event in view["events"][-30:]:
            lines.append("#{:<4} {:<30} {}".format(
                event["seq"], event["type"], json.dumps(event["payload"], sort_keys=True)[:500]
            ))
        if not view["events"]:
            lines.append("No box events yet.")
        return self._respond("\n".join(lines))

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
