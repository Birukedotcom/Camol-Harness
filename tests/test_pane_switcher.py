import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from camol.app import InteractiveController
from camol.overview import planned_state
from camol.pane_switcher import filter_rows, row_label, switch_snapshot
from camol.schema import canonical_digest
from camol.store import SQLiteEventStore
from camol.orchestrator import Orchestrator


class PaneSwitcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.controller = InteractiveController(self.workspace, state_root=self.root / "state")
        self.document = json.loads((Path(__file__).resolve().parents[1] / "examples/local-n-box-runbook.json").read_text())
        self.plan = dict(schema="camol.product_plan", schema_version=2, proposal=None,
            run_id=self.document["run"]["id"], runbook=self.document, execution_status="ready",
            execution_limitation="fixture", source=dict(workspace=str(self.workspace), revision="a" * 40))
        self.controller.session = self.controller.store.update(self.controller.session,
            plan=self.plan, plan_digest=canonical_digest(self.plan), run_id=self.plan["run_id"])
        self.controller.spawn_fn = Mock(side_effect=AssertionError("navigation cannot spawn"))
        self.controller.connections.refresh = Mock(side_effect=AssertionError("navigation cannot probe accounts"))
        self.controller.converse_fn = Mock(side_effect=AssertionError("navigation cannot call a model"))

    def test_snapshot_has_exact_stable_identity_and_no_worker_content(self):
        state = planned_state(self.document)
        state["evidence"] = {"raw": "never preview raw credentials or tool output"}
        snapshot = switch_snapshot(self.controller.session, state, "plan_only")
        state["agents"] = dict(reversed(list(state["agents"].items())))
        self.assertEqual(snapshot, switch_snapshot(self.controller.session, state, "plan_only"))
        self.assertNotIn("never preview", json.dumps(snapshot))
        state["agents"].pop(next(iter(state["agents"])))
        self.assertNotEqual(snapshot["scope"], switch_snapshot(self.controller.session, state, "plan_only")["scope"])

    def test_77_boxes_attention_group_and_multitoken_literal_search(self):
        state = planned_state(self.document)
        prototype = next(iter(state["agents"].values()))
        state["agents"] = {"worker-{:03d}".format(i): dict(prototype) for i in range(77)}
        task_id = next(iter(state["tasks"]))
        state["agents"]["worker-075"]["task_id"] = task_id
        state["tasks"][task_id].update(status="waiting", gate_wait=dict(phase="candidate"))
        snapshot = switch_snapshot(self.controller.session, state, "ledger_snapshot")
        self.assertEqual(len(snapshot["rows"]), 78)
        self.assertEqual(snapshot["rows"][1]["box_id"], "worker-075")
        matches = filter_rows(snapshot["rows"], "WORKER-075 attention waiting")
        self.assertEqual(len(matches), 1)
        self.assertEqual(filter_rows(snapshot["rows"], "[.*]"), [])
        with self.assertRaises(ValueError):
            filter_rows(snapshot["rows"], "x" * 257)

    def test_terminal_controls_and_rich_markup_are_not_interpreted(self):
        state = planned_state(self.document)
        agent = next(iter(state["agents"].values()))
        agent["status"] = "[red]unsafe\x1b]52;payload\x07"
        rows = switch_snapshot(self.controller.session, state, "plan_only")["rows"]
        labels = "\n".join(row_label(row) for row in rows)
        self.assertNotIn("\x1b", labels)
        self.assertNotIn("\x07", labels)
        self.assertIn("[red]", labels)  # rendered as literal Rich Text by the TUI

    def test_command_selection_is_read_only_and_persists_only_view_target(self):
        before = self.controller.store.load()
        response = self.controller.handle("/switch")
        self.assertIsNotNone(response.switcher)
        row = response.switcher["rows"][1]
        chosen = self.controller.handle("/switch --select {} --scope {}".format(row["key"], response.switcher["scope"]))
        self.assertEqual(chosen.box_id, row["box_id"])
        self.assertIn("dormant or unavailable", chosen.messages[0])
        self.assertEqual(self.controller.store.load()["selected_box"], row["box_id"])
        for field in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status"):
            self.assertEqual(before[field], self.controller.store.load()[field])
        self.assertFalse(Path(before["state_dir"]).exists())
        result = self.controller.handle("/switch --select orchestrator --scope " + response.switcher["scope"])
        self.assertTrue(result.focus_orchestrator)
        self.assertIsNone(self.controller.store.load()["selected_box"])
        self.controller.spawn_fn.assert_not_called()
        self.controller.connections.refresh.assert_not_called()
        self.controller.converse_fn.assert_not_called()

    def test_another_client_plan_change_invalidates_open_picker(self):
        response = self.controller.handle("/switch")
        row = response.switcher["rows"][1]
        other = InteractiveController(self.workspace, state_root=self.root / "state")
        plan = copy.deepcopy(self.plan)
        plan["execution_limitation"] = "changed review"
        other.session = other.store.update(other.session, plan=plan, plan_digest=canonical_digest(plan))
        denied = self.controller.handle("/switch --select {} --scope {}".format(row["key"], response.switcher["scope"]))
        self.assertIn("scope changed", denied.messages[0])
        self.assertIsNone(self.controller.store.load()["selected_box"])

    def test_exact_key_distinguishes_worker_named_orchestrator(self):
        document = copy.deepcopy(self.document)
        worker = copy.deepcopy(document["agents"][0])
        worker["id"] = "orchestrator"
        worker["box"] = ".camol-boxes/orchestrator"
        document["agents"].append(worker)
        plan = dict(self.plan, runbook=document)
        self.controller.session = self.controller.store.update(self.controller.session,
            plan=plan, plan_digest=canonical_digest(plan))
        picker = self.controller.handle("/switch")
        self.assertIsNotNone(picker.switcher)
        result = self.controller.handle("/switch --select box:orchestrator --scope " + picker.switcher["scope"])
        self.assertEqual(result.box_id, "orchestrator")
        self.assertFalse(result.focus_orchestrator)
        result = self.controller.handle("/switch --select orchestrator --scope " + picker.switcher["scope"])
        self.assertTrue(result.focus_orchestrator)

    def test_corrupt_existing_ledger_never_looks_dormant_and_wrong_plan_is_denied(self):
        state = Path(self.controller.session["state_dir"])
        state.mkdir(parents=True)
        database = state / "camol.sqlite3"
        store = SQLiteEventStore(database)
        try:
            changed = copy.deepcopy(self.document)
            changed["run"]["objective"] += " changed"
            Orchestrator(store).initialize(changed)
        finally:
            store.close()
        self.assertIn("differs", self.controller.handle("/switch").messages[0])
        database.write_bytes(b"invalid sqlite")
        response = self.controller.handle("/switch")
        self.assertIn("unreadable", response.messages[0])
        self.assertIsNone(response.switcher)
