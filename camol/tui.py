"""Textual terminal client for Camol's detachable orchestrator."""

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static, TextArea

from .app import CommandResponse, InteractiveController
from .boot import compose_boot


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
            self.set_timer(self.duration, self.dismiss)

    def on_resize(self) -> None:
        self._render_art()

    def _render_art(self) -> None:
        self.query_one("#boot-art", Static).update(compose_boot(self.size.width, self.size.height - 1))

    def action_dismiss(self) -> None:
        self.dismiss()


class PromptArea(TextArea):
    """A multiline editor whose submission chord belongs to the app."""


class CamolApp(App):
    TITLE = "Camol"
    SUB_TITLE = "persistent orchestration harness"
    CSS = """
    Screen { background: #090b0c; color: #e8eceb; }
    BootScreen { align: left top; padding: 1 1; }
    #boot-art { width: 100%; height: 100%; color: #f4f5f4; }
    #dependency-rail { height: 1; background: #111719; color: #9ca9a6; padding: 0 1; }
    #context { height: 1; background: #182022; color: #d5dfdc; padding: 0 1; }
    #transcript { height: 1fr; padding: 1 2; scrollbar-color: #6e7d79; }
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
    ):
        super().__init__()
        self.controller = controller
        self.show_boot = show_boot
        self.boot_duration = boot_duration
        self.history = []
        self.history_index = 0
        self.selected = "orchestrator"
        self.boxes = []
        self.stream_text = ""

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
        self.history.append(text)
        self.history = self.history[-200:]
        self.history_index = len(self.history)
        self.query_one("#transcript", RichLog).write("you > " + text)
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
        if response.login_argv:
            with self.suspend():
                completed = subprocess.run(list(response.login_argv), check=False)
            log.write("camol > provider login exited {}. Run /connections to refresh status.".format(completed.returncode))
        if response.exit_client:
            self.exit()
            return
        self.query_one("#prompt", PromptArea).focus()
        self.call_later(self._refresh_fleet)

    def action_orchestrator(self) -> None:
        self.selected = "orchestrator"
        self.query_one("#context", Static).update("ORCHESTRATOR")
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
