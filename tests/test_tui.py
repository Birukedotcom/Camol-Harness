import tempfile
import asyncio
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Input, OptionList

from camol.app import CommandResponse, InteractiveController
from camol.connections import _record
from camol.tui import CamolApp, LoginProviderScreen, PromptArea, SlashCommandScreen


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
            self.assertEqual(transcript_styles.scrollbar_background.hex, "#09100B")
            self.assertEqual(transcript_styles.scrollbar_color.hex, "#1F713C")
            self.assertEqual(app.query_one("#prompt", PromptArea).styles.background.hex, "#050B07")
            self.assertEqual(app.screen.styles.background.hex, "#030604")
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
            self.assertTrue(app.query_one("#box-transcript").display)
            self.assertFalse(app.query_one("#transcript").display)
            app.action_orchestrator()
            self.assertTrue(app.query_one("#transcript").display)

    async def test_stream_waits_for_split_secret_then_redacts_before_display(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await asyncio.to_thread(app._stream_chunk, "response sk-live-abc")
            self.assertNotIn("sk-live-abc", str(app.query_one("#stream").render()))
            await asyncio.to_thread(app._stream_chunk, "defghijklmnopqrstuvwxyz123456 ")
            rendered = str(app.query_one("#stream").render())
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
            self.assertIn("[REDACTED]", rendered)

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

    async def test_typing_slash_opens_command_palette_and_enter_runs_safe_command(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, SlashCommandScreen)
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/skills")

    async def test_palette_filters_as_the_user_types_a_command(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            command_input = app.screen.query_one("#command-input", Input)
            await pilot.press("s", "k", "i", "l", "l", "s")
            self.assertEqual(command_input.value, "/skills")
            self.assertEqual(app.screen.query_one("#command-options", OptionList).option_count, 1)
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/skills")

    async def test_space_stays_in_typed_command_instead_of_scrolling_the_palette(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            command_input = app.screen.query_one("#command-input", Input)
            await pilot.press("g", "r", "i", "l", "l", "space", "b", "u", "i", "l", "d")
            self.assertEqual(command_input.value, "/grill build")
            self.assertIsInstance(app.screen, SlashCommandScreen)
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/grill build")

    async def test_enter_sends_and_shift_enter_adds_a_line(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("first")
            prompt.move_cursor((0, len(prompt.text)))
            await pilot.press("shift+enter")
            await pilot.press("s", "e", "c", "o", "n", "d")
            self.assertEqual(prompt.text, "first\nsecond")
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("first\nsecond")

    async def test_reopen_shows_a_reattach_boundary_without_replaying_old_output(self):
        self.controller.handle("/btw retained context")
        reopened = InteractiveController(self.workspace, state_root=Path(self.temporary.name) / "state")
        app = CamolApp(reopened, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("REATTACHED", rendered)
            self.assertIn("durable transcript entries retained", rendered)
            self.assertNotIn("retained context", rendered)

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

    async def test_picker_reconnects_even_when_discovery_already_reports_connected(self):
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
            app._submit.assert_called_once_with("/login codex")

    async def test_control_c_during_native_login_detaches_without_a_traceback(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)):
            with (
                patch.object(app, "suspend", return_value=nullcontext()),
                patch("camol.tui.subprocess.run", side_effect=KeyboardInterrupt),
            ):
                app._apply_response(
                    CommandResponse(
                        login_argv=("claude", "auth", "login"),
                        login_provider="claude",
                    )
                )
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("Provider login cancelled; client detached safely", rendered)


if __name__ == "__main__":
    unittest.main()
