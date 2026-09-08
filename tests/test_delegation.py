import copy
import json
from pathlib import Path
import unittest

from camol.app import SLASH_COMMANDS
from camol.delegation import delegation_snapshot, render_delegation
from camol.overview import planned_state
from camol.schema import canonical_digest
from tests import test_pane_switcher, test_revision_commands


class DelegationViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_pane_switcher.PaneSwitcherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.state = planned_state(self.fixture.document)

    def test_static_matches_are_not_readiness_and_views_are_detached(self):
        task_id = next(iter(self.state["tasks"]))
        self.state["tasks"][task_id]["capabilities"] = ["build"]
        agents = list(self.state["agents"].values())
        agents[0].update(capabilities=["build"], status="busy", task_id="another-task")
        agents[1].update(capabilities=["inspect"])
        before = copy.deepcopy(self.state)
        report = delegation_snapshot(self.state, task_id=task_id, basis="plan_only")
        self.assertEqual(self.state, before)
        self.assertFalse(report["readiness_proven"])
        self.assertFalse(report["allocation_changed"])
        self.assertEqual(report["snapshot_digest"], canonical_digest({k: v for k, v in report.items() if k != "snapshot_digest"}))
        matches = [row for row in report["rows"] if row["capability_match"]]
        self.assertTrue(any(row["current_task"] == "another-task" for row in matches))
        self.assertTrue(any(row["missing_capabilities"] == ["build"] for row in report["rows"]))
        report["task"]["required_capabilities"].append("tampered")
        self.assertEqual(self.state, before)
        self.assertIn("NOT readiness", render_delegation(report))

    def test_many_boxes_pages_and_unmet_dependencies_do_not_imply_dispatch(self):
        prototype = next(iter(self.state["agents"].values()))
        task_id = next(iter(self.state["tasks"]))
        prototype["capabilities"] = list(self.state["tasks"][task_id]["capabilities"])
        self.state["agents"] = {"box-{:03d}".format(n): copy.deepcopy(prototype) for n in range(77)}
        report = delegation_snapshot(self.state, task_id=task_id, offset=70, limit=10)
        self.assertEqual(len(report["rows"]), 7)
        self.assertEqual(report["declared_box_count"], 77)
        self.assertFalse(report["page"]["more"])
        overview = delegation_snapshot(self.state)
        row = next(row for row in overview["rows"] if row["task_id"] == task_id)
        self.assertEqual(row["capability_matches"], 77)
        self.assertIsNone(row["assigned_box"])
        self.assertTrue(any(row["unmet_dependencies"] for row in overview["rows"]))
        for options in ({"limit": True}, {"limit": 201}, {"offset": -1}, {"task_id": []}, {"task_id": "0"}):
            with self.assertRaises(ValueError):
                delegation_snapshot(self.state, **options)

    def test_public_command_never_initializes_run_probes_spends_or_approves(self):
        before = self.controller.store.load()
        response = self.controller.handle("/delegate --json --limit 2")
        report = json.loads(response.messages[0])
        self.assertEqual(report["basis"], "plan_only")
        self.assertEqual(len(report["rows"]), 2)
        self.assertTrue(response.focus_orchestrator)
        task_id = report["rows"][0]["task_id"]
        detail = json.loads(self.controller.handle("/delegate " + task_id + " --json").messages[0])
        self.assertEqual(detail["task_id"], task_id)
        self.assertIn("/delegate", {item.command for item in SLASH_COMMANDS})
        for field in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status", "selected_box"):
            self.assertEqual(before[field], self.controller.store.load()[field])
        self.assertFalse(Path(before["state_dir"]).exists())
        for mock in (self.controller.spawn_fn, self.controller.connections.refresh, self.controller.converse_fn):
            mock.assert_not_called()

    def test_invalid_commands_and_corrupt_ledger_cannot_fall_back_or_act(self):
        for command in ("/delegate --limit 0", "/delegate --offset -1", "/delegate --json --json",
                        "/delegate --limit 1 --limit 2", "/delegate apply yes", "/delegate --unknown", "/delegate --from x"):
            self.assertIn("denied", self.controller.handle(command).messages[0])
        directory = Path(self.controller.session["state_dir"])
        directory.mkdir(parents=True)
        (directory / "camol.sqlite3").write_bytes(b"corrupted")
        result = self.controller.handle("/delegate")
        self.assertIn("denied", result.messages[0])
        self.assertNotIn("plan_only", result.messages[0])

    def test_content_is_excluded_and_terminal_controls_are_literal(self):
        self.state["evidence"] = {"raw": "private worker prose"}
        report = delegation_snapshot(self.state)
        self.assertNotIn("private worker prose", json.dumps(report))
        report["rows"][0]["task_id"] = "[red]unsafe\x1b]52;clipboard\x07"
        rendered = render_delegation(report)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertIn("[red]", rendered)


class DelegationRevisionTests(unittest.TestCase):
    def test_real_delegated_successor_build_still_needs_final_human_acceptance(self):
        fixture = test_revision_commands.RevisionCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.command = fixture.command.replace("/revise", "/delegate", 1)
        fixture.test_real_detached_successor_build_and_final_human_acceptance()

    def test_review_uses_existing_exact_approval_path_without_auto_handoff(self):
        fixture = test_revision_commands.RevisionCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from camol.revision_ui import RevisionUI
        response = fixture.controller.handle(fixture.command.replace("/revise", "/delegate", 1))
        self.assertIn("STOPPED-OWNER REVISION", response.messages[0])
        review = RevisionUI(fixture.controller.store).inspect(fixture.controller.session)
        self.assertEqual(fixture.controller.session["run_id"], "original")
        self.assertIn("/revise apply " + review["review_digest"], response.messages[0])
        self.assertIn("denied", fixture.controller.handle("/delegate apply " + review["review_digest"]).messages[0])
        self.assertIn("denied", fixture.controller.handle("/revise apply sha256:" + "0" * 64).messages[0])
        adopted = fixture.controller.handle("/revise apply " + review["review_digest"])
        self.assertNotIn("denied", adopted.messages[0])
        self.assertEqual(fixture.controller.session["run_id"], "successor")
        fixture.controller.spawn_fn.assert_not_called()

    def test_delegation_review_refuses_live_owner(self):
        fixture = test_revision_commands.RevisionCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from camol.api import Harness
        with Harness(fixture.fixture.source, fixture.fixture.state):
            response = fixture.controller.handle(fixture.command.replace("/revise", "/delegate", 1))
        self.assertIn("denied", response.messages[0])
        self.assertEqual(fixture.controller.session["run_id"], "original")
        fixture.controller.spawn_fn.assert_not_called()


class DelegationTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_composer_can_inspect_delegation_without_losing_following_draft(self):
        from camol.tui import CamolApp, PromptArea
        from tests.test_tui import TuiTests
        fixture = test_pane_switcher.PaneSwitcherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/delegate")
            await pilot.press("enter")
            def visible():
                return "DELEGATION VIEW" in "\n".join(line.text for line in app.query_one("#transcript").lines)
            await TuiTests.wait_for_ui(self, pilot, visible, "delegation overview")
            prompt.load_text("unsent next step")
            await app._refresh_fleet()
            self.assertEqual(prompt.text, "unsent next step")
            self.assertFalse(app.in_box)
            fixture.controller.spawn_fn.assert_not_called()
            fixture.controller.converse_fn.assert_not_called()
