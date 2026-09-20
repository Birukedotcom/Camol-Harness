"""Native Camol view for local Qwen routing and a connected text generator."""

from rich.text import Text
from textual.binding import Binding
from textual.widgets import RichLog, Static

from .classifier_tui import ClassifierPreviewApp
from .routed_chat import CHAT_COMMANDS
from .tui import CamolApp


class RoutedChatApp(ClassifierPreviewApp):
    SUB_TITLE = "Qwen routed chat"
    CSS = ClassifierPreviewApp.CSS
    MODEL_CARDS = (("qwen", "GLiClass Qwen · request router"), ("generator", "Connected LLM · response"))
    COMMANDS = CHAT_COMMANDS
    PANEL_TITLE = "REQUEST → PROMPT → RESPONSE"
    PANEL_NOTE = "Qwen suggests a procedure. Your original request stays in the LLM prompt. Scores are not certainty."
    PLACEHOLDER = "Ask Camol · Enter sends · Shift+Enter adds a line · /prompt inspects the last prompt"
    BINDINGS = [
        Binding("alt+b", "show_prompt", "View prompt"),
        Binding("f2", "example", "Ping-pong plan"),
        Binding("ctrl+l", "clear_preview", "New conversation"),
        Binding("ctrl+c", "detach", "Exit", priority=True),
    ]

    def _render_existing_messages(self):
        log = self.query_one("#transcript", RichLog)
        log.write(Text("CAMOL / QWEN ROUTED CHAT", style="bold #68e892"))
        log.write("Your request → local Qwen route → focused prompt → connected LLM response.")
        log.write("Generator: " + self.controller.model + ". Text responses only; no tool execution.")
        log.write("F2 tries the 3D ping-pong plan. /prompt shows the prompt; /model selects the LLM.")
        log.write("Saved conversation and routing logs: " + str(self.controller.store.project_dir))
        for message in self.controller.session["messages"][-12:]:
            log.write(Text(message["role"] + " > " + message["content"]))

    def _render_dependency_rail(self):
        self.query_one("#dependency-rail", Static).update("CAMOL  ·  QWEN ROUTING  ·  " + self.controller.model + "  ·  NO TOOLS")
        self.query_one("#context", Static).update("ORCHESTRATOR — preserve the request, focus the response")

    async def _refresh_fleet(self):
        if not self.query("#fleet"):
            return
        state = "ready" if self.controller.ready else "loading"
        if self.controller.load_failed:
            state = "unavailable"
        if self.controller._command_lock.locked():
            state = "working"
        self.query_one("#fleet", Static).update("QWEN [{}]  ·  GENERATOR [{}]  ·  /prompt inspects routing".format(state, self.controller.model))
        self._render_dependency_rail()

    def _submit(self, text):
        if not self.controller._command_lock.locked():
            if text == "/load" or text == "/example" or not text.startswith("/"):
                self.query_one("#stream", Static).update("Loading local Qwen…" if text == "/load" else "Routing your request, then asking " + self.controller.model + "…")
        return CamolApp._submit(self, text)

    def _apply_response(self, response):
        if self._client_work.closed:
            return
        super()._apply_response(response)
        generation = getattr(response, "generation", None)
        card = self.query_one("#result-generator", Static)
        if generation:
            card.update("{}\n{}\nInput tokens: {} · output: {}\n/prompt shows the assembled request.".format(
                generation["model"], generation["status"],
                generation["input_tokens"] if generation["input_tokens"] is not None else "unknown",
                generation["output_tokens"] if generation["output_tokens"] is not None else "unknown"))
        elif getattr(response, "reset_results", False):
            card.update(self.controller.model + "\nSee the conversation for status.")
        elif "Ready" in str(card.render()) or "Loading" in str(card.render()):
            card.update(self.controller.model + "\nWaiting for your request.\nUse /model to change the generator.")

    def action_show_prompt(self):
        self._dispatch_input("/prompt")


def run_chat(controller, *, show_boot=True):
    RoutedChatApp(controller, show_boot=show_boot, discover_connections=False).run()
    return 0
