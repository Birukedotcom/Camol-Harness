import asyncio
import time
import unittest

from textual.widgets import Input, OptionList, RichLog, Static

from camol.remote_monitor import RemoteMonitor
from camol.remote_tui import RemoteMonitorApp
from camol.ssh_protocol import SSHTransportError
from tests.test_remote_monitor import FakeClient


class RemoteTuiTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, pilot, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("remote UI did not settle")
            await pilot.pause(.02)

    async def test_paging_search_spaces_exact_selection_escape_and_ctrl_c(self):
        client = FakeClient()
        app = RemoteMonitorApp(RemoteMonitor(client), interval=300)
        async with app.run_test(size=(110, 30)) as pilot:
            await self.wait_for(pilot, lambda: app.snapshot is not None)
            self.assertEqual(len(app.page_rows), 50)
            await pilot.press("ctrl+n")
            self.assertEqual(len(app.page_rows), 10)
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: app.snapshot["selected_box"] == "box-050")
            self.assertIn("box-050", "\n".join(line.text for line in app.query_one("#remote-detail", RichLog).lines))
            search = app.query_one("#remote-search", Input)
            search.focus()
            await pilot.press("t", "a", "s", "k", "space", "1")
            self.assertEqual(search.value, "task 1")
            self.assertTrue(app.page_rows)
            await pilot.press("escape")
            await self.wait_for(pilot, lambda: app.snapshot["selected_box"] is None)
            await pilot.press("ctrl+c")
            self.assertTrue(app._closed)
        self.assertTrue(all(command in {"status", "box"} for command, _ in client.calls))

    async def test_disconnect_retains_labeled_data_without_promoting_it_to_current(self):
        client = FakeClient()
        app = RemoteMonitorApp(RemoteMonitor(client), interval=300)
        async with app.run_test(size=(85, 25)) as pilot:
            await self.wait_for(pilot, lambda: app.snapshot is not None)
            old = app.snapshot
            client.failure = SSHTransportError("HOST_IDENTITY_DENIED", "must not show raw secret banner")
            app.action_refresh()
            await self.wait_for(pilot, lambda: app.connection == "STALE")
            self.assertIs(app.snapshot, old)
            rendered = "\n".join(line.text for line in app.query_one("#remote-detail", RichLog).lines)
            self.assertIn("STALE", rendered)
            self.assertIn("HOST_IDENTITY_DENIED", rendered)
            self.assertNotIn("raw secret", rendered)
            client.failure = None
            app.action_refresh()
            await self.wait_for(pilot, lambda: app.connection == "OBSERVED")

    async def test_selection_during_refresh_cannot_publish_wrong_box_and_detach_cancels(self):
        client = FakeClient()
        app = RemoteMonitorApp(RemoteMonitor(client), interval=300)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.wait_for(pilot, lambda: app.snapshot is not None)
            client.wait = asyncio.Event()
            app.action_refresh()
            await pilot.pause(.02)
            await pilot.press("down", "enter")
            client.wait.set()
            await self.wait_for(pilot, lambda: app.snapshot["selected_box"] == "box-001")
            client.wait = asyncio.Event()
            app.action_refresh()
            await pilot.pause(.02)
            await pilot.press("ctrl+c")
            self.assertTrue(client.cancelled)
            self.assertTrue(app._job.done())

    async def test_unavailable_first_observation_has_no_local_fallback(self):
        client = FakeClient()
        client.failure = SSHTransportError("TRANSPORT_UNAVAILABLE", "raw remote error")
        app = RemoteMonitorApp(RemoteMonitor(client), interval=300)
        async with app.run_test(size=(60, 20)) as pilot:
            await self.wait_for(pilot, lambda: app.connection == "UNAVAILABLE")
            self.assertIsNone(app.snapshot)
            self.assertEqual(app.page_rows, [])

    async def test_failed_box_change_labels_the_actual_retained_selection(self):
        client = FakeClient()
        app = RemoteMonitorApp(RemoteMonitor(client), interval=300)
        async with app.run_test(size=(110, 30)) as pilot:
            await self.wait_for(pilot, lambda: app.snapshot is not None)
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: app.snapshot["selected_box"] == "box-000")
            client.failure = SSHTransportError("TRANSPORT_UNAVAILABLE", "safe fixture")
            await pilot.press("down", "enter")
            await self.wait_for(pilot, lambda: app.connection == "STALE")
            self.assertEqual(app.snapshot["selected_box"], "box-000")
            self.assertEqual(app.selected_box, "box-001")
            self.assertIn("requested box-001 not observed", str(app.query_one("#remote-header", Static).render()))
