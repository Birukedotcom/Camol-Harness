import unittest

from camol.planning import (
    GrillState,
    PlanningError,
    compile_runbook,
    effective_resource_limits,
    proposal_from_grill,
    reject_sensitive_text,
    validate_proposal,
)
from camol.schema import canonical_digest


class PlanningTests(unittest.TestCase):
    def complete_grill(self):
        grill = GrillState.start("Build a safe feature")
        for answer in (
            "A checked-in feature with passing tests",
            "Do not deploy; do not touch credentials",
            "No secrets in logs; preserve existing behavior",
            "core | implement core\ntests | add tests | after=core",
            "python3 -m unittest discover -v",
            "boxes=2; turns=6; tokens=48000; cost_cents=100; turn_timeout_seconds=600",
        ):
            grill = grill.answer(answer)
        return grill

    def test_grill_is_ordered_and_compiles_to_schema_v4(self):
        grill = self.complete_grill()
        proposal = proposal_from_grill(grill)
        self.assertEqual(proposal["schema_version"], 2)
        runbook = compile_runbook(
            proposal,
            run_id="feature-123",
            adapter={"kind": "process", "argv": ["python3", "agent.py"]},
        )

        self.assertEqual(runbook["schema_version"], 4)
        self.assertEqual(runbook["run"]["id"], "feature-123")
        self.assertEqual(len(runbook["agents"]), 2)
        self.assertEqual(runbook["tasks"][1]["depends_on"], ["core"])
        self.assertEqual(runbook["tasks"][-1]["depends_on"], ["core", "tests"])
        self.assertEqual(
            runbook["tasks"][0]["verification"][0]["argv"],
            ["git", "diff", "--check", "HEAD"],
        )
        self.assertEqual(
            runbook["tasks"][-1]["verification"][0]["argv"],
            ["python3", "-m", "unittest", "discover", "-v"],
        )
        self.assertEqual(runbook["tasks"][-1]["verification"][0]["cwd"], "workspace_root")
        self.assertTrue(any(rule["id"] == "human-exclusion-1" for rule in runbook["rules"]))
        exclusions = [rule["text"] for rule in runbook["rules"] if rule["id"].startswith("human-exclusion-")]
        self.assertEqual(exclusions, [
            "Excluded from worker scope: Do not deploy",
            "Excluded from worker scope: do not touch credentials",
        ])
        self.assertEqual(canonical_digest(proposal), canonical_digest(proposal_from_grill(grill)))

    def test_legacy_v1_proposal_remains_readable_with_original_shape(self):
        proposal = proposal_from_grill(self.complete_grill())
        proposal["schema_version"] = 1
        proposal["resource_limits"] = {
            "max_concurrency": 2,
            "max_turns_per_task": 6,
            "max_total_tokens": 48_000,
        }
        # V1 permitted a forward reference. Validation must preserve that
        # persisted contract even though new V2 proposals reject it.
        proposal["tasks"] = list(reversed(proposal["tasks"]))
        normalized = validate_proposal(proposal)
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(effective_resource_limits(normalized), {
            "box_pool_size": 2,
            "max_concurrency": 2,
            "max_turns_per_task": 6,
            "max_total_tokens": 48_000,
            "max_worker_cost_usd_cents": 100,
            "turn_timeout_seconds": 1_800,
        })

        six_field_v1 = proposal_from_grill(self.complete_grill())
        six_field_v1["schema_version"] = 1
        normalized = validate_proposal(six_field_v1)
        self.assertEqual(
            effective_resource_limits(normalized),
            six_field_v1["resource_limits"],
        )

        invalid_version = dict(six_field_v1, schema_version=[])
        with self.assertRaisesRegex(PlanningError, "schema is unsupported"):
            validate_proposal(invalid_version)

    def test_shell_verification_pipeline_is_rejected(self):
        grill = GrillState.start("test")
        for answer in ("done", "nothing", "safe", "task | do it"):
            grill = grill.answer(answer)
        with self.assertRaisesRegex(PlanningError, "argv"):
            grill.answer("pytest && deploy")
        self.assertEqual(grill.question_index, 4)
        self.assertEqual(grill.answer("pytest").question_index, 5)

    def test_incomplete_grill_cannot_produce_plan(self):
        with self.assertRaisesRegex(PlanningError, "every"):
            proposal_from_grill(GrillState.start("test"))

    def test_resource_prose_and_forward_dependencies_are_rejected(self):
        grill = GrillState.start("test")
        answers = (
            "Return HTTP 200", "no deploy", "Preserve Python 3.9",
            "tests | test it | after=core\ncore | build it",
            "python3 -m unittest", "boxes=2 tokens=1000 local only",
        )
        for answer in answers[:3]:
            grill = grill.answer(answer)
        with self.assertRaisesRegex(PlanningError, "declared earlier"):
            grill.answer(answers[3])

        grill = GrillState.start("test")
        for answer in (
            "Return HTTP 200", "no deploy", "Preserve Python 3.9",
            "core | build it", "python3 -m unittest",
        ):
            grill = grill.answer(answer)
        with self.assertRaisesRegex(PlanningError, "only boxes"):
            grill.answer("boxes=1 local only")

    def test_numeric_suffixes_and_total_turn_cap_are_preserved(self):
        grill = GrillState.start("test")
        for answer in (
            "Return HTTP 200", "no deploy", "Preserve Python 3.9",
            "core | build it", "python3 -m unittest", "boxes=1 tokens=1000",
        ):
            grill = grill.answer(answer)
        proposal = proposal_from_grill(grill)
        runbook = compile_runbook(
            proposal, run_id="numbers", adapter={"kind": "process", "argv": ["python3", "agent.py"]}
        )
        self.assertEqual(proposal["outcomes"], ["Return HTTP 200"])
        self.assertIn("Preserve Python 3.9", proposal["invariants"])
        self.assertEqual(runbook["run"]["token_policy"]["max_tokens_per_turn"], 1000)

    def test_secret_detection_allows_ordinary_security_engineering_language(self):
        for text in (
            "Implement token validation",
            "Preserve basic authentication",
            "preserve token max_tokens_per_turn",
            "use basic authentication-required",
            "token MaximumTokensPerRequest",
            "Change cookie: behavior",
            "Rotate credentials through an opaque reference",
            "AUTH_ENABLED=true",
            "GIT_AUTHOR_NAME=test",
            "SESSION_TIMEOUT=300",
            "TOKEN_VALIDATION=strict",
            "API_KEY=environment",
            "AUTH_TOKEN=required",
            "PASSWORD=redacted",
            "max_tokens=12000",
            "tokens=12000",
        ):
            self.assertEqual(reject_sensitive_text(text), text)
        for text in (
            "use sk-live-abcdefghijklmnopqrstuvwxyz123456",
            "api_key=abcdefghijklmnopqrstuvwxyz",
            "Authorization: Bearer abcdefghijklmnop",
            "Bearer abcdefghijklmnop",
            "Cookie: session=abcdefgh",
            "--password hunter2-secret",
            "MY_TOKEN=abcdefghijk",
            "MY_TOKEN: abcdefghijk",
            "PAT=abcdefghijk",
            "clientSecret=abcdefghijklmnopqrstuvwxyz",
            "accessToken=abcdefghijklmnopqrstuvwxyz",
            "SESSION=abcdefghijklmnopqrstuvwxyz",
            "PASSWORD=123456",
            "AUTH_TOKEN=123456789",
            "Bearer abcdefghijklmnop-qrstuvwxyz",
            "Bearer deadbeefdeadbeef",
        ):
            with self.assertRaisesRegex(PlanningError, "credential material"):
                reject_sensitive_text(text)


if __name__ == "__main__":
    unittest.main()
