"""Textual terminal client for Camol's detachable orchestrator."""

import asyncio
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from textual.app import App, ComposeResult
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import WorkerCancelled
from rich.text import Text

from .app import SLASH_COMMANDS, CommandResponse, InteractiveController, SlashCommand
from .boot import compose_boot
from .connections import ConnectionError, observation_label
from .probes import Redactor
from .pane_switcher import filter_rows, row_label
from .tiled_monitor import TiledMonitor


async def _await_view_worker(worker):
    """Preserve caller cancellation across Textual's Worker.wait conversion.

    Worker supersession is a harmless view refresh loss. Cancelling the timer
    that awaits it must still stop that timer, including on Python 3.9 where
    Task.cancelling() is unavailable.
    """
    waiter = asyncio.create_task(worker.wait())
    try:
        return await asyncio.shield(waiter)
    except asyncio.CancelledError:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        raise


class _OwnedClientWork:
    """Track actual persistence, not a cancelled executor Future's lifetime."""

    def __init__(self):
        self.lock = threading.Lock()
        self.idle = threading.Event()
        self.idle.set()
        self.closed = False
        self.tickets = {}

    def reserve(self):
        with self.lock:
            if self.closed:
                return None
            ticket = object()
            self.tickets[ticket] = "queued"
            self.idle.clear()
            return ticket

    def start(self, ticket):
        with self.lock:
            if self.closed or ticket not in self.tickets:
                return False
            self.tickets[ticket] = "running"
            return True

    def finish(self, ticket):
        with self.lock:
            self.tickets.pop(ticket, None)
            if not self.tickets:
                self.idle.set()

    def close(self):
        with self.lock:
            self.closed = True
            self.tickets = {key: value for key, value in self.tickets.items() if value == "running"}
            if not self.tickets:
                self.idle.set()


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


