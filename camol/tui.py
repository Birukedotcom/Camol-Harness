"""Textual terminal client for Camol's detachable orchestrator."""

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from textual.app import App, ComposeResult
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .app import CommandResponse, InteractiveController
from .boot import compose_boot
from .connections import ConnectionError
from .probes import Redactor


class BootScreen(Screen):
    BINDINGS = [Binding("escape,enter", "dismiss", "Continue", show=True)]

    def __init__(self, duration: float = 0.9):
        super().__init__()
        self.duration = duration

    def compose(self) -> ComposeResult:
        yield Static("", id="boot-art")

    def on_mount(self) -> None:
        self._render_art()
        if self.duration >= 0:
            # Textual awaits timer callback return values. Screen.dismiss()
            # returns an AwaitDismiss handle, which must not be awaited from
            # the screen's own message handler; route through the action so
            # the callback itself returns None.
            self.set_timer(self.duration, self.action_dismiss)

    def on_resize(self) -> None:
        self._render_art()

    def _render_art(self) -> None:
        self.query_one("#boot-art", Static).update(compose_boot(self.size.width, self.size.height - 1))

    def action_dismiss(self) -> None:
        self.dismiss()


class PromptArea(TextArea):
    """A multiline editor whose submission chord belongs to the app."""


class LoginProviderScreen(ModalScreen):
    """Keyboard-only provider chooser for the native CLI login handoff."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    LoginProviderScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.72);
    }
    #login-dialog {
        width: 58;
        height: auto;
        max-height: 16;
        border: round #6e7d79;
        background: #121819;
        padding: 1 2;
    }
    #login-title { height: 1; color: #eef2f1; text-style: bold; }
    #login-help { height: 2; color: #9ca9a6; margin-bottom: 1; }
    #login-options { height: 6; background: #121819; border: none; }
    """

    LABELS = {"claude": "Claude Code", "codex": "Codex CLI"}

    def __init__(self, providers: Sequence[str], statuses: Mapping[str, str]):
        super().__init__()
        self.providers = tuple(providers)
        self.statuses = dict(statuses)

    def compose(self) -> ComposeResult:
        with Vertical(id="login-dialog"):
            yield Static("CONNECT AN ACCOUNT", id="login-title")
            yield Static("Use ↑/↓ and Enter. The provider owns the browser sign-in.", id="login-help")
            yield OptionList(
                *(
                    Option(
                        "{}  [{}]".format(
                            self.LABELS.get(provider, provider),
                            "connected" if self.statuses.get(provider) == "ready" else "sign in",
                        ),
                        id=provider,
                    )
                    for provider in self.providers
                ),
                id="login-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CamolApp(App):
    TITLE = "Camol"
    SUB_TITLE = "persistent orchestration harness"
    CSS = """
    Screen { background: #090b0c; color: #e8eceb; }
    BootScreen { align: left top; padding: 1 1; }
    #boot-art { width: 100%; height: 100%; color: #f4f5f4; }
    #dependency-rail { height: 1; background: #111719; color: #9ca9a6; padding: 0 1; }
    #context { height: 1; background: #182022; color: #d5dfdc; padding: 0 1; }
    #transcript {
        height: 1fr;
        padding: 1 2;
        scrollbar-size-vertical: 1;
        scrollbar-color: #6e7d79;
        scrollbar-color-hover: #83938f;
        scrollbar-color-active: #9baba7;
        scrollbar-background: #1e1e1e;
        scrollbar-background-hover: #1e1e1e;
        scrollbar-background-active: #1e1e1e;
        scrollbar-corner-color: #1e1e1e;
    }
    #stream { height: auto; max-height: 5; color: #b8c7c3; padding: 0 2; }
    #prompt { height: 5; border: solid #62716d; margin: 0 1; background: #0e1314; }
    #fleet { height: 2; background: #111719; color: #c8d1cf; padding: 0 1; }
    Footer { background: #182022; }
    """
    BINDINGS = [
        Binding("ctrl+enter", "submit", "Send", priority=True),
        Binding("alt+enter", "submit", "Send", show=False, priority=True),
        Binding("alt+0", "orchestrator", "Orchestrator", show=False),
        Binding("alt+1", "box(1)", "Box 1", show=False),
        Binding("alt+2", "box(2)", "Box 2", show=False),
        Binding("alt+3", "box(3)", "Box 3", show=False),
        Binding("alt+4", "box(4)", "Box 4", show=False),
        Binding("alt+5", "box(5)", "Box 5", show=False),
        Binding("alt+6", "box(6)", "Box 6", show=False),
        Binding("alt+7", "box(7)", "Box 7", show=False),
        Binding("alt+8", "box(8)", "Box 8", show=False),
        Binding("alt+9", "box(9)", "Box 9", show=False),
        Binding("left_square_bracket", "cycle(-1)", "Previous box", show=False),
        Binding("right_square_bracket", "cycle(1)", "Next box", show=False),
        Binding("alt+up", "history(-1)", "Previous input", show=False),
        Binding("alt+down", "history(1)", "Next input", show=False),
        Binding("ctrl+q", "detach", "Detach"),
        Binding("f10", "detach", "Detach", show=False),
    ]

    def __init__(
        self,
        controller: InteractiveController,
        *,
        show_boot: bool = True,
        boot_duration: float = 0.9,
        discover_connections: bool = True,
    ):
        super().__init__()
        self.controller = controller
        self.show_boot = show_boot
        self.boot_duration = boot_duration
        self.discover_connections = discover_connections
        self.history = []
        self.history_index = 0
        self.selected = "orchestrator"
        self.boxes = []
        self.stream_text = ""
        self._connection_probe_lock = threading.Lock()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="dependency-rail")
            yield Static("ORCHESTRATOR", id="context")
            yield RichLog(id="transcript", markup=False, wrap=True, highlight=False)
            yield Static("", id="stream")
            yield PromptArea(
                "", id="prompt", soft_wrap=True, show_line_numbers=False,
                placeholder="Message the orchestrator or type /help — Ctrl+Enter sends",
            )
            yield Static("orchestrator [Alt+0]", id="fleet")
            yield Footer()

    async def on_mount(self) -> None:
        self._render_existing_messages()
        self._render_dependency_rail()
        await self._refresh_fleet()
        self.query_one("#prompt", PromptArea).focus()
        self.set_interval(1.0, self._refresh_fleet)
        if self.discover_connections:
            self._probe_connections()
        if self.show_boot:
            self.push_screen(BootScreen(self.boot_duration))

    def _render_existing_messages(self) -> None:
        log = self.query_one("#transcript", RichLog)
        if not self.controller.session["messages"]:
            log.write("CAMOL PRODUCT V0")
            log.write("No work starts from conversation alone. Type /help or /grill GOAL.")
            return
        for item in self.controller.session["messages"][-100:]:
            label = {"human": "you", "orchestrator": "orchestrator", "system": "camol"}[item["role"]]
            log.write("{} > {}".format(label, item["content"]))

    def _render_dependency_rail(self) -> None:
        records = {record["connection_id"]: record for record in self.controller.connections.load()}
        def provider(connection_id: str, binary: str) -> str:
            record = records.get(connection_id)
            if record and record["status"] == "ready":
                return "■ " + binary
            if record and record["status"] in {"auth_required", "error"}:
                return "□ " + binary
            if not record and self.discover_connections and shutil.which(binary):
                return "↻ " + binary
            return ("□ " if shutil.which(binary) else "· ") + binary
        parts = [
            "■ git" if shutil.which("git") else "· git",
            "■ docker" if shutil.which("docker") else "· docker",
            provider("claude-cli", "claude"),
            provider("codex-cli", "codex"),
            "model=" + self.controller.session["model"],
            "effort=" + self.controller.session["effort"],
        ]
        self.query_one("#dependency-rail", Static).update("  ".join(parts))
        self._render_orchestrator_context(records)

    def _render_orchestrator_context(self, records: Mapping[str, Mapping[str, str]]) -> None:
        if self.selected != "orchestrator":
            return
        model = self.controller.session["model"]
        provider = model.partition(":")[0]
        connection_id = {"claude": "claude-cli", "codex": "codex-cli"}.get(provider)
        connected = connection_id is not None and records.get(connection_id, {}).get("status") == "ready"
        suffix = " · connected" if connected else ""
        self.query_one("#context", Static).update("ORCHESTRATOR — model={}{}".format(model, suffix))

    def _probe_connections(
        self,
        *,
        login_provider: Optional[str] = None,
        login_returncode: Optional[int] = None,
    ) -> None:
        """Refresh account inventory without delaying boot or blocking input."""
        def probe() -> None:
            # A disposable startup refresh may be skipped if another scan owns the
            # lock. A post-login refresh is a state transition and must queue behind
            # that scan rather than leaving the UI stuck at "verifying".
            if not self._connection_probe_lock.acquire(blocking=login_provider is not None):
                return
            try:
                try:
                    self.controller.connections.probe_all()
                except (ConnectionError, OSError, ValueError):
                    # Per-provider failures are normally recorded by probe_all. A
                    # registry failure must not make the client itself unavailable.
                    pass
                try:
                    self.call_from_thread(
                        self._finish_connection_probe,
                        login_provider,
                        login_returncode,
                    )
                except (ConnectionError, RuntimeError):
                    # The terminal is disposable. A detach may race this read-only
                    # inventory refresh and must never wait for it.
                    pass
            finally:
                self._connection_probe_lock.release()

        threading.Thread(target=probe, name="camol-connection-probe", daemon=True).start()

    def _finish_connection_probe(
        self,
        login_provider: Optional[str],
        login_returncode: Optional[int],
    ) -> None:
        self._render_dependency_rail()
        if login_provider is None:
            return
        log = self.query_one("#transcript", RichLog)
        response = self.controller.confirm_provider_connection(
            login_provider,
            login_returncode=login_returncode,
        )
        for message in response.messages:
            log.write("camol > " + message)
        self._render_dependency_rail()
        self.query_one("#prompt", PromptArea).focus()

    async def _refresh_fleet(self) -> None:
        boxes = await self.run_worker(
            self.controller.box_summaries, thread=True, group="fleet", exclusive=True
        ).wait()
        self.boxes = boxes
        items = ["ORCH[Alt+0]"]
        for index, box in enumerate(boxes, 1):
            mark = "!" if box["status"] in {"blocked", "waiting"} else "■" if box.get("connected") == "yes" else "□"
            shortcut = "Alt+{}".format(index) if index <= 9 else "cycle"
            selected = ">" if self.selected == box["box_id"] else ""
            items.append("{}{} {}:{}({})".format(selected, mark, index, box["box_id"], shortcut))
        self.query_one("#fleet", Static).update("  ".join(items))
        self._render_dependency_rail()

    def action_submit(self) -> None:
        prompt = self.query_one("#prompt", PromptArea)
        text = prompt.text.strip()
        if not text:
            return
        prompt.clear()
        redactor = Redactor()
        visible_text = redactor.text(text) if redactor.contains_sensitive(text) else text
        self.history.append(visible_text)
        self.history = self.history[-200:]
        self.history_index = len(self.history)
        self.query_one("#transcript", RichLog).write("you > " + visible_text)
        self._submit(text)

    @work(thread=True, group="commands", exclusive=True)
    def _submit(self, text: str) -> None:
        self.stream_text = ""
        response = self.controller.handle(text, on_chunk=self._stream_chunk)
        self.call_from_thread(self._apply_response, response)

    def _stream_chunk(self, chunk: str) -> None:
        self.stream_text = (self.stream_text + chunk)[-4000:]
        self.call_from_thread(
            self.query_one("#stream", Static).update,
            "orchestrator ~ " + self.stream_text,
        )

    def _apply_response(self, response: CommandResponse) -> None:
        self.query_one("#stream", Static).update("")
        log = self.query_one("#transcript", RichLog)
        for message in response.messages:
            log.write("camol > " + message)
        if response.login_choices:
            status_by_provider: Dict[str, str] = {}
            try:
                records = self.controller.connections.load()
            except ConnectionError:
                records = []
            for record in records:
                if record["connection_id"] == "claude-cli":
                    status_by_provider["claude"] = record["status"]
                elif record["connection_id"] == "codex-cli":
                    status_by_provider["codex"] = record["status"]
            self.push_screen(
                LoginProviderScreen(response.login_choices, status_by_provider),
                self._login_provider_selected,
            )
            return
        if response.login_argv:
            try:
                with self.suspend():
                    completed = subprocess.run(list(response.login_argv), check=False)
                returncode = completed.returncode
            except OSError:
                returncode = None
                log.write("camol > Provider login could not start. Verifying installation and account status…")
            else:
                log.write("camol > Provider login returned. Verifying connection status…")
            self._probe_connections(
                login_provider=response.login_provider,
                login_returncode=returncode,
            )
        if response.exit_client:
            self.exit()
            return
        self.query_one("#prompt", PromptArea).focus()
        self.call_later(self._refresh_fleet)

    def _login_provider_selected(self, provider: Optional[str]) -> None:
        if provider is None:
            self.query_one("#prompt", PromptArea).focus()
            return
        self.query_one("#transcript", RichLog).write("you > /login " + provider)
        connection_id = {"claude": "claude-cli", "codex": "codex-cli"}[provider]
        try:
            records = self.controller.connections.load()
        except ConnectionError:
            records = []
        record = next(
            (
                item
                for item in records
                if item["connection_id"] == connection_id
            ),
            None,
        )
        if record is not None and record["status"] == "ready":
            self._apply_response(self.controller.confirm_provider_connection(provider, login_returncode=0))
            return
        self._submit("/login " + provider)

    def action_orchestrator(self) -> None:
        self.selected = "orchestrator"
        self._render_dependency_rail()
        self.query_one("#prompt", PromptArea).focus()

    def action_box(self, index: int) -> None:
        boxes = self.boxes
        if not 1 <= index <= len(boxes):
            self.notify("Box {} does not exist".format(index), severity="warning")
            return
        target = boxes[index - 1]["box_id"]
        self.selected = target
        self.query_one("#context", Static).update("BOX {} — read-only evidence view".format(target))
        self._submit("/box " + target)

    def action_cycle(self, direction: int) -> None:
        boxes = self.boxes
        choices = ["orchestrator"] + [item["box_id"] for item in boxes]
        current = choices.index(self.selected) if self.selected in choices else 0
        selected = choices[(current + direction) % len(choices)]
        if selected == "orchestrator":
            self.action_orchestrator()
        else:
            self.action_box(choices.index(selected))

    def action_history(self, direction: int) -> None:
        if not self.history:
            return
        self.history_index = max(0, min(len(self.history), self.history_index + direction))
        value = "" if self.history_index == len(self.history) else self.history[self.history_index]
        prompt = self.query_one("#prompt", PromptArea)
        prompt.load_text(value)
        prompt.focus()

    def action_detach(self) -> None:
        self.exit()


def run_tui(
    workspace: Path,
    *,
    state_root: Optional[Path] = None,
    show_boot: bool = True,
) -> int:
    controller = InteractiveController(workspace, state_root=state_root)
    CamolApp(controller, show_boot=show_boot).run()
    return 0
