import unittest

from camol.planning import GrillState, PlanningError, compile_runbook, proposal_from_grill
from camol.schema import canonical_digest


class PlanningTests(unittest.TestCase):
    def complete_grill(self):
        grill = GrillState.start("Build a safe feature")
        for answer in (
            "A checked-in feature with passing tests",
            "Do not deploy; do not touch credentials",
            "No secrets in logs; preserve existing behavior",
            "python3 -m unittest discover -v",
            "one local box; six turns; no paid preflight",
        ):
            grill = grill.answer(answer)
        return grill

    def test_grill_is_ordered_and_compiles_to_schema_v4(self):
        grill = self.complete_grill()
        proposal = proposal_from_grill(grill)
        runbook = compile_runbook(
            proposal,
            run_id="feature-123",
            adapter={"kind": "process", "argv": ["python3", "agent.py"]},
        )

        self.assertEqual(runbook["schema_version"], 4)
        self.assertEqual(runbook["run"]["id"], "feature-123")
        self.assertEqual(
            runbook["tasks"][0]["verification"][0]["argv"],
            ["python3", "-m", "unittest", "discover", "-v"],
        )
        self.assertEqual(canonical_digest(proposal), canonical_digest(proposal_from_grill(grill)))

    def test_shell_verification_pipeline_is_rejected(self):
        grill = GrillState.start("test")
        for answer in ("done", "nothing", "safe", "pytest && deploy", "one box"):
            grill = grill.answer(answer)
        with self.assertRaisesRegex(PlanningError, "argv"):
            proposal_from_grill(grill)

    def test_incomplete_grill_cannot_produce_plan(self):
        with self.assertRaisesRegex(PlanningError, "every"):
            proposal_from_grill(GrillState.start("test"))


if __name__ == "__main__":
    unittest.main()
