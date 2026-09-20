import unittest
from dataclasses import replace

from camol.evidence import EvidenceRecord
from camol.gates import (GateApproval, GateAssessment, GateBinding, GateError, GateObservation,
                         GatePolicy, Invariant, Obligation, Waiver, assess_gate, inherit_invariants)


DIGEST = "sha256:" + "1" * 64
OTHER = "sha256:" + "2" * 64
NOW = "2026-09-07T12:00:00+00:00"
BINDING = GateBinding("run-1", DIGEST, DIGEST, DIGEST, DIGEST)
INVARIANT = Invariant("safe", 1, "owner", "global", "never", "payment credentials never enter logs",
                      ("payment accepted",), ("redacted payment transcript",), "high",
                      ("seed credential marker",), ("test_result",), True, "preauthorized")
OBLIGATION = Obligation("safe-exit", "owner", "ACCEPTED", ("safe",), ("task-1",))


def observed(identifier="evidence-1", family="deterministic", verdict="SUPPORTED_BY_REQUIRED_EVIDENCE", **kwargs):
    evidence = EvidenceRecord(evidence_id=identifier, run_id="run-1", task_id="task-1", debug_case_id=None,
                              agent_id="builder", lease_id="lease-1", fence_digest=DIGEST, kind="test_result",
                              epistemic_status="EXECUTED", producer="verifier", observed_at=NOW,
                              data={"passed": verdict == "SUPPORTED_BY_REQUIRED_EVIDENCE"})
    values = dict(observation_id="observation-" + identifier, binding=BINDING, invariant_id="safe",
                  evaluator_family=family, verifier_id="reviewer", verdict=verdict, evidence=evidence)
    values.update(kwargs)
    return GateObservation(**values)


def approval(policy, **kwargs):
    value = GateApproval("approval-1", BINDING, policy.digest(), "owner", "2026-09-07T11:00:00+00:00", "2026-09-07T13:00:00+00:00")
    return replace(value, **kwargs)


def assess(observations, policy=None, **kwargs):
    values = dict(binding=BINDING, policy=policy or GatePolicy.compile("basic", "basic"), invariants=(INVARIANT,),
                  obligations=(OBLIGATION,), observations=observations, builder_ids=("builder",), now=NOW)
    values.update(kwargs)
    return assess_gate(**values)


