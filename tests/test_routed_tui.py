import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from textual.widgets import RichLog, Static

from camol.conversation import ConversationReply
from camol.routed_chat import RoutedChatController
from camol.routed_tui import RoutedChatApp
from camol.tui import PromptArea
from tests.test_classifier_preview import result


class RoutedTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_generates_answer_in_native_view_and_prompt_is_inspectable(self):
        with tempfile.TemporaryDirectory() as directory:
            generate = Mock(return_value=ConversationReply("Plan: keep arrows and A/D.", "claude", "sonnet", "claude-test", 50, 10))
            controller = RoutedChatController(Mock(return_value=result("qwen", "plan")), [], warm=lambda: None,
                                              workspace=Path(directory) / "work", state_root=Path(directory) / "state",
                                              converse_fn=generate)
            app = RoutedChatApp(controller, show_boot=False, discover_connections=False)
            async with app.run_test(size=(100, 32)) as pilot:
                async def until(predicate):
                    deadline = time.monotonic() + 4
                    while not predicate():
                        if time.monotonic() > deadline:
                            self.fail("routed view did not settle")
                        await pilot.pause(.02)
                    await pilot.pause(.05)

                await until(lambda: controller.ready)
                app.query_one("#prompt", PromptArea).load_text("Plan 3D ping-pong with arrows and A/D")
                await pilot.press("enter")
                await until(lambda: "completed" in str(app.query_one("#result-generator", Static).render()))
                transcript = lambda: "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)
                self.assertIn("Plan: keep arrows", transcript())
                self.assertIn("plan", str(app.query_one("#result-qwen", Static).render()))
                self.assertEqual(transcript().count("CAMOL / QWEN ROUTED CHAT"), 1)
                self.assertTrue(generate.call_args.kwargs["no_tools"])
                app.action_show_prompt()
                await until(lambda: "user_request" in transcript())
                await pilot.resize_terminal(80, 24)
                await pilot.pause(.1)
                self.assertGreaterEqual(app.query_one("#classifier-panel").region.y, app.query_one("#transcript").region.bottom)


if __name__ == "__main__":
    unittest.main()
