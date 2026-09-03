import tempfile
import unittest
from pathlib import Path

from camol.app import InteractiveController


class InteractiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.state_root = root / "state"
        self.controller = InteractiveController(self.workspace, state_root=self.state_root)

    def tearDown(self):
        self.temporary.cleanup()

    def complete_grill(self, model="manual"):
        if model != "manual":
            self.controller.handle("/model " + model)
        self.controller.handle("/grill Build it safely")
        for answer in (
            "A feature and passing tests",
            "Do not deploy or expose credentials",
            "No secrets in output",
            "core | implement core\ntests | add tests | after=core",
            "python3 -m unittest discover -v",
            "boxes=2 turns=4 tokens=12000 no deployment",
        ):
            response = self.controller.handle(answer)
        return response

    def test_grill_renders_n_box_plan_and_requires_second_step_approval(self):
        response = self.complete_grill("claude:fable")
        self.assertIn("Nothing has started", response.messages[0])
        self.assertIn("tests <- [core]", response.messages[1])
        self.assertIn("canonical product plan JSON:", response.messages[1])
        self.assertIn('"schema": "camol.product_plan"', response.messages[1])
        digest = self.controller.session["plan_digest"]
        self.assertIsNone(self.controller.session["approved_digest"])
        confirmation = self.controller.handle("/approve")
        self.assertIn(digest, confirmation.messages[0])
        self.assertIsNone(self.controller.session["approved_digest"])
        approved = self.controller.handle("/approve yes")
        self.assertIn("Approved", approved.messages[0])
        self.assertEqual(self.controller.session["approved_digest"], digest)

    def test_setting_change_invalidates_frozen_candidate(self):
        self.complete_grill("claude:fable")
        self.controller.handle("/approve yes")
        self.controller.handle("/effort low")
        self.assertIsNone(self.controller.session["plan"])
        self.assertIsNone(self.controller.session["approved_digest"])

    def test_no_run_before_approval_or_without_executable_provider(self):
        self.complete_grill()
        denied = self.controller.handle("/run --accept-spend")
        self.assertIn("requires the exact current plan", denied.messages[0])
        self.controller.handle("/approve yes")
        denied = self.controller.handle("/run --accept-spend")
        self.assertIn("planning-only", denied.messages[0])

    def test_quit_detaches_without_stop(self):
        response = self.controller.handle("/quit")
        self.assertTrue(response.exit_client)
        self.assertIn("not stopped", response.messages[0])

    def test_box_pool_is_derived_not_fixed_at_three(self):
        self.complete_grill("claude:fable")
        boxes = self.controller.box_summaries()
        self.assertEqual(len(boxes), 2)
        self.assertEqual([item["box_id"] for item in boxes], ["builder", "builder-2"])


if __name__ == "__main__":
    unittest.main()
