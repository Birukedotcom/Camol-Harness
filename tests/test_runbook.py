import copy
import hashlib
import json
import unittest
from pathlib import Path

from camol.runbook import (
    LATEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    RunbookError,
    migrate_runbook_v1_to_v2,
    runbook_digest,
    validate_runbook,
)


ROOT = Path(__file__).resolve().parents[1]

# Golden digests captured from the pre-M0 kernel (commit 93a2319). If either
# changes, an existing frozen plan would silently stop resuming. Do not update
# them without a documented migration.
GOLDEN_EXAMPLE_DIGEST_V1 = "sha256:2850aa84a60f5177cd17f38ed1d6c3de9c921d2d2bdf4004c8ac29f77e3ee2ff"
GOLDEN_LEGACY_MAX_AGENTS_DIGEST_V1 = "sha256:c49c78b044439a6a9be9d12e9973783919e10e2089ec0fe7e349d8d1a0322c88"


def _pre_m0_digest(runbook):
    """The exact formula the kernel used before M0 introduced canonical_digest."""
    canonical = json.dumps(runbook, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


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


class RunbookV1CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8"))

    def test_example_v1_digest_is_pinned(self):
        normalized = validate_runbook(self.raw)
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(runbook_digest(normalized), GOLDEN_EXAMPLE_DIGEST_V1)
        self.assertEqual(runbook_digest(normalized), _pre_m0_digest(normalized))

    def test_legacy_max_agents_digest_is_pinned(self):
        legacy = copy.deepcopy(self.raw)
        legacy["run"]["max_agents"] = legacy["run"].pop("max_concurrency")
        normalized = validate_runbook(legacy)
        self.assertEqual(runbook_digest(normalized), GOLDEN_LEGACY_MAX_AGENTS_DIGEST_V1)
        self.assertEqual(runbook_digest(normalized), _pre_m0_digest(normalized))

    def test_v1_normalization_is_idempotent(self):
        once = validate_runbook(self.raw)
        twice = validate_runbook(copy.deepcopy(once))
        self.assertEqual(once, twice)
        self.assertEqual(runbook_digest(once), runbook_digest(twice))

    def test_v1_still_ignores_unknown_fields_and_carries_no_v2_fields(self):
        tolerant = copy.deepcopy(self.raw)
        tolerant["notes"] = "ignored by v1"
        normalized = validate_runbook(tolerant)
        self.assertNotIn("notes", normalized)
        self.assertNotIn("readiness_policy", normalized["run"])
        self.assertTrue(all("trust_tier" not in agent for agent in normalized["agents"]))
        self.assertEqual(runbook_digest(normalized), GOLDEN_EXAMPLE_DIGEST_V1)

    def test_v2_fields_are_not_silently_reinterpreted_inside_v1(self):
        leaked = copy.deepcopy(self.raw)
        leaked["run"]["readiness_policy"] = {"receipt_ttl_seconds": 60}
        with self.assertRaisesRegex(RunbookError, "schema v2 field"):
            validate_runbook(leaked)
        leaked = copy.deepcopy(self.raw)
        leaked["agents"][0]["trust_tier"] = "sandboxed"
        with self.assertRaisesRegex(RunbookError, "schema v2 field"):
            validate_runbook(leaked)

    def test_unknown_schema_versions_are_rejected_clearly(self):
        self.assertEqual(SUPPORTED_SCHEMA_VERSIONS, (1, 2))
        self.assertEqual(LATEST_SCHEMA_VERSION, 2)
        for version in (0, 3, "1", None, True, 1.0):
            invalid = copy.deepcopy(self.raw)
            invalid["schema_version"] = version
            with self.assertRaisesRegex(RunbookError, "unsupported schema_version"):
                validate_runbook(invalid)


class RunbookV2Tests(unittest.TestCase):
    def setUp(self):
        raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8"))
        self.v1 = raw
        self.policy = {"receipt_ttl_seconds": 300}
        self.tiers = {agent["id"]: "developer_trusted" for agent in raw["agents"]}

    def _v2(self):
        return migrate_runbook_v1_to_v2(self.v1, readiness_policy=self.policy, trust_tiers=self.tiers)

    def test_migration_is_explicit_deterministic_and_non_mutating(self):
        snapshot = copy.deepcopy(self.v1)
        first = self._v2()
        second = self._v2()
        self.assertEqual(self.v1, snapshot)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["run"]["max_concurrency"], 3)
        self.assertNotIn("max_agents", first["run"])
        self.assertEqual(first["run"]["readiness_policy"], self.policy)
        self.assertTrue(all(agent["trust_tier"] == "developer_trusted" for agent in first["agents"]))
        self.assertEqual(validate_runbook(copy.deepcopy(first)), first)

    def test_migration_maps_legacy_max_agents_to_max_concurrency(self):
        legacy = copy.deepcopy(self.v1)
        legacy["run"]["max_agents"] = legacy["run"].pop("max_concurrency")
        migrated = migrate_runbook_v1_to_v2(legacy, readiness_policy=self.policy, trust_tiers=self.tiers)
        self.assertEqual(migrated["run"]["max_concurrency"], 3)
        self.assertNotIn("max_agents", migrated["run"])

    def test_v2_digest_differs_from_v1_but_v1_digest_is_unchanged(self):
        v1_digest = runbook_digest(validate_runbook(self.v1))
        v2_digest = runbook_digest(self._v2())
        self.assertEqual(v1_digest, GOLDEN_EXAMPLE_DIGEST_V1)
        self.assertNotEqual(v1_digest, v2_digest)
        self.assertEqual(v2_digest, runbook_digest(self._v2()))

    def test_migration_requires_every_new_field_explicitly(self):
        with self.assertRaisesRegex(RunbookError, "missing agents: verifier"):
            migrate_runbook_v1_to_v2(
                self.v1, readiness_policy=self.policy, trust_tiers={"strategist": "sandboxed", "builder": "sandboxed"}
            )
        with self.assertRaisesRegex(RunbookError, "unknown agents: ghost"):
            migrate_runbook_v1_to_v2(self.v1, readiness_policy=self.policy, trust_tiers=dict(self.tiers, ghost="sandboxed"))
        with self.assertRaisesRegex(RunbookError, "receipt_ttl_seconds must be a positive integer"):
            migrate_runbook_v1_to_v2(self.v1, readiness_policy={}, trust_tiers=self.tiers)
        with self.assertRaisesRegex(RunbookError, "trust_tier must be one of"):
            migrate_runbook_v1_to_v2(self.v1, readiness_policy=self.policy, trust_tiers=dict(self.tiers, builder="root"))
        with self.assertRaisesRegex(RunbookError, "requires a schema_version 1"):
            migrate_runbook_v1_to_v2(self._v2(), readiness_policy=self.policy, trust_tiers=self.tiers)

    def test_v2_rejects_legacy_alias_and_unknown_fields(self):
        v2 = self._v2()
        aliased = copy.deepcopy(v2)
        aliased["run"]["max_agents"] = aliased["run"].pop("max_concurrency")
        with self.assertRaisesRegex(RunbookError, "schema v1 alias"):
            validate_runbook(aliased)
        for mutate, message in [
            (lambda r: r.__setitem__("notes", "x"), "runbook has unknown fields: notes"),
            (lambda r: r["run"].__setitem__("budget", 1), "run has unknown fields: budget"),
            (lambda r: r["run"]["readiness_policy"].__setitem__("mode", "x"), "readiness_policy has unknown fields: mode"),
            (lambda r: r["agents"][0].__setitem__("model", "x"), r"agents\[0\] has unknown fields: model"),
            (lambda r: r["agents"][0]["adapter"].__setitem__("env", {}), r"adapter has unknown fields: env"),
            (lambda r: r["tasks"][0].__setitem__("owner", "x"), r"tasks\[0\] has unknown fields: owner"),
            (lambda r: r["tasks"][0]["steps"][0].__setitem__("hint", "x"), "step 0 has unknown fields: hint"),
            (lambda r: r["tasks"][0]["verification"][0].__setitem__("shell", "x"), "verification 0 has unknown fields: shell"),
        ]:
            broken = copy.deepcopy(v2)
            mutate(broken)
            with self.assertRaisesRegex(RunbookError, message):
                validate_runbook(broken)

    def test_v2_requires_readiness_policy_and_trust_tier(self):
        v2 = self._v2()
        missing_policy = copy.deepcopy(v2)
        del missing_policy["run"]["readiness_policy"]
        with self.assertRaisesRegex(RunbookError, "readiness_policy is required"):
            validate_runbook(missing_policy)
        missing_tier = copy.deepcopy(v2)
        del missing_tier["agents"][1]["trust_tier"]
        with self.assertRaisesRegex(RunbookError, r"agents\[1\].trust_tier must be one of"):
            validate_runbook(missing_tier)

    def test_readiness_cannot_be_disabled_by_any_plan_field(self):
        for flag in (False, True):
            bypass = self._v2()
            bypass["run"]["readiness_policy"]["require_readiness_receipt"] = flag
            with self.assertRaisesRegex(RunbookError, "readiness proof cannot be disabled by a plan"):
                validate_runbook(bypass)
            with self.assertRaisesRegex(RunbookError, "readiness proof cannot be disabled by a plan"):
                migrate_runbook_v1_to_v2(
                    self.v1,
                    readiness_policy={"receipt_ttl_seconds": 300, "require_readiness_receipt": flag},
                    trust_tiers=self.tiers,
                )
        self.assertEqual(set(self._v2()["run"]["readiness_policy"]), {"receipt_ttl_seconds"})

    def test_v2_integer_fields_reject_booleans(self):
        v2 = self._v2()
        cases = [
            (lambda r, v: r["run"].__setitem__("max_concurrency", v), "max_concurrency must be a positive integer"),
            (lambda r, v: r["run"]["readiness_policy"].__setitem__("receipt_ttl_seconds", v), "receipt_ttl_seconds must be a positive integer"),
            (lambda r, v: r["run"]["token_policy"].__setitem__("max_tokens_per_turn", v), "max_tokens_per_turn must be a positive integer"),
            (lambda r, v: r["run"]["token_policy"].__setitem__("max_total_tokens", v), "max_total_tokens must be a positive integer"),
            (lambda r, v: r["run"]["token_policy"].__setitem__("max_turns_per_task", v), "max_turns_per_task must be a positive integer"),
            (lambda r, v: r["run"]["token_policy"].__setitem__("checkpoint_reserve", v), "checkpoint_reserve must be a non-negative integer"),
            (lambda r, v: r["agents"][0]["adapter"].__setitem__("timeout_seconds", v), r"timeout_seconds must be a positive integer"),
            (lambda r, v: r["tasks"][0].__setitem__("max_attempts", v), "max_attempts must be a positive integer"),
        ]
        for mutate, message in cases:
            for flag in (True, False):
                broken = copy.deepcopy(v2)
                mutate(broken, flag)
                with self.assertRaisesRegex(RunbookError, message, msg="{} <- {}".format(message, flag)):
                    validate_runbook(broken)

    def test_v1_integer_tolerance_is_unchanged(self):
        # Compatibility pin: v1 keeps its pre-M0 acceptance of `True` where an
        # int was expected, so already-frozen v1 digests cannot shift. v2 is strict.
        tolerant = copy.deepcopy(self.v1)
        tolerant["tasks"][0]["max_attempts"] = True
        self.assertEqual(validate_runbook(tolerant)["tasks"][0]["max_attempts"], True)


if __name__ == "__main__":
    unittest.main()