class GateTests(unittest.TestCase):
    def test_basic_requires_executed_evidence_and_roundtrips_strict_records(self):
        item = observed()
        result = assess([item])
        self.assertTrue(result.passed)
        for value in (BINDING, INVARIANT, OBLIGATION, item, GatePolicy.compile("basic", "basic"), result):
            self.assertEqual(type(value).from_dict(value.to_dict()), value)
            with self.assertRaises(GateError):
                type(value).from_dict(dict(value.to_dict(), unknown=True))
        untrusted = replace(item, evidence=replace(item.evidence, epistemic_status="UNVERIFIED", producer="worker"))
        self.assertEqual(assess([untrusted]).verdict, "OBSERVATION_INCOMPLETE")

    def test_stale_candidate_or_environment_cannot_reuse_pass(self):
        for binding in (replace(BINDING, artifact_digest=OTHER), replace(BINDING, environment_digest=OTHER), replace(BINDING, evaluator_digest=OTHER), replace(BINDING, plan_digest=OTHER)):
            with self.subTest(binding=binding):
                result = assess([observed(binding=binding)])
                self.assertFalse(result.passed)
                self.assertIn("safe-exit", result.missing_obligations)

    def test_non_disproof_cannot_discharge_positive_obligation(self):
        result = assess([observed(verdict="NOT_DISPROVED_WITHIN_BUDGET")])
        self.assertFalse(result.passed)
        weak = replace(INVARIANT, positive_evidence_required=False)
        self.assertTrue(assess([observed(verdict="NOT_DISPROVED_WITHIN_BUDGET")], invariants=[weak]).passed)

    def test_backed_runs_whole_frozen_cascade_and_keeps_counterexample(self):
        policy = GatePolicy.compile("backed", "backed", integration=True)
        items = [observed("e" + str(index), family) for index, family in enumerate(policy.required_families)]
        self.assertTrue(assess(items, policy).passed)
        self.assertFalse(assess(items[:-1], policy).passed)
        items.append(observed("counterexample", "adaptive", "DISPROVED"))
        result = assess(items, policy)
        self.assertEqual(result.verdict, "DISPROVED")
        self.assertIn("counterexample", result.evidence_ids)

    def test_critical_requires_independent_real_boundary_rollback_and_human_gate(self):
        policy = GatePolicy.compile("critical", "critical", real_boundary=True)
        items = [observed("e" + str(index), family) for index, family in enumerate(policy.required_families)]
        result = assess(items, policy)
        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertTrue(assess(items, policy, approval=approval(policy)).passed)
        self.assertFalse(assess(items, policy, approval=approval(policy, approved_by="builder")).passed)
        self.assertFalse(assess([replace(item, verifier_id="builder") for item in items], policy, approval=approval(policy)).passed)
        self.assertFalse(assess(items, policy, approval=approval(policy, binding=replace(BINDING, artifact_digest=OTHER))).passed)

    def test_approval_validity_is_half_open_and_future_records_do_not_pass(self):
        policy = GatePolicy.compile("critical", "critical")
        value = approval(policy)
        self.assertTrue(value.fresh("2026-09-07T11:00:00+00:00"))
        self.assertFalse(value.fresh("2026-09-07T13:00:00+00:00"))
        self.assertFalse(value.fresh("2026-09-07T10:00:00+00:00"))

    def test_inheritance_cannot_edit_existing_contract(self):
        self.assertEqual(inherit_invariants([INVARIANT], [INVARIANT]), (INVARIANT,))
        with self.assertRaisesRegex(GateError, "amendment"):
            inherit_invariants([INVARIANT], [replace(INVARIANT, positive_evidence_required=False)])

    def test_obligation_requires_each_declared_task(self):
        obligation = replace(OBLIGATION, task_ids=("task-1", "task-2"))
        self.assertFalse(assess([observed()], obligations=[obligation]).passed)
        second = observed("second-task")
        second = replace(second, evidence=replace(second.evidence, task_id="task-2"))
        self.assertTrue(assess([observed(), second], obligations=[obligation]).passed)

    def test_explicit_live_waiver_is_visible_and_expired_waiver_blocks(self):
        policy = GatePolicy.compile("basic", "basic")
        compensation = observed()
        counterexample = observed("counterexample", verdict="DISPROVED")
        waiver = Waiver("waiver-1", "safe", "temporary compensating control", "safe-exit", ("evidence-1",), approval(policy))
        self.assertEqual(Waiver.from_dict(waiver.to_dict()), waiver)
        result = assess([compensation, counterexample], policy, waivers=[waiver])
        self.assertEqual(result.status, "GREEN_WITH_WAIVER")
        self.assertIn("waiver-1", result.waiver_ids)
        result = assess([compensation, counterexample], policy, waivers=[waiver], now="2026-09-07T13:00:00+00:00")
        self.assertEqual(result.verdict, "DISPROVED")

    def test_threshold_cannot_silently_drop_required_families(self):
        with self.assertRaises(GateError):
            GatePolicy("unsafe", "backed", ("deterministic",), False, False)
        with self.assertRaises(GateError):
            GatePolicy("unsafe", "critical", ("deterministic", "property", "metamorphic", "adaptive", "rollback"), False, True)

    def test_duplicate_observations_and_ungated_invariants_fail_closed(self):
        with self.assertRaises(GateError):
            assess([observed(), observed()])
        with self.assertRaises(GateError):
            assess([observed()], invariants=[INVARIANT, replace(INVARIANT, invariant_id="extra")])
