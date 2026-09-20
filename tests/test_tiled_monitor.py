import copy
import unittest
from unittest.mock import Mock

from camol.app import InteractiveError
from camol.tui import CamolApp, PromptArea
from camol.tiled_monitor import TiledMonitor, MonitorTile
from textual.widgets import RichLog, Static
from tests import test_pane_layout


class TiledMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_pane_layout.PaneLayoutTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller

    async def wait_for(self, pilot, predicate):
        for _ in range(100):
            if predicate():
                return
            await pilot.pause(.03)
        self.fail("terminal state did not settle")

    async def test_grid_selection_escape_and_focus_keep_composer_and_draft(self):
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(180, 45)) as pilot:
            prompt = app.query_one(PromptArea)
            prompt.load_text("/layout grid")
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: app.layout_mode == "grid")
            monitor = app.query_one(TiledMonitor)
            await pilot.pause()
            self.assertTrue(monitor.display)
            self.assertTrue(app.query_one("#transcript", RichLog).display)
            self.assertIn("plan_only", str(monitor.query_one(Static).render()))
            prompt.load_text("my unsent note with spaces")
            await app._refresh_monitor()
            self.assertEqual(prompt.text, "my unsent note with spaces")
            tile = next(button for button in monitor.query(MonitorTile) if button.display)
            expected = tile.address[0][4:]
            tile.focus()
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: app.in_box)
            self.assertEqual(app.selected, expected)
            self.assertTrue(app.query_one("#transcript", RichLog).display)
            self.assertTrue(app.query_one("#box-transcript", RichLog).display)
            self.assertFalse(monitor.display)
            self.assertEqual(prompt.text, "my unsent note with spaces")
            await pilot.press("escape")
            self.assertFalse(app.in_box)
            self.assertTrue(monitor.display)
            self.assertIs(app.focused, prompt)
            app._apply_response(self.controller.handle("/layout focus"))
            self.assertFalse(monitor.display)
            self.assertEqual(prompt.text, "my unsent note with spaces")
            self.assertIsNone(self.controller.store.load()["approved_digest"])

    async def test_failure_disables_tiles_and_resize_cannot_resurrect_them(self):
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(140, 35)) as pilot:
            app._apply_response(self.controller.handle("/layout split"))
            monitor = app.query_one(TiledMonitor)
            self.controller.read_monitor = Mock(side_effect=InteractiveError("corrupt ledger"))
            await app._refresh_monitor()
            await pilot.pause()
            self.assertIsNone(monitor.snapshot)
            monitor.render_page()
            self.assertFalse(monitor.addresses)
            self.assertTrue(all(button.disabled for button in monitor.query(MonitorTile)))
            self.assertIn("UNAVAILABLE", str(monitor.query_one(Static).render()))
            self.assertIsNotNone(app.query_one(PromptArea))

    async def test_many_boxes_page_and_queued_selection_captures_original_identity(self):
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(220, 45)) as pilot:
            response = self.controller.handle("/layout grid")
            snapshot = copy.deepcopy(response.monitor)
            prototype = snapshot["rows"][0]
            snapshot["rows"] = [dict(prototype, key="box:worker-{}".format(i), box_id="worker-{}".format(i)) for i in range(77)]
            self.controller.read_monitor = Mock(return_value=snapshot)
            app._apply_response(response)
            monitor = app.query_one(TiledMonitor)
            monitor.show_snapshot(snapshot)
            await pilot.pause()
            app.action_monitor_page(1)
            self.assertEqual(monitor.number, 2)
            captured = []
            app._submit = Mock(side_effect=captured.append)
            tile = next(button for button in monitor.query(MonitorTile) if button.display)
            original = tile.address
            tile.press()
            monitor.turn_page(1)
            await pilot.pause()
            self.assertEqual(len(captured), 1)
            self.assertIn(original[0], captured[0])
            self.assertIn(original[1], captured[0])
            self.assertNotEqual(tile.address, original)
