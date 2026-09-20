import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from camol.app import InteractiveError
from camol.overview import planned_state
from camol.pane_layout import monitor_snapshot, page, tile_label
from camol.schema import canonical_digest
from tests import test_pane_switcher


class PaneLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_pane_switcher.PaneSwitcherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.addCleanup(self.controller.close_client)

    def test_projection_is_bounded_metadata_and_pages_arbitrary_box_counts(self):
        state = planned_state(self.fixture.document)
        prototype = next(iter(state["agents"].values()))
        state["agents"] = {"box-{:03d}".format(i): copy.deepcopy(prototype) for i in range(77)}
        state["evidence"] = {"secret": "never present in tiles"}
        snapshot = monitor_snapshot(self.controller.session, state, "plan_only")
        self.assertEqual(len(snapshot["rows"]), 77)
        self.assertNotIn("never present", json.dumps(snapshot))
        first = page(snapshot, "grid", 1, 120, 50)
        self.assertEqual(len(first["items"]), 6)
        last = page(snapshot, "grid", 999, 120, 50)
        self.assertEqual(last["number"], 13)
        self.assertEqual(len(last["items"]), 5)
        self.assertEqual(len(page(snapshot, "split", 1, 80, 50)["items"]), 1)
        for mode, number in (("other", 1), ("grid", True), ("grid", 0)):
            with self.assertRaises(ValueError):
                page(snapshot, mode, number, 100, 30)

    def test_layout_changes_no_plan_or_approval_and_stale_selection_is_denied(self):
        before = self.controller.store.load()
        response = self.controller.handle("/layout grid")
        self.assertEqual(response.layout, "grid")
        self.assertEqual(response.monitor["basis"], "plan_only")
        after = self.controller.store.load()
        for key in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status", "selected_box"):
            self.assertEqual(before[key], after[key])
        self.assertFalse(Path(before["state_dir"]).exists())
        self.assertEqual(self.controller.read_monitor(), response.monitor)
        self.controller._command_lock.acquire()
        try:
            with self.assertRaises(InteractiveError):
                self.controller.read_monitor()
        finally:
            self.controller._command_lock.release()
        row = response.monitor["rows"][0]
        changed_plan = copy.deepcopy(self.controller.session["plan"])
        changed_plan["execution_limitation"] = "changed plan review"
        self.controller.session = self.controller.store.update(self.controller.session, plan=changed_plan, plan_digest=canonical_digest(changed_plan))
        denied = self.controller.handle("/switch --select {} --scope {}".format(row["key"], response.monitor["scope"]))
        self.assertIn("scope changed", denied.messages[0])
        self.controller.spawn_fn.assert_not_called()
        self.controller.converse_fn.assert_not_called()
        self.controller.connections.refresh.assert_not_called()

    def test_corrupt_ledger_cannot_be_presented_as_a_healthy_plan(self):
        directory = Path(self.controller.session["state_dir"])
        directory.mkdir(parents=True)
        (directory / "camol.sqlite3").write_text("not a ledger")
        response = self.controller.handle("/layout grid")
        self.assertIsNone(response.monitor)
        self.assertIn("unreadable", response.messages[0])
        self.assertIsNone(self.controller.handle("/layout focus").monitor)

    def test_tile_labels_keep_markup_literal_and_show_gate_wait_and_recorded_usage(self):
        state = planned_state(self.fixture.document)
        box, agent = next(iter(state["agents"].items()))
        task_id = next(iter(state["tasks"]))
        agent.update(task_id=task_id, stats=dict(tokens=120, turns=2))
        state["tasks"][task_id].update(status="waiting", runtime_wait=dict(code="[red] BUDGET\x1b"), gate_wait=dict(phase="candidate"))
        row = next(row for row in monitor_snapshot(self.controller.session, state, "ledger_snapshot")["rows"] if row["box_id"] == box)
        label = tile_label(row, 40)
        self.assertIn("candidate", label)
        self.assertIn("[red] BUDGET", label)
        self.assertIn("120 tokens · 2 turns · cursor 40", label)
        self.assertNotIn("\x1b", label)
