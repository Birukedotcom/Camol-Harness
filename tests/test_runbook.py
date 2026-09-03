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

    def test_accepts_an_arbitrary_worker_pool(self):
        expanded = copy.deepcopy(self.raw)
        expanded["run"]["max_concurrency"] = 4
        fourth = copy.deepcopy(expanded["agents"][1])
        fourth.update(id="builder-2", box=".camol/boxes/builder-2")
        expanded["agents"].append(fourth)

        runbook = validate_runbook(expanded)

        self.assertEqual(len(runbook["agents"]), 4)
        self.assertEqual(runbook["run"]["max_concurrency"], 4)

    def test_concurrency_cannot_exceed_registered_workers(self):
        invalid = copy.deepcopy(self.raw)
        invalid["run"]["max_concurrency"] = 4
        with self.assertRaisesRegex(RunbookError, "cannot exceed registered workers"):
            validate_runbook(invalid)

    def test_conflicting_legacy_concurrency_is_rejected(self):
        invalid = copy.deepcopy(self.raw)
        invalid["run"]["max_agents"] = 2
        with self.assertRaisesRegex(RunbookError, "conflicts with legacy"):
            validate_runbook(invalid)

    def test_legacy_max_agents_is_preserved_for_plan_digest_compatibility(self):
        legacy = copy.deepcopy(self.raw)
        legacy["run"]["max_agents"] = legacy["run"].pop("max_concurrency")

        runbook = validate_runbook(legacy)

        self.assertEqual(runbook["run"]["max_agents"], 3)
        self.assertNotIn("max_concurrency", runbook["run"])

    def test_worker_pool_cannot_be_empty(self):
        invalid = copy.deepcopy(self.raw)
        invalid["agents"] = []
        with self.assertRaisesRegex(RunbookError, "at least one"):
            validate_runbook(invalid)

    def test_concurrency_must_be_positive(self):
        invalid = copy.deepcopy(self.raw)
        invalid["run"]["max_concurrency"] = 0
        with self.assertRaisesRegex(RunbookError, "positive integer"):
            validate_runbook(invalid)

    def test_dependencies_must_point_backward_to_keep_the_plan_acyclic(self):
        invalid = copy.deepcopy(self.raw)
        invalid["tasks"][0]["depends_on"] = ["integrate"]
        with self.assertRaisesRegex(RunbookError, "declared earlier"):
            validate_runbook(invalid)


if __name__ == "__main__":
    unittest.main()