class BoxSwitcherScreen(ModalScreen):
    """A bounded page of a fixed metadata snapshot; no terminal input routing."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor(-1)", "Previous", show=False, priority=True),
        Binding("down", "cursor(1)", "Next", show=False, priority=True),
        Binding("pageup", "page(-1)", "Previous page", show=False, priority=True),
        Binding("pagedown", "page(1)", "Next page", show=False, priority=True),
    ]
    CSS = """
    BoxSwitcherScreen { align: center middle; background: rgba(0, 4, 1, 0.82); }
    #box-picker { width: 90%; max-width: 110; height: 85%; border: round #2fbd62; background: #07100a; padding: 0 1; }
    #box-picker-title { height: 1; color: #f4fff7; text-style: bold; }
    #box-picker-help { height: auto; max-height: 3; color: #83a58d; }
    #box-picker-input { height: 3; color: #ffffff; background: #050b07; border: tall #17462a; }
    #box-picker-options { height: 1fr; border: none; background: #07100a; color: #dce7df; scrollbar-size-vertical: 1; }
    #box-picker-options > .option-list--option-highlighted { color: #ffffff; background: #123a20; }
    #box-picker-page { height: 1; color: #83a58d; }
    """
    PAGE_SIZE = 50

    def __init__(self, snapshot, selected_key="orchestrator"):
        super().__init__()
        self.snapshot = snapshot
        self.search_query = snapshot["query"]
        self.rows = filter_rows(snapshot["rows"], snapshot["query"])
        self.selected_key = selected_key
        index = next((i for i, row in enumerate(self.rows) if row["key"] == self.selected_key), 0)
        self.page_offset = index // self.PAGE_SIZE * self.PAGE_SIZE

    def _options(self):
        return [Option(Text(row_label(row)), id=row["key"]) for row in self.rows[self.page_offset:self.page_offset + self.PAGE_SIZE]]

    def compose(self) -> ComposeResult:
        with Vertical(id="box-picker"):
            yield Static("BOX SWITCHER / {} / {}".format(Redactor().text(self.snapshot["run_id"]), self.snapshot["basis"]),
                         id="box-picker-title", markup=False)
            yield Static("Search box, group, task, status or adapter · ↑/↓ Enter · PgUp/PgDn · Esc\n★ pinned · /pin BOX on|off · /group BOX NAME · Display only; no authority change.", id="box-picker-help")
            yield Input(value=self.snapshot["query"], placeholder="Search boxes…", max_length=256,
                        select_on_focus=False, id="box-picker-input")
            yield OptionList(*self._options(), id="box-picker-options")
            yield Static("", id="box-picker-page")

    def on_mount(self):
        self._render_page()
        field = self.query_one("#box-picker-input", Input)
        field.cursor_position = len(field.value)
        field.focus()

    def _render_page(self):
        options = self.query_one("#box-picker-options", OptionList)
        options.set_options(self._options())
        page = self.rows[self.page_offset:self.page_offset + self.PAGE_SIZE]
        options.highlighted = next((i for i, row in enumerate(page) if row["key"] == self.selected_key), 0) if page else None
        self.query_one("#box-picker-page", Static).update("{}–{} / {} matches | {} boxes | cursor {}".format(
            self.page_offset + 1 if page else 0, self.page_offset + len(page), len(self.rows),
            len(self.snapshot["rows"]) - 1, self.snapshot["event_cursor"]))

    def on_input_changed(self, event: Input.Changed):
        if event.input.id != "box-picker-input":
            return
        if event.value == self.search_query:
            return
        self.search_query = event.value
        self.rows = filter_rows(self.snapshot["rows"], event.value)
        self.page_offset = 0
        self._render_page()

    def action_cursor(self, direction):
        options = self.query_one("#box-picker-options", OptionList)
        (options.action_cursor_up if direction < 0 else options.action_cursor_down)()

    def action_page(self, direction):
        if self.rows:
            self.page_offset = max(0, min((len(self.rows) - 1) // self.PAGE_SIZE * self.PAGE_SIZE,
                                     self.page_offset + direction * self.PAGE_SIZE))
            self._render_page()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "box-picker-input":
            index = self.query_one("#box-picker-options", OptionList).highlighted
            if index is not None:
                self.dismiss((self.rows[self.page_offset + index]["key"], self.snapshot["scope"]))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss((event.option.id, self.snapshot["scope"]))

    def action_cancel(self):
        self.dismiss(None)


class PromptArea(TextArea):
    """Composer with chat-style Enter-to-send behavior."""

    BINDINGS = [
        Binding("enter", "send", "Send"),
        Binding("ctrl+enter,alt+enter", "send", "Send", show=False),
        Binding("shift+enter,ctrl+j", "newline", "New line"),
    ]

    def on_key(self, event):
        # Priority bindings run in the app before previously forwarded typing
        # reaches TextArea. Handle submission in this widget's ordered queue.
        # Prevent the base TextArea handler from inserting Enter as a newline.
        if event.key in {"enter", "ctrl+enter", "alt+enter", "shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            if event.key in {"shift+enter", "ctrl+j"}:
                self.action_newline()
            else:
                self.action_send()

    def action_send(self) -> None:
        self.app.action_submit()

    def action_newline(self) -> None:
        self.insert("\n")


class LoginProviderScreen(ModalScreen):
    """Keyboard-only provider chooser for the native CLI login handoff."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    LoginProviderScreen {
        align: center middle;
        background: rgba(0, 4, 1, 0.82);
    }
    #login-dialog {
        width: 58;
        height: auto;
        max-height: 16;
        border: round #2fbd62;
        background: #07100a;
        padding: 1 2;
    }
    #login-title { height: 1; color: #f4fff7; text-style: bold; }
    #login-help { height: 2; color: #83a58d; margin-bottom: 1; }
    #login-options {
        height: 6;
        color: #dce7df;
        background: #07100a;
        border: none;
        scrollbar-size-vertical: 1;
        scrollbar-color: #1f713c;
        scrollbar-background: #09100b;
    }
    #login-options > .option-list--option-highlighted {
        color: #ffffff;
        background: #123a20;
        text-style: bold;
    }
    """

    LABELS = {"claude": "Claude Code", "codex": "Codex CLI"}

    def __init__(self, providers: Sequence[str], statuses: Mapping[str, str]):
        super().__init__()
        self.providers = tuple(providers)
        self.statuses = dict(statuses)

    def compose(self) -> ComposeResult:
        with Vertical(id="login-dialog"):
            yield Static("SIGN IN / RECONNECT", id="login-title")
            yield Static("Enter launches the provider login and URL. Escape cancels.", id="login-help")
            yield OptionList(
                *(
                    Option(
                        "{}  [{}]".format(
                            self.LABELS.get(provider, provider),
                            "auth previously observed — reconnect" if self.statuses.get(provider) == "ready" else "sign in",
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


@dataclass(frozen=True)
class SlashSelection:
    command: SlashCommand
    text: str


class SlashCommandScreen(ModalScreen):
    """Typeable slash-command palette opened by entering `/`."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor(-1)", "Previous", show=False, priority=True),
        Binding("down", "cursor(1)", "Next", show=False, priority=True),
        Binding("tab", "complete", "Complete", show=False, priority=True),
    ]
    CSS = """
    SlashCommandScreen {
        align: center middle;
        background: rgba(0, 4, 1, 0.82);
    }
    #command-dialog {
        width: 78;
        height: 26;
        max-height: 90%;
        border: round #2fbd62;
        background: #07100a;
        padding: 1 2;
    }
    #command-title { height: 1; color: #f4fff7; text-style: bold; }
    #command-help { height: 2; color: #83a58d; margin-bottom: 1; }
    #command-input {
        height: 3;
        color: #ffffff;
        background: #030705;
        border: tall #1a773c;
        margin-bottom: 1;
    }
    #command-input:focus { border: tall #45e07a; }
    #command-options {
        height: 1fr;
        color: #dce7df;
        background: #07100a;
        border: none;
        scrollbar-size-vertical: 1;
        scrollbar-color: #1f713c;
        scrollbar-color-hover: #32a85a;
        scrollbar-color-active: #52e27e;
        scrollbar-background: #09100b;
        scrollbar-background-hover: #09100b;
        scrollbar-background-active: #09100b;
    }
    #command-options > .option-list--option-highlighted {
        color: #ffffff;
        background: #123a20;
        text-style: bold;
    }
    """

    def __init__(self, commands: Sequence[SlashCommand]):
        super().__init__()
        self.commands = tuple(commands)
        self.command_by_id = {
            "command-{}".format(index): command for index, command in enumerate(self.commands)
        }
        self.filtered = self.commands

    def _options(self) -> Sequence[Option]:
        return tuple(
            Option(
                "{:<14} {}".format(command.command, command.description),
                id="command-{}".format(self.commands.index(command)),
            )
            for command in self.filtered
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="command-dialog"):
            yield Static("COMMANDS & BUILT-IN PROTOCOLS", id="command-title")
            yield Static("Type to filter · Space stays text · ↑/↓ choose · Tab completes · Enter accepts", id="command-help")
            yield Input(value="/", placeholder="Type a command or skill", id="command-input", select_on_focus=False)
            yield OptionList(*self._options(), id="command-options")

    def on_mount(self) -> None:
        command_input = self.query_one("#command-input", Input)
        command_input.cursor_position = len(command_input.value)
        command_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command-input":
            return
        token = event.value.strip().partition(" ")[0].lower()
        self.filtered = tuple(
            command for command in self.commands if command.command.startswith(token)
        )
        options = self.query_one("#command-options", OptionList)
        options.set_options(self._options())
        options.highlighted = 0 if self.filtered else None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "command-input":
            self._accept(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        command = self.command_by_id[event.option.id]
        self.dismiss(SlashSelection(command, command.command))

    def action_cursor(self, direction: int) -> None:
        options = self.query_one("#command-options", OptionList)
        if direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()

    def action_complete(self) -> None:
        command = self._highlighted_command()
        if command is None:
            return
        command_input = self.query_one("#command-input", Input)
        command_input.value = command.command + (" " if command.takes_value else "")
        command_input.cursor_position = len(command_input.value)

    def _highlighted_command(self) -> Optional[SlashCommand]:
        options = self.query_one("#command-options", OptionList)
        highlighted = options.highlighted
        if highlighted is None or highlighted >= len(self.filtered):
            return None
        return self.filtered[highlighted]

    def _accept(self, value: str) -> None:
        text = value.strip()
        token = text.partition(" ")[0].lower()
        exact = next((command for command in self.commands if command.command == token), None)
        command = exact or self._highlighted_command()
        if command is None:
            return
        if exact is None:
            text = command.command
        self.dismiss(SlashSelection(command, text))

    def action_cancel(self) -> None:
        self.dismiss(None)


class CamolApp(App):
    TITLE = "Camol"
    SUB_TITLE = "persistent orchestration harness"
    CSS = """
    Screen { background: #030604; color: #e7eee9; }
    BootScreen { align: left top; padding: 1 1; }
    #boot-art { width: 100%; height: 100%; color: #68e892; }
    #dependency-rail { height: auto; max-height: 3; background: #07100a; color: #79d996; padding: 0 1; }
    #context { height: 1; background: #0b1710; color: #f1f7f3; padding: 0 1; }
    #transcript, #box-transcript {
        height: 1fr;
        padding: 1 2;
        scrollbar-size-vertical: 1;
        scrollbar-color: #1f713c;
        scrollbar-color-hover: #32a85a;
        scrollbar-color-active: #52e27e;
        scrollbar-background: #09100b;
        scrollbar-background-hover: #09100b;
        scrollbar-background-active: #09100b;
        scrollbar-corner-color: #09100b;
    }
    #box-transcript { display: none; }
    #work-area { height: 1fr; }
    #work-area > RichLog, #work-area > TiledMonitor { width: 1fr; }
    #stream { height: auto; max-height: 5; color: #68e892; padding: 0 2; }
    #prompt {
        height: 5;
        color: #ffffff;
        border: solid #1f713c;
        margin: 0 1;
        background: #050b07;
    }
    #prompt:focus { border: solid #45e07a; }
    #fleet { height: 2; background: #07100a; color: #b7c3ba; padding: 0 1; }
    Footer { background: #0b1710; color: #dce7df; }
    FooterKey {
        background: #0b1710;
        .footer-key--key {
            color: #68e892;
            background: #0b1710;
        }
        .footer-key--description {
            color: #aab7ae;
            background: #0b1710;
        }
    }
    """
    BINDINGS = [
        Binding("alt+0", "orchestrator", "Orchestrator", show=False),
        Binding("alt+b", "switcher", "Boxes", show=True),
        Binding("alt+left", "monitor_page(-1)", "Previous tiles", show=False),
        Binding("alt+right", "monitor_page(1)", "Next tiles", show=False),
        Binding("escape", "monitor_back", "Composer", show=False),
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
        Binding("ctrl+c", "detach", "Detach", priority=True),
        Binding("ctrl+q", "detach", "Detach", show=False),
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
        self.in_box = False
        self.selected_view = "events"
        self.layout_mode = "focus"
        self._box_rendered = None
        self._visible_box_start = 0
        self.boxes = []
        self.stream_text = ""
        self._stream_suppressed = False
        self._connection_probe_lock = threading.Lock()
        self._slash_palette_open = False
        self._box_picker_open = False
        self._slash_palette_timer = None
        self._client_work = _OwnedClientWork()
        self._client_loop = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="dependency-rail")
            yield Static("ORCHESTRATOR", id="context")
            with Horizontal(id="work-area"):
                yield RichLog(id="transcript", markup=False, wrap=True, highlight=False)
                yield RichLog(id="box-transcript", markup=False, wrap=True, highlight=False)
                yield TiledMonitor(id="tiled-monitor")
            yield Static("", id="stream", markup=False)
            yield PromptArea(
                "", id="prompt", soft_wrap=True, show_line_numbers=False,
                placeholder="Message the orchestrator · Enter sends · Shift+Enter adds a line · / opens commands",
            )
            yield Static("orchestrator [Alt+0]", id="fleet", markup=False)
            yield Footer()

    async def on_mount(self) -> None:
        self._client_loop = asyncio.get_running_loop()
        self._render_existing_messages()
        self._render_dependency_rail()
        await self._refresh_fleet()
        self.query_one("#prompt", PromptArea).focus()
        self.set_interval(1.0, self._refresh_fleet)
        # Startup is cached-only, including when legacy callers pass
        # discover_connections=True. Refresh requires an explicit command/login.
        if self.show_boot:
            self.push_screen(BootScreen(self.boot_duration))

    def _render_existing_messages(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.write("CAMOL PRODUCT V0")
        if not self.controller.session["messages"]:
            log.write("No work starts from conversation alone. Type / for commands or /grill GOAL.")
            return
        log.write(
            "REATTACHED — status={} · model={} · effort={}".format(
                self.controller.session["status"],
                self.controller.session["model"],
                self.controller.session["effort"],
            )
        )
        log.write(
            "{} durable transcript entries retained but not replayed. Use /history to inspect them or /clear to reset this view.".format(
                len(self.controller.session["messages"])
            )
        )

    def _render_dependency_rail(self) -> None:
        records = {record["connection_id"]: record for record in self.controller.connections.load()}
        def provider(connection_id: str, binary: str) -> str:
            record = records.get(connection_id)
            if record:
                return ("! " if record["status"] in {"auth_required", "error"} else "□ ") + binary + " " + observation_label(record)
            return ("□ " + binary + " installed" if shutil.which(binary) else "· " + binary + " absent")
        parts = [
            "task unverified",
            "□ git installed" if shutil.which("git") else "· git absent",
            "□ docker installed; daemon?" if shutil.which("docker") else "· docker absent",
            provider("claude-cli", "claude"),
            provider("codex-cli", "codex"),
            *(["□ local " + observation_label(records["local-openai"])] if "local-openai" in records else []),
            *(["↻ explicit refresh"] if self.controller.connection_refresh_active.is_set() or self._connection_probe_lock.locked() else []),
            "model=" + self.controller.session["model"],
            "effort=" + self.controller.session["effort"],
        ]
        self.query_one("#dependency-rail", Static).update("  ".join(parts))
        self._render_orchestrator_context(records)

    def _render_orchestrator_context(self, records: Mapping[str, Mapping[str, str]]) -> None:
        if self.in_box:
            return
        model = self.controller.session["model"]
        provider = model.partition(":")[0]
        connection_id = {"claude": "claude-cli", "codex": "codex-cli"}.get(provider)
        record = records.get(connection_id) if connection_id else None
        suffix = " · " + observation_label(record) if record else " · connection unverified"
        suffix += " · task unverified"
        self.query_one("#context", Static).update("ORCHESTRATOR — model={}{}".format(model, suffix))

    def _probe_connections(
        self,
        *,
        login_provider: Optional[str] = None,
        login_returncode: Optional[int] = None,
    ) -> None:
        """Refresh only an explicitly chosen login provider; never on startup."""
        if login_provider not in {"claude", "codex"}:
            return
        ticket = self._client_work.reserve()
        if ticket is None:
            return
        def probe() -> None:
            if not self._client_work.start(ticket):
                return
            # Serialize explicit post-login observations rather than leaving a
            # queued login in an indeterminate "verifying" state.
            self._connection_probe_lock.acquire()
            refreshed = False
            try:
                try:
                    self.controller.connections.refresh(login_provider, cancel_event=self.controller._cancel_event)
                    refreshed = True
                except (ConnectionError, OSError, ValueError):
                    # A registry/probe failure must not make the client itself
                    # unavailable or promote an older authentication observation.
                    pass
            finally:
                self._connection_probe_lock.release()
                self._client_work.finish(ticket)
            # Release both probe serialization and persistence ownership before
            # scheduling a view update. No probe waits for an unmounting UI pump.
            self._schedule_ui(self._finish_connection_probe, login_provider, login_returncode, refreshed)

        threading.Thread(target=probe, name="camol-connection-probe", daemon=True).start()

    def _finish_connection_probe(
        self,
        login_provider: Optional[str],
        login_returncode: Optional[int],
        refresh_succeeded: bool = True,
    ) -> None:
        if self._client_work.closed:
            return
        self._render_dependency_rail()
        if login_provider is None:
            return
        if not refresh_succeeded:
            self.query_one("#transcript", RichLog).write("camol > Login status refresh failed; cached authentication is not a new confirmation. Use /connections refresh " + login_provider)
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
        try:
            boxes = await _await_view_worker(self.run_worker(
                self.controller.box_summaries, thread=True, group="fleet", exclusive=True
            ))
        except WorkerCancelled:
            # A newer exclusive refresh or client detach cancels this view
            # request. It is not a failure of the authoritative run.
            return
        # Detach/modal transitions can remove the target screen while this
        # read-only background query is pending. A disposable view is not a run
        # failure, and must not crash the client after a successful command.
        try:
            fleet = self.query_one("#fleet", Static)
        except NoMatches:
            return
        self.boxes = boxes
        items = ["ORCH[Alt+0]", "BOXES {}".format(len(boxes))]
        if any(box.get("organization_error") for box in boxes):
            items.append("[pane preferences unavailable]")
        selected_index = next((index for index, box in enumerate(boxes) if self.in_box and box["box_id"] == self.selected), 0)
        page_size = min(9, max(1, (self.size.width - 20) // 24))
        start = (selected_index // page_size) * page_size
        self._visible_box_start = start
        visible_boxes = boxes[start:start + page_size]
        for index, box in enumerate(visible_boxes, 1):
            mark = "!" if box["status"] in {"blocked", "waiting"} or box.get("runtime_wait") else "■" if box.get("connected") == "yes" else "□"
            shortcut = "Alt+{}".format(index)
            selected = ">" if self.in_box and self.selected == box["box_id"] else ""
            items.append("{}{}{} {}:{}({})".format(selected, "★" if box.get("pinned") else "", mark, index, box["box_id"], shortcut))
        if len(boxes) > len(visible_boxes):
            items.append("[{}–{}/{}; cycle [ ] or /box ID]".format(start + 1, start + len(visible_boxes), len(boxes)))
        fleet.update("  ".join(items))
        self._render_dependency_rail()
        if self.layout_mode != "focus" and not self.in_box:
            await self._refresh_monitor()
        if self.in_box:
            target, subview = self.selected, self.selected_view
            try:
                rendered = await _await_view_worker(self.run_worker(
                    lambda: self.controller.inspect_box(target, subview),
                    thread=True, group="box-view", exclusive=True,
                ))
            except WorkerCancelled:
                return
            if self.in_box and self.selected == target and self.selected_view == subview:
                if not self.query("#box-transcript"):
                    return
                self._render_box(target, subview, rendered)

    async def _refresh_monitor(self):
        try:
            snapshot = await _await_view_worker(self.run_worker(self.controller.read_monitor, thread=True,
                group="monitor", exclusive=True, exit_on_error=False))
        except WorkerCancelled:
            return
        except Exception as error:
            # Worker exceptions must remain a visible observation failure, not
            # a crash or a fallback to planned healthy tiles.
            if self.query("#tiled-monitor"):
                self.query_one(TiledMonitor).unavailable(Redactor().text(str(error)))
            return
        if self.layout_mode != "focus" and self.query("#tiled-monitor"):
            self.query_one(TiledMonitor).show_snapshot(snapshot, self.layout_mode)

    def _sync_layout(self):
        self.query_one("#transcript", RichLog).display = not self.in_box or self.layout_mode != "focus"
        self.query_one("#box-transcript", RichLog).display = self.in_box
        self.query_one(TiledMonitor).display = self.layout_mode != "focus" and not self.in_box

    def on_tiled_monitor_selected(self, message):
        import shlex
        self._submit("/switch --select {} --scope {}".format(shlex.quote(message.key), shlex.quote(message.scope)))

    def action_monitor_page(self, direction):
        if self.layout_mode != "focus" and not self.in_box and len(self.screen_stack) == 1:
            self.query_one(TiledMonitor).turn_page(direction)

    def action_monitor_back(self):
        if len(self.screen_stack) == 1:
            self.action_orchestrator()

    def action_submit(self) -> None:
        prompt = self.query_one("#prompt", PromptArea)
        text = prompt.text.strip()
        if not text:
            return
        prompt.clear()
        self._dispatch_input(text)

    def _dispatch_input(self, text: str) -> None:
        redactor = Redactor()
        visible_text = redactor.text(text) if redactor.contains_sensitive(text) else text
        self.history.append(visible_text)
        self.history = self.history[-200:]
        self.history_index = len(self.history)
        self.query_one("#transcript", RichLog).write("you > " + visible_text)
        self._submit(text)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "prompt":
            return
        if event.text_area.text == "/" and not self._slash_palette_open:
            # Delay just long enough to distinguish an intentional bare slash
            # from a pasted or quickly typed `/login claude` command. Opening a
            # modal between bytes would split the command across two widgets.
            self._slash_palette_timer = self.set_timer(0.12, self._open_slash_palette_if_still_bare)
        elif self._slash_palette_timer is not None:
            self._slash_palette_timer.stop()
            self._slash_palette_timer = None

    def _open_slash_palette_if_still_bare(self) -> None:
        self._slash_palette_timer = None
        prompt = self.query_one("#prompt", PromptArea)
        if prompt.text != "/" or self._slash_palette_open:
            return
        self._slash_palette_open = True
        self.push_screen(SlashCommandScreen(SLASH_COMMANDS), self._slash_command_selected)

    def _slash_command_selected(self, selection: Optional[SlashSelection]) -> None:
        self._slash_palette_open = False
        prompt = self.query_one("#prompt", PromptArea)
        if selection is None:
            prompt.focus()
            return
        command = selection.command
        text = selection.text
        has_arguments = bool(text.partition(" ")[2].strip())
        if command.run_from_palette or has_arguments:
            prompt.clear()
            self._dispatch_input(text)
            return
        value = command.command + (" " if command.takes_value else "")
        prompt.load_text(value)
        prompt.move_cursor((0, len(value)))
        prompt.focus()

    def _submit(self, text: str):
        ticket = self._client_work.reserve()
        if ticket is not None:
            return self._run_command(ticket, text)

    @work(thread=True, group="commands", exclusive=False)
    def _run_command(self, ticket, text: str) -> None:
        if not self._client_work.start(ticket):
            return
        stream_state = {"text": "", "suppressed": False}
        try:
            response = self.controller.handle(text, on_chunk=lambda chunk: self._stream_chunk(chunk, stream_state))
        finally:
            # Release persistence ownership before asking the event loop to
            # render: unmount must never deadlock against call_from_thread.
            self._client_work.finish(ticket)
        self._schedule_ui(self._apply_response, response)

    def _stream_chunk(self, chunk: str, stream_state=None) -> None:
        if self._client_work.closed:
            return
        state = stream_state if stream_state is not None else {"text": self.stream_text, "suppressed": self._stream_suppressed}
        if state["suppressed"]:
            return
        state["text"] += chunk
        if len(state["text"]) > 64000:
            state["suppressed"] = True
            state["text"] = ""
            if stream_state is None:
                self.stream_text, self._stream_suppressed = "", True
            self._schedule_stream("Provider output is lengthy; waiting for the final redacted response…")
            return
        # Keep the trailing token private until it is complete: credentials can
        # arrive split across arbitrary provider chunks.
        boundary = max((index for index, character in enumerate(state["text"]) if character.isspace()), default=-1)
        visible = Redactor().text(state["text"][:boundary + 1])[-4000:]
        if stream_state is None:
            self.stream_text, self._stream_suppressed = state["text"], state["suppressed"]
        self._schedule_stream("orchestrator ~ " + visible)

    def _schedule_stream(self, text):
        # Streaming must never hold controller persistence open while waiting
        # for an unmounting message pump to service a synchronous UI callback.
        def render():
            try:
                self.query_one("#stream", Static).update(text)
            except NoMatches:
                pass
        self._schedule_ui(render)

    def _schedule_ui(self, callback, *args):
        def render():
            if not self._client_work.closed:
                callback(*args)
        if self._client_loop is not None and not self._client_work.closed:
            try:
                # Enter the app's message pump, not the worker thread's copied
                # context. Modal composition needs Textual's active-app context.
                # This remains nonblocking: detached persistence never waits
                # for a UI callback during shutdown.
                self._client_loop.call_soon_threadsafe(self.call_later, render)
            except RuntimeError:
                pass

    def _apply_response(self, response: CommandResponse) -> None:
        if self._client_work.closed:
            return
        if response.focus_orchestrator:
            self.action_orchestrator()
        if response.layout is not None:
            self.layout_mode = response.layout
            self.action_orchestrator()
            if response.monitor is not None:
                self.query_one(TiledMonitor).show_snapshot(response.monitor, response.layout)
        self.query_one("#stream", Static).update("")
        log = self.query_one("#transcript", RichLog)
        if response.clear_transcript:
            log.clear()
        if response.box_id is not None:
            self._render_box(response.box_id, response.box_view or "events", "\n".join(response.messages))
            self.query_one("#prompt", PromptArea).focus()
            return
        if response.switcher is not None:
            if not self._box_picker_open:
                self._box_picker_open = True
                selected_key = "box:" + self.selected if self.in_box else "orchestrator"
                self.push_screen(BoxSwitcherScreen(response.switcher, selected_key), self._box_picker_selected)
            return
        if response.messages and self.in_box:
            # The composer addresses the orchestrator even while inspecting a
            # worker. Never hide replies, denials or approval prompts behind it.
            self.action_orchestrator()
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
            except KeyboardInterrupt:
                log.write("camol > Provider login cancelled; client detached safely.")
                self.exit()
                return
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
        # /login is an explicit request to enter the provider's native account
        # flow, even if an earlier discovery record is green. This makes account
        # switching and reconnecting predictable and guarantees that the user can
        # see the provider-owned URL/prompt.
        self._submit("/login " + provider)

    def action_orchestrator(self) -> None:
        self.selected = "orchestrator"
        self.in_box = False
        self.query_one("#transcript", RichLog).display = True
        self.query_one("#box-transcript", RichLog).display = False
        self._sync_layout()
        self._render_dependency_rail()
        self.query_one("#prompt", PromptArea).focus()

    def action_switcher(self):
        if not self._box_picker_open and len(self.screen_stack) == 1:
            self._submit("/switch")

    def _box_picker_selected(self, selection):
        self._box_picker_open = False
        self.query_one("#prompt", PromptArea).focus()
        if selection is not None:
            import shlex
            key, scope = selection
            self._submit("/switch --select {} --scope {}".format(shlex.quote(key), shlex.quote(scope)))

    def action_box(self, index: int) -> None:
        boxes = self.boxes
        index += self._visible_box_start
        if not 1 <= index <= len(boxes):
            self.notify("Box {} does not exist".format(index), severity="warning")
            return
        target = boxes[index - 1]["box_id"]
        self._submit("/box " + target)

    def _render_box(self, target: str, subview: str, rendered: str) -> None:
        self.selected = target
        self.in_box = True
        self.selected_view = subview
        self.query_one("#context", Static).update("BOX {} / {} — read-only evidence view".format(target, subview))
        self.query_one("#transcript", RichLog).display = False
        log = self.query_one("#box-transcript", RichLog)
        log.display = True
        self._sync_layout()
        if rendered != self._box_rendered:
            log.clear()
            log.write(rendered)
            self._box_rendered = rendered

    def action_cycle(self, direction: int) -> None:
        boxes = self.boxes
        choices = [None] + [item["box_id"] for item in boxes]
        current = choices.index(self.selected) if self.in_box and self.selected in choices else 0
        selected = choices[(current + direction) % len(choices)]
        if selected is None:
            self.action_orchestrator()
        else:
            self._submit("/box " + selected)

    def action_history(self, direction: int) -> None:
        if not self.history:
            return
        self.history_index = max(0, min(len(self.history), self.history_index + direction))
        value = "" if self.history_index == len(self.history) else self.history[self.history_index]
        prompt = self.query_one("#prompt", PromptArea)
        prompt.load_text(value)
        prompt.focus()

    def action_detach(self) -> None:
        self._client_work.close()
        self.controller.close_client()
        self.exit()

    async def on_unmount(self) -> None:
        self._client_work.close()
        self.controller.close_client()
        # Textual cancellation only cancels the Future, not its Python thread.
        # Wait for actual controller persistence to settle before callers may
        # release this session's files; never stop the authoritative supervisor.
        await asyncio.to_thread(self._client_work.idle.wait)


def run_tui(
    workspace: Path,
    *,
    state_root: Optional[Path] = None,
    show_boot: bool = True,
) -> int:
    controller = InteractiveController(workspace, state_root=state_root)
    CamolApp(controller, show_boot=show_boot).run()
    return 0
