"""Classifier comparison inside Camol's native terminal client."""

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, RichLog, Static

from .classifier_preview import PREVIEW_COMMANDS
from .tui import CamolApp, PromptArea, SlashCommandScreen


class ClassifierPreviewApp(CamolApp):
    SUB_TITLE = "local classifier preview"
    CSS = CamolApp.CSS + """
    #classifier-panel { width: 44%; min-width: 28; height: 1fr; background: #07100a; padding: 0 1; }
    #classifier-heading { height: auto; color: #68e892; text-style: bold; padding: 1 0; }
    .classifier-card { height: auto; min-height: 8; border: round #17462a; padding: 0 1; margin-bottom: 1; }
    .classifier-card .model-name { height: 1; color: #68e892; text-style: bold; }
    .classifier-result { height: auto; }
    #classifier-note { height: auto; color: #83a58d; padding-bottom: 1; }
    #classifier-context { height: auto; max-height: 4; color: #b7c3ba; padding-bottom: 1; }
    .preview-narrow #classifier-panel { width: 100%; height: 2fr; min-height: 5; }
    .preview-narrow #work-area { layout: vertical; }
    .preview-narrow #work-area > #transcript { width: 100%; min-height: 4; }
    """
    BINDINGS = [
        Binding("alt+b", "routes", "Routes"),
        Binding("f2", "example", "Try example"),
        Binding("ctrl+l", "clear_preview", "Clear"),
        Binding("ctrl+c", "detach", "Exit", priority=True),
    ]

    async def on_mount(self, event):
        # Textual walks inherited message handlers itself. We explicitly call
        # Camol's mount after adding the panel, so stop a second inherited call.
        event.prevent_default()
        panel = VerticalScroll(id="classifier-panel")
        await self.query_one("#work-area").mount(panel)
        await panel.mount(Static("ROUTE COMPARISON", id="classifier-heading"))
        for name, title in (("small", "GLiClass Small"), ("qwen", "GLiClass Qwen 0.5B")):
            card = Vertical(classes="classifier-card")
            await panel.mount(card)
            await card.mount(Static(title, classes="model-name", markup=False),
                             Static("Loading local weights…", id="result-" + name,
                                    classes="classifier-result", markup=False))
        await panel.mount(Static("Scores rank these candidate routes. They are not certainty or permission to execute.", id="classifier-note"),
                          Static("Task context: none · use /state TEXT", id="classifier-context", markup=False))
        self.query_one("#prompt", PromptArea).placeholder = "Test a request · Enter compares · Shift+Enter adds a line · / opens preview commands"
        self.query_one("#transcript", RichLog).min_width = 0
        self.query_one(Footer).compact = True
        await super().on_mount()
        self._resize_preview()
        self._submit("/load")

    def _render_existing_messages(self):
        log = self.query_one("#transcript", RichLog)
        log.write(Text("CAMOL / CLASSIFIER PREVIEW", style="bold #68e892"))
        log.write("Your request → candidate procedure → review the two proposals.")
        log.write("Try: Review this pull request for bugs and regressions.")
        log.write("Use /state to supply context for follow-ups such as ‘Do it.’")
        log.write("Local models only. Nothing is dispatched or saved to your harness session.")

    def _render_dependency_rail(self):
        self.query_one("#dependency-rail", Static).update("CAMOL  ·  CLASSIFIER PREVIEW  ·  LOCAL CPU  ·  NO EXECUTION")
        self.query_one("#context", Static).update("ORCHESTRATOR — compare a proposed procedure before taking action")

    async def _refresh_fleet(self):
        if not self.query("#fleet"):
            return
        state = "loading" if not self.controller.ready else "ready"
        if self.controller.load_failed:
            state = "unavailable"
        if self.controller._command_lock.locked():
            state = "working"
        self.query_one("#fleet", Static).update("SMALL [{}]  ·  QWEN [{}]  ·  preview models; no worker boxes".format(state, state))
        self._render_dependency_rail()

    def _open_slash_palette_if_still_bare(self):
        self._slash_palette_timer = None
        prompt = self.query_one("#prompt", PromptArea)
        if prompt.text != "/" or self._slash_palette_open:
            return
        self._slash_palette_open = True
        self.push_screen(SlashCommandScreen(PREVIEW_COMMANDS), self._slash_command_selected)

    def _submit(self, text):
        if not self.controller._command_lock.locked():
            if text == "/load" or text == "/example" or not text.startswith("/"):
                self.query_one("#stream", Static).update("Loading local classifiers…" if text == "/load" else "Comparing the same request with both models…")
        return super()._submit(text)

    def _apply_response(self, response):
        if self._client_work.closed:
            return
        if getattr(response, "reset_results", False):
            for name in ("small", "qwen"):
                self.query_one("#result-" + name, Static).update("Waiting for a request.")
        for row in getattr(response, "comparison", None) or ():
            result = Text()
            result.append((row["proposed_route"] or "ABSTAIN") + "\n", style="bold #68e892" if row["proposed_route"] else "bold yellow")
            result.append("{:.1f} ms · {} tokens\n".format(row["latency_ms"], row["tokens"]), style="#83a58d")
            for score in row["scores"][:4]:
                result.append("{:.3f}  {}\n".format(score["score"], score["route"]))
            if row["abstained"]:
                result.append("Too uncertain to propose a route.", style="yellow")
            self.query_one("#result-" + row["model"], Static).update(result)
        if self.controller.ready and not getattr(response, "comparison", None):
            for name in ("small", "qwen"):
                widget = self.query_one("#result-" + name, Static)
                if "Loading local weights" in str(widget.render()):
                    widget.update("Ready — enter a request.")
        self.query_one("#classifier-context", Static).update("Task context: " + (self.controller.state or "none · use /state TEXT"))
        super()._apply_response(response)

    def action_example(self):
        self._dispatch_input("/example")

    def action_routes(self):
        self._dispatch_input("/routes")

    def action_clear_preview(self):
        self._dispatch_input("/clear")

    def on_resize(self):
        self._resize_preview()

    def _resize_preview(self):
        self.set_class(self.size.width < 85, "preview-narrow")


def run_preview(controller, *, show_boot=True):
    ClassifierPreviewApp(controller, show_boot=show_boot, discover_connections=False).run()
    return 0
