import copy
import json
import unittest
from pathlib import Path

from camol.runbook import RunbookError, validate_runbook


ROOT = Path(__file__).resolve().parents[1]


class RunbookTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8"))

    def test_three_agent_example_is_valid(self):
        runbook = validate_runbook(self.raw)
        self.assertEqual([agent["id"] for agent in runbook["agents"]], [
            "strategist",
            "builder",
            "verifier",
        ])

    def test_v1_refuses_more_or_fewer_than_three_boxes(self):
        invalid = copy.deepcopy(self.raw)
        invalid["agents"].pop()
        with self.assertRaisesRegex(RunbookError, "exactly three"):
            validate_runbook(invalid)

    def test_dependencies_must_point_backward_to_keep_the_plan_acyclic(self):
        invalid = copy.deepcopy(self.raw)
        invalid["tasks"][0]["depends_on"] = ["integrate"]
        with self.assertRaisesRegex(RunbookError, "declared earlier"):
            validate_runbook(invalid)


if __name__ == "__main__":
    unittest.main()
