"""Qwen request routing followed by a no-tools Camol planning response."""

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .app import SlashCommand
from .classifier_preview import PreviewResponse
from .conversation import ConversationCancelled, ConversationError, converse, parse_selection, require_tool_free_provider
from .connections import validate_loopback_endpoint
from .planning import PlanningError, reject_sensitive_text
from .probes import Redactor
from .routed_prompt import build_routed_prompt
from .session import SessionStore, SessionError


CHAT_COMMANDS = (
    SlashCommand("/help", "Show routed chat commands", run_from_palette=True),
    SlashCommand("/model", "Choose claude:MODEL or local:MODEL", takes_value=True),
    SlashCommand("/state", "Set relevant task context", takes_value=True),
    SlashCommand("/prompt", "Inspect the last assembled LLM prompt", run_from_palette=True),
    SlashCommand("/history", "Show this conversation's saved messages", run_from_palette=True),
    SlashCommand("/example", "Request a 3D ping-pong implementation plan", run_from_palette=True),
    SlashCommand("/clear", "Start fresh; keep the audit log", run_from_palette=True),
    SlashCommand("/cancel", "Cancel the current response", run_from_palette=True),
    SlashCommand("/quit", "Exit", run_from_palette=True),
)


@dataclass(frozen=True)
class RoutedResponse(PreviewResponse):
    generation: object = None
    error: bool = False


