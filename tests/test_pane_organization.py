import copy
import json
import os
from pathlib import Path
import unittest

from camol.app import InteractiveController
from camol.pane_organization import PaneOrganizationError, empty, load, save, update, validate
from camol.pane_switcher import filter_rows, row_label, switch_snapshot
from camol.overview import planned_state
from camol.schema import canonical_digest
from tests import test_pane_switcher


class PaneOrganizationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_pane_switcher.PaneSwitcherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.controller = fixture.controller
        self.ids = [agent["id"] for agent in fixture.document["agents"]]
        self.path = self.controller.store.project_dir / "pane-organization.json"
        self.scope = self.controller.handle("/switch").switcher["scope"]

    def test_read_only_views_do_not_create_preferences_or_worker_state(self):
        before = self.controller.store.load()
        for command in ("/pin", "/group", "/switch", "/boxes"):
            self.assertNotIn("denied:", self.controller.handle(command).messages[0])
        self.assertFalse(self.path.exists())
        self.assertFalse(Path(before["state_dir"]).exists())
        for field in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status", "selected_box"):
            self.assertEqual(before[field], self.controller.store.load()[field])
        self.controller.spawn_fn.assert_not_called()
        self.controller.converse_fn.assert_not_called()
        self.controller.connections.refresh.assert_not_called()

    def test_pin_group_restart_and_two_client_updates_preserve_authority(self):
        before = self.controller.store.load()
        target = self.ids[-1]
        for command in ("/pin " + target, "/pin " + target, "/group " + target + ' "API build"'):
            self.assertNotIn("denied:", self.controller.handle(command).messages[0])
        second = InteractiveController(self.fixture.workspace, state_root=self.fixture.root / "state")
        second.handle("/pin " + self.ids[0])
        self.controller.handle("/group " + self.ids[0] + " verifier")
        value = load(self.path, self.scope)
        self.assertEqual(value["pins"], [target, self.ids[0]])
        self.assertEqual(value["groups"], {target: "API build", self.ids[0]: "verifier"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        snapshot = second.handle("/switch API build").switcher
        self.assertEqual([row["box_id"] for row in filter_rows(snapshot["rows"], "API build")], [target])
        self.assertTrue(snapshot["rows"][1]["pinned"])
        self.assertEqual(snapshot["rows"][1]["box_id"], target)
        self.assertEqual(second.box_summaries()[0]["box_id"], target)
        for field in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status", "selected_box"):
            self.assertEqual(before[field], second.store.load()[field])
        self.assertFalse(Path(before["state_dir"]).exists())

    def test_exact_unpin_clear_and_input_denials(self):
        target = self.ids[0]
        for command in ("/pin " + target, "/group " + target + " core", "/pin " + target + " off", "/group " + target + " --clear"):
            self.assertNotIn("denied:", self.controller.handle(command).messages[0])
        self.assertEqual(load(self.path, self.scope), empty(self.scope))
        original = self.path.read_bytes()
        for command in ("/pin 999", "/pin absent", "/pin " + target + " toggle", "/pin " + target + " on extra",
                        "/group " + target, "/group " + target + " --bad", "/group " + target + " " + "x" * 65):
            self.assertIn("denied:", self.controller.handle(command).messages[0], command)
            self.assertEqual(self.path.read_bytes(), original)

    def test_revision_does_not_inherit_old_pins_and_reads_do_not_rewrite(self):
        self.controller.handle("/pin " + self.ids[0])
        original = self.path.read_bytes()
        plan = copy.deepcopy(self.fixture.plan)
        plan["execution_limitation"] = "revised plan"
        self.controller.session = self.controller.store.update(self.controller.session, plan=plan, plan_digest=canonical_digest(plan))
        snapshot = self.controller.handle("/switch").switcher
        self.assertNotEqual(snapshot["scope"], self.scope)
        self.assertFalse(any(row.get("pinned") for row in snapshot["rows"]))
        self.assertEqual(self.path.read_bytes(), original)

    def test_pin_does_not_invalidate_existing_exact_selection(self):
        picker = self.controller.handle("/switch").switcher
        self.controller.handle("/pin " + self.ids[-1])
        response = self.controller.handle("/switch --select box:{} --scope {}".format(self.ids[0], picker["scope"]))
        self.assertEqual(response.box_id, self.ids[0])

    def test_attention_is_not_hidden_by_pins_or_custom_groups(self):
        state = planned_state(self.fixture.document)
        target = self.ids[0]
        task = next(iter(state["tasks"]))
        state["agents"][target]["task_id"] = task
        state["tasks"][task].update(status="waiting")
        preferences = update(empty(self.scope), self.ids[-1], pinned=True, group="[red] api")
        snapshot = switch_snapshot(self.controller.session, state, "plan_only", preferences)
        self.assertEqual(snapshot["rows"][1]["box_id"], target)
        self.assertEqual(snapshot["rows"][1]["group"], "attention")
        self.assertIn("[red] api", row_label(snapshot["rows"][2]))
        self.assertIn("★", row_label(snapshot["rows"][2]))

    def test_corrupt_duplicate_linked_or_special_preferences_are_denied(self):
        for raw in (b'{"schema":1,"schema":2}', b'{}', b'x' * 65537):
            self.path.write_bytes(raw)
            self.path.chmod(0o600)
            with self.assertRaises(PaneOrganizationError):
                load(self.path, self.scope)
            self.assertIn("denied:", self.controller.handle("/switch").messages[0])
            self.assertTrue(all(box.get("organization_error") for box in self.controller.box_summaries()))
            self.assertIn("WARNING", self.controller.handle("/boxes").messages[0])
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.unlink()
        outside = self.fixture.root / "outside"
        outside.write_text(json.dumps(empty(self.scope)))
        self.path.symlink_to(outside)
        with self.assertRaises(PaneOrganizationError):
            save(self.path, empty(self.scope))
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(PaneOrganizationError):
            load(self.path, self.scope)

    def test_validation_bounds_and_no_lost_updates_under_project_lock(self):
        for changes in ({"pins": [self.ids[0]] * 2}, {"groups": {self.ids[0]: "bad\x1bgroup"}},
                        {"schema_version": True}, {"unknown": 1}, {"pins": ["box-" + str(i) for i in range(1001)]}):
            with self.assertRaises(ValueError):
                validate(dict(empty(self.scope), **changes))
        with self.controller.store.transaction():
            self.assertIn("another client", self.controller.handle("/pin " + self.ids[0]).messages[0])
        self.assertFalse(self.path.exists())
