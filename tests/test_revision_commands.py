import json
import shlex
import unittest
import time
from pathlib import Path
from unittest.mock import Mock, patch

from camol.app import InteractiveController, SLASH_COMMANDS, validate_envelope
from camol.api import Harness
from camol.revision_ui import RevisionUI
from tests import test_revision_ui
from tests.test_gate_runtime import v5_plan


class RevisionCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_revision_ui.RevisionUITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.prepare()
        self.controller = InteractiveController(self.fixture.source, state_root=self.fixture.root / "interactive")
        self.controller.spawn_fn = Mock(return_value={"started": True, "pid": 123})
        self.owner = patch("camol.app.getpass.getuser", return_value="human-owner")
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.path = self.fixture.root / "next plan.json"
        self.path.write_text(json.dumps(v5_plan("successor")))
        self.command = "/revise --from " + shlex.quote(str(self.path)) + " --reason 'Reviewed amendment'"

    def review(self):
        response = self.controller.handle(self.command)
        self.assertIn("STOPPED-OWNER REVISION", response.messages[0])
        return RevisionUI(self.controller.store).inspect(self.controller.session)

    def test_exact_review_apply_reopen_and_explicit_run_preserve_source(self):
        review = self.review()
        self.assertIn("/revise", {item.command for item in SLASH_COMMANDS})
        self.assertEqual(self.controller.session["run_id"], "original")
        self.assertIn(review["review_digest"], self.controller.handle("/revise").messages[0])
        denied = self.controller.handle("/revise apply sha256:" + "0" * 64)
        self.assertIn("denied", denied.messages[0])
        response = self.controller.handle("/revise apply " + review["review_digest"])
        self.assertTrue(response.focus_orchestrator)
        self.assertEqual(self.controller.session["run_id"], "successor")
        self.assertEqual(self.controller.session["plan"]["schema_version"], 5)
        self.controller.spawn_fn.assert_not_called()
        self.assertIn("LINKED REVISION", self.controller.handle("/plan").messages[0])
        self.assertIn("LINKED REVISION", self.controller.handle("/review").messages[0])
        self.assertIn("denied", self.controller.handle("/approve yes").messages[0])
        reloaded = InteractiveController(self.fixture.source, state_root=self.fixture.root / "interactive")
        self.assertEqual(validate_envelope(reloaded.session["plan"]), reloaded.session["plan"])
        response = self.controller.handle("/run")
        self.assertNotIn("denied", response.messages[0])
        self.controller.spawn_fn.assert_called_once()
        self.assertEqual(self.controller.spawn_fn.call_args.kwargs["expected_source"], review["successor_product_plan"]["source"])

    def test_recovery_after_kernel_commit_adopts_only_reviewed_successor(self):
        review = self.review()
        update = self.controller.store.update
        def fail_handoff(session, **changes):
            if changes.get("run_id") == "successor":
                raise OSError("fixture handoff write failure")
            return update(session, **changes)
        with patch.object(self.controller.store, "update", side_effect=fail_handoff):
            denied = self.controller.handle("/revise apply " + review["review_digest"])
        self.assertIn("denied", denied.messages[0])
        self.assertEqual(self.controller.session["run_id"], "original")
        with Harness(self.fixture.source, self.fixture.state) as harness:
            count = len(harness.store.read("successor"))
            self.assertEqual(harness.orchestrator.state("original")["status"], "superseded")
        response = self.controller.handle("/revise recover")
        self.assertTrue(response.focus_orchestrator)
        self.assertEqual(self.controller.session["run_id"], "successor")
        with Harness(self.fixture.source, self.fixture.state) as harness:
            self.assertEqual(len(harness.store.read("successor")), count)
        self.controller.spawn_fn.assert_not_called()

    def test_apply_does_not_allow_bare_yes_bad_arity_or_implicit_effects(self):
        for command in ("/revise apply yes", "/revise apply", "/revise recover extra", "/revise --from x",
                        self.command + " --reason duplicate", "/revise --reason why --from"):
            with self.subTest(command=command):
                self.assertIn("denied", self.controller.handle(command).messages[0])
        effects = self.fixture.root / "effects.json"
        effects.write_text('{"not":"an array"}')
        self.assertIn("denied", self.controller.handle(self.command + " --effects " + shlex.quote(str(effects))).messages[0])
        self.assertEqual(self.controller.session["run_id"], "original")
        self.controller.spawn_fn.assert_not_called()

    def test_changed_source_after_adoption_denies_new_process_launch(self):
        review = self.review()
        self.controller.handle("/revise apply " + review["review_digest"])
        tracked = next(path for path in self.fixture.source.iterdir() if path.is_file())
        tracked.write_text(tracked.read_text() + "\n# changed after review\n")
        self.assertIn("denied", self.controller.handle("/run").messages[0])
        self.controller.spawn_fn.assert_not_called()

    def test_recovery_without_commit_never_approves(self):
        self.review()
        response = self.controller.handle("/revise recover")
        self.assertIn("denied", response.messages[0])
        self.assertEqual(self.controller.session["run_id"], "original")
        self.controller.spawn_fn.assert_not_called()

    def test_real_detached_successor_build_and_final_human_acceptance(self):
        from camol.supervisor import spawn_supervisor
        from tests.test_product_e2e import ProductFlowTests
        from camol.debug_execution import source_identity
        from camol.store import ReadOnlyEventStore
        from camol.state import project
        baseline = source_identity(self.fixture.source)
        review = self.review()
        self.controller.handle("/revise apply " + review["review_digest"])
        self.controller.spawn_fn = spawn_supervisor
        try:
            response = self.controller.handle("/run")
            self.assertIn("detached", response.messages[-1])
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status = self.controller._control("status")
                if status["run"]["status"] in {"awaiting_acceptance", "completed", "blocked"}:
                    break
                time.sleep(.05)
            self.assertEqual(status["run"]["status"], "awaiting_acceptance", status)
            self.assertEqual(source_identity(self.fixture.source), baseline)
            acceptance = self.controller._control("acceptance")["acceptance"]
            response = self.controller.handle("/accept " + acceptance["outcome_digest"])
            self.assertNotIn("denied", response.messages[0])
            self.assertEqual(self.controller._control("status")["run"]["status"], "completed")
        finally:
            ProductFlowTests.stop_fixture(self, self.fixture.state)
        store = ReadOnlyEventStore(self.fixture.state / "camol.sqlite3")
        try:
            successor = project(store.read("successor"))
            self.assertEqual(successor["status"], "completed")
            self.assertEqual(project(store.read("original"))["status"], "superseded")
        finally:
            store.close()