class RoutedChatController:
    def __init__(self, predict, routes, *, warm, workspace, state_root, model=None,
                 endpoint="http://127.0.0.1:11434/v1", converse_fn=converse):
        self.endpoint = validate_loopback_endpoint(endpoint)
        self.predict, self.routes, self.warm = predict, routes, warm
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = SessionStore(self.workspace, Path(state_root))
        with self.store.transaction():
            self.session = self.store.load()
            model = model or (self.session["model"] if self.session["model"] != "manual" else "claude:sonnet")
            require_tool_free_provider(parse_selection(model))
            self.session = self.store.update(self.session, model=model, effort="medium")
        self.state = self.session.get("goal") or ""
        self.model = model
        self.converse_fn = converse_fn
        self.ready = False
        self.load_failed = False
        self.last_prompt = ""
        if self.session["messages"]:
            for event in self.store.proposal_events()[-1:]:
                if event.get("schema") == "camol.routed_prompt":
                    self.last_prompt = event["prompt"]
        self._command_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._closed_event = threading.Event()

    def close_client(self):
        self._closed_event.set()
        self._cancel_event.set()

    def _message(self, role, text, kind="conversation"):
        self.session = self.store.append_message(self.session, role, text, kind=kind)

    def handle(self, raw, *, on_chunk=None):
        text = raw.strip()
        if self._closed_event.is_set():
            return RoutedResponse(messages=("Routed chat is closed.",))
        if text == "/cancel":
            self._cancel_event.set()
            return RoutedResponse(messages=("Cancellation requested. The provider may already have consumed tokens.",))
        if text in {"/quit", "/exit"}:
            self.close_client()
            return RoutedResponse(exit_client=True)
        if text == "/prompt":
            return RoutedResponse(messages=(self.last_prompt or "No prompt assembled yet. Enter a request first.",))
        if not self._command_lock.acquire(blocking=False):
            return RoutedResponse(messages=("A response is running. Use /cancel or wait.",))
        try:
            if self._closed_event.is_set():
                return RoutedResponse()
            self._cancel_event.clear()
            reject_sensitive_text(text, "routed chat")
            if text == "/load":
                self.warm()
                self.ready, self.load_failed = True, False
                comparison, generation = None, None
                with self.store.transaction():
                    self.session = self.store.load()
                    events = self.store.proposal_events() if self.session["messages"] else []
                    calls = self.store.planning_calls() if events else []
                    if events and events[-1].get("schema") == "camol.routed_prompt":
                        comparison = (events[-1]["classifier"],)
                        if calls and calls[-1].get("call_id") == events[-1]["call_id"]:
                            call = calls[-1]
                            generation = {"model": call.get("resolved_model") or events[-1]["generator"],
                                          "status": call["status"], "input_tokens": call.get("input_tokens"),
                                          "output_tokens": call.get("output_tokens")}
                return RoutedResponse(comparison=comparison, generation=generation,
                                      messages=("Qwen routing ready. Generator: {}. Enter a request to get an answer; /prompt shows what was sent.".format(self.model),))
            with self.store.transaction():
                self.session = self.store.load()
                self.state = self.session.get("goal") or ""
                self.model = self.session["model"]
                if text == "/example":
                    text = "Create an implementation plan for a browser-based 3D ping-pong game controlled with Left/Right arrows or A/D. Include an AI opponent, ball physics, scoring, restart controls, milestones and acceptance tests. Plan only; do not write code yet."
                elif text in {"/", "/help"}:
                    return RoutedResponse(messages=(
                        "Enter a request: local GLiClass Qwen proposes a route, then your LLM answers a focused prompt.",
                        "/model claude:sonnet or /model local:MODEL selects the generator. Claude uses your existing CLI login; local uses the configured loopback endpoint.",
                        "/state TEXT · /prompt · /history · /clear · /cancel · /quit. No tool execution, workers or deployments.",
                        "Private conversation and routing logs: " + str(self.store.project_dir),
                    ))
                elif text == "/model":
                    return RoutedResponse(messages=("Generator: {} · local endpoint: {}".format(self.model, self.endpoint),))
                elif text.startswith("/model "):
                    model = text[7:].strip()
                    require_tool_free_provider(parse_selection(model))
                    self.session = self.store.update(self.session, model=model)
                    self.model = model
                    return RoutedResponse(messages=("Generator set to {}. The next request will use it.".format(model),))
                elif text == "/state":
                    return RoutedResponse(messages=("Task context: " + (self.state or "none"),))
                elif text.startswith("/state "):
                    self.state = text[7:].strip()
                    self.session = self.store.update(self.session, goal=self.state)
                    return RoutedResponse(messages=("Task context saved.",), reset_results=True)
                elif text == "/clear":
                    self.session = self.store.update(self.session, messages=[], goal=None)
                    self.state, self.last_prompt = "", ""
                    return RoutedResponse(messages=("New conversation. Previous audit entries are retained.",),
                                          clear_transcript=True, reset_results=True)
                elif text == "/history":
                    return RoutedResponse(messages=tuple("{} > {}".format(m["role"], m["content"]) for m in self.session["messages"][-12:]) or ("No saved messages yet.",))
                elif text.startswith("/"):
                    return RoutedResponse(messages=("This command is unavailable in routed chat. Use /help.",))
                if not text:
                    return RoutedResponse()
                return self._generate(text, on_chunk)
        except (PlanningError, ConversationError, SessionError, ValueError, OSError, RuntimeError, ImportError) as error:
            self.load_failed = not self.ready
            return RoutedResponse(messages=("Routed chat error: " + Redactor().text(str(error)),), reset_results=True, error=True)
        finally:
            self._command_lock.release()

    def _generate(self, text, on_chunk):
        history = list(self.session["messages"])
        # A bounded recent user turn helps resolve follow-ups without replacing
        # the verbatim current request. Explicit task context stays separate.
        previous = [m["content"] for m in history if m["role"] == "human" and m["kind"] == "conversation"]
        classifier_state = self.state
        if previous:
            classifier_state += "\nPrevious user request: " + previous[-1][:700]
        self._message("human", text)
        call_id = "routed-" + uuid4().hex
        self.last_prompt = ""
        try:
            observation = self.predict(text, classifier_state)
            self.ready, self.load_failed = True, False
            if self._cancel_event.is_set():
                raise ConversationCancelled("cancelled before the LLM request")
            self.last_prompt = build_routed_prompt(text, self.state, observation)
        except (ValueError, RuntimeError, OSError, ImportError) as error:
            self.store.append_proposal_event({
                "schema": "camol.routing_error", "schema_version": 1, "call_id": call_id,
                "error": Redactor().text(str(error)), "generator_called": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            self._message("system", "Routing failed: " + Redactor().text(str(error)), kind="error")
            raise
        self.store.append_proposal_event({
            "schema": "camol.routed_prompt", "schema_version": 1, "call_id": call_id,
            "classifier": observation, "generator": self.model, "prompt": self.last_prompt,
            "prompt_sha256": hashlib.sha256(self.last_prompt.encode()).hexdigest(),
            "tool_policy": "none", "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if on_chunk:
            on_chunk("Qwen route: {} → {}\n\n".format(observation["proposed_route"] or "uncertain; preserve original request", self.model))
        reply, status = None, "failed"
        began = time.monotonic()
        try:
            reply = self.converse_fn(self.model, self.last_prompt, history,
                                     effort="medium", workspace=self.workspace, local_endpoint=self.endpoint,
                                     on_chunk=on_chunk, cancel_event=self._cancel_event, no_tools=True)
            if self._cancel_event.is_set() or self._closed_event.is_set():
                raise ConversationCancelled("response cancelled")
            self._message("orchestrator", reply.text)
            status = "completed"
            return RoutedResponse(comparison=(observation,), generation={
                "model": reply.resolved_model or self.model, "status": status,
                "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
            }, messages=(reply.text, "Generator: {} · /prompt shows the routed prompt".format(reply.resolved_model or self.model)))
        except ConversationCancelled:
            status = "cancelled"
            self._message("system", "LLM response cancelled.", kind="error")
            raise
        except (ConversationError, OSError, RuntimeError, ValueError) as error:
            self._message("system", "LLM response failed: " + Redactor().text(str(error)), kind="error")
            raise
        finally:
            self.store.append_planning_call({
                "schema": "camol.planning_call", "schema_version": 1, "call_id": call_id,
                "session_id": self.session["session_id"], "provider": parse_selection(self.model).provider,
                "requested_model": parse_selection(self.model).model,
                "resolved_model": reply.resolved_model if reply else None,
                "status": status, "duration_seconds": time.monotonic() - began,
                "request_kind": "qwen_routed_chat", "tool_policy": "none",
                "input_tokens": reply.input_tokens if reply else None,
                "output_tokens": reply.output_tokens if reply else None,
            })
