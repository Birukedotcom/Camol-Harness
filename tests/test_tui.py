import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from camol.app import InteractiveController
from camol.connections import _record
from camol.tui import CamolApp, LoginProviderScreen, PromptArea


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
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#prompt", PromptArea).styles.height.value, 5)
            transcript_styles = app.query_one("#transcript").styles
            self.assertEqual(transcript_styles.scrollbar_size_vertical, 1)
            self.assertEqual(transcript_styles.scrollbar_background.hex, "#1E1E1E")
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

    async def test_boot_timer_dismisses_only_the_boot_screen(self):
        app = CamolApp(
            self.controller,
            show_boot=True,
            boot_duration=0.01,
            discover_connections=False,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.screen.id, "_default")
            self.assertIsNotNone(app.query_one("#prompt", PromptArea))

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
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause(0.2)
            fleet = str(app.query_one("#fleet").render())
            self.assertIn("builder", fleet)
            self.assertIn("builder-2", fleet)
            app.action_box(2)
            await pilot.pause(0.2)
            self.assertEqual(app.selected, "builder-2")
            self.assertIn("BOX builder-2", str(app.query_one("#context").render()))

    async def test_connection_inventory_refreshes_in_background_on_startup(self):
        records = [
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="ready",
                runtime="claude",
                detail="Authenticated Claude CLI",
            )
        ]
        original_probe = self.controller.connections.probe_all

        def probe_all():
            self.controller.connections.save(records)
            return records

        self.controller.connections.probe_all = Mock(side_effect=probe_all)
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            rail = str(app.query_one("#dependency-rail").render())
            self.assertIn("■ claude", rail)
            self.controller.connections.probe_all.assert_called_once_with()
        self.controller.connections.probe_all = original_probe

    async def test_login_opens_keyboard_picker_and_enter_selects_provider(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._apply_response(self.controller.handle("/login"))
            await pilot.pause()
            self.assertIsInstance(app.screen, LoginProviderScreen)
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/login codex")

    async def test_confirmed_login_activates_model_and_populates_orchestrator(self):
        record = _record(
            "claude-cli",
            "anthropic",
            "cli",
            status="ready",
            runtime="claude",
            detail="Authenticated Claude CLI",
        )
        self.controller.connections.save([record])
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._finish_connection_probe("claude", 0)
            await pilot.pause()
            self.assertEqual(self.controller.session["model"], "claude:fable")
            self.assertIn("connected", str(app.query_one("#context").render()))
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("Claude connection confirmed", rendered)
            self.assertIn("model set to claude:fable", rendered)

    async def test_picker_reuses_a_connected_account_without_reauthentication(self):
        self.controller.connections.save([
            _record(
                "codex-cli",
                "openai",
                "cli",
                status="ready",
                runtime="codex",
                detail="Authenticated Codex CLI",
            )
        ])
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._apply_response(self.controller.handle("/login"))
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_not_called()
            self.assertEqual(self.controller.session["model"], "codex")
            self.assertIn("connected", str(app.query_one("#context").render()))


if __name__ == "__main__":
    unittest.main()
