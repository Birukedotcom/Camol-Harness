import tempfile
import unittest
from pathlib import Path

from camol.app import InteractiveController
from camol.tui import CamolApp, PromptArea


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.controller = InteractiveController(self.workspace, state_root=root / "state")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_terminal_app_has_transcript_multiline_prompt_and_fleet(self):
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#prompt", PromptArea).styles.height.value, 5)
            self.assertIn("ORCH[Alt+0]", str(app.query_one("#fleet").render()))
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/help")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("/grill GOAL", rendered)
            prompt.load_text("sk-live-abcdefghijklmnopqrstuvwxyz123456")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
            self.assertIn("[REDACTED]", rendered)
            prompt.load_text("MY_TOKEN=abcdefghijk")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertNotIn("abcdefghijk", rendered)
            self.assertEqual(app.history[-1], "MY_TOKEN=[REDACTED]")

    async def test_n_box_fleet_and_keyboard_navigation(self):
        for command in (
            "/model claude:fable",
            "/grill build",
            "done",
            "no deploy",
            "preserve secrets",
            "a | first\nb | second | after=a",
            "python3 -m unittest",
            "boxes=2 turns=3 tokens=10000 cost_cents=100 turn_timeout_seconds=600",
        ):
            self.controller.handle(command)
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause(0.2)
            fleet = str(app.query_one("#fleet").render())
            self.assertIn("builder", fleet)
            self.assertIn("builder-2", fleet)
            app.action_box(2)
            await pilot.pause(0.2)
            self.assertEqual(app.selected, "builder-2")
            self.assertIn("BOX builder-2", str(app.query_one("#context").render()))


if __name__ == "__main__":
    unittest.main()
