"""In-memory classifier preview controller with no execution capabilities."""

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from .app import CommandResponse, SlashCommand
from .planning import PlanningError, reject_sensitive_text
from .probes import Redactor


PREVIEW_COMMANDS = (
    SlashCommand("/help", "Show classifier preview help", run_from_palette=True),
    SlashCommand("/state", "Set the task context used by both classifiers", takes_value=True),
    SlashCommand("/routes", "Show the candidate procedures", run_from_palette=True),
    SlashCommand("/example", "Compare a sample code-review request", run_from_palette=True),
    SlashCommand("/clear", "Clear the transcript, results, and task context", run_from_palette=True),
    SlashCommand("/cancel", "Discard a pending comparison", run_from_palette=True),
    SlashCommand("/quit", "Exit the local preview", run_from_palette=True),
)


@dataclass(frozen=True)
class PreviewResponse(CommandResponse):
    comparison: Optional[Tuple[dict, ...]] = None
    reset_results: bool = False


class ClassifierPreviewController:
    """Only a predictor is supplied; no provider, store, supervisor or tool API.

    The predictor accepts (text, observed_state). Results are always observations.
    Even commands available elsewhere in Camol cannot reach its command engine.
    """

    def __init__(self, predict, routes, *, warm=None):
        self.predict = predict
        self.warm = warm
        self.routes = routes
        self.state = ""
        self.ready = False
        self.load_failed = False
        self._command_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._closed_event = threading.Event()

    def close_client(self):
        self._closed_event.set()
        self._cancel_event.set()

    def handle(self, raw, *, on_chunk=None):
        if self._closed_event.is_set():
            return PreviewResponse(messages=("Preview is closed.",))
        text = raw.strip()
        if text == "/cancel":
            self._cancel_event.set()
            return PreviewResponse(messages=("Pending comparison will be discarded.",), reset_results=True)
        if text in {"/quit", "/exit"}:
            self.close_client()
            return PreviewResponse(exit_client=True)
        if not self._command_lock.acquire(blocking=False):
            return PreviewResponse(messages=("A comparison is running. Wait or use /cancel.",))
        try:
            if self._closed_event.is_set():
                return PreviewResponse(messages=("Preview is closed.",))
            self._cancel_event.clear()
            reject_sensitive_text(text, "classifier preview")
            if text == "/load":
                if self.warm is not None:
                    self.warm()
                self.ready = True
                self.load_failed = False
                return PreviewResponse(messages=("Both local classifiers are ready. Enter a request to compare their routes.",))
            if text == "/example":
                text = "Review this pull request for bugs and regressions."
            elif text in {"/help", "/"}:
                return PreviewResponse(messages=(
                    "Enter a request to compare Small and Qwen. Enter sends; Shift+Enter adds a line.",
                    "/state TEXT supplies task context · /routes lists procedures · /example tries a review request",
                    "/clear resets the view and context · /cancel discards a comparison · /quit exits",
                    "Preview only: no planning model, commands, worker launches or repository changes.",
                ))
            elif text == "/routes":
                return PreviewResponse(messages=tuple("{} — {}".format(r["id"], r["description"]) for r in self.routes))
            elif text == "/state":
                return PreviewResponse(messages=("Task context: " + (self.state or "none"),))
            elif text.startswith("/state "):
                self.state = text[7:].strip()
                return PreviewResponse(messages=("Task context updated for both models.",), reset_results=True)
            elif text == "/clear":
                self.state = ""
                return PreviewResponse(messages=("Transcript, results and context cleared.",),
                                       clear_transcript=True, reset_results=True)
            elif text.startswith("/"):
                return PreviewResponse(messages=("{} is unavailable in classifier preview. Use /help.".format(text.split()[0]),))
            if not text:
                return PreviewResponse()
            results = tuple(self.predict(text, self.state))
            self.ready = True
            self.load_failed = False
            if self._cancel_event.is_set() or self._closed_event.is_set():
                return PreviewResponse(messages=("Comparison discarded.",), reset_results=True)
            summary = []
            for row in results:
                route = row["proposed_route"] or "ABSTAIN"
                summary.append("{} → {} ({:.1f} ms)".format(row["model"], route, row["latency_ms"]))
            agreed = len({row["proposed_route"] for row in results}) == 1 and all(row["proposed_route"] for row in results)
            message = "Both propose the same route." if agreed else "Review the difference or abstention before choosing a procedure."
            return PreviewResponse(comparison=results, messages=(" · ".join(summary), message))
        except (PlanningError, ValueError, OSError, RuntimeError, ImportError) as error:
            self.load_failed = not self.ready
            return PreviewResponse(messages=("Preview error: " + Redactor().text(str(error)),), reset_results=True)
        finally:
            self._command_lock.release()
