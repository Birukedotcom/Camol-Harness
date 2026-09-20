import asyncio
import copy
import sqlite3
import unittest

from camol.events import new_event
from camol.effects import EffectRequest, outcome_payload
from camol.artifacts import ArtifactError, ArtifactStore, RunArchive
from camol.gate_runtime import acceptance_digest
from camol.orchestrator import Orchestrator
from camol.revisions import RevisionError, _impact, _quiescent, _validate_proposal, _effect_policy, _usage_snapshot, collect_revision_lineage, prior_effect_reuse, verify_revision_lineage
from camol.runner import HarnessRunner
from camol.schema import canonical_digest
from camol.state import project
from camol.store import ConcurrentAppendError, SQLiteEventStore
from camol.usage import accounted_tokens, usage_report
from tests import test_evaluation as fixture
from tests.test_gate_runtime import v5_plan
from tests import test_usage as usage_fixture


class PlanRevisionTests(unittest.TestCase):
    setUp = fixture.EvaluationLoopTests.setUp
    tearDown = fixture.EvaluationLoopTests.tearDown

    def harness(self, *, execute=False):
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(v5_plan("original"))
        orchestrator.approve_plan("original", "human-owner", state["plan_digest"])
        if execute:
            asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal("original"))
        return orchestrator, store

    def test_linked_successor_reverifies_every_task_and_preserves_usage_and_source(self):
        orchestrator, store = self.harness(execute=True)
        source = orchestrator.state("original")
        self.assertEqual(source["status"], "awaiting_acceptance")
        proposal = orchestrator.propose_revision("original", v5_plan("successor"), "Repeat with reviewed contract")
        original_events = store.read("original")
        next_state = orchestrator.apply_revision("original", proposal["proposal_digest"], "human-owner")
        self.assertEqual(orchestrator.state("original")["status"], "superseded")
        self.assertEqual(store.read("original")[:-1], original_events)
        self.assertEqual(next_state["status"], "ready")
        self.assertEqual(next_state["tasks"]["change"]["status"], "pending")
        self.assertEqual(next_state["tasks"]["change"]["completed_step_ids"], [])
        self.assertEqual(next_state["gate_assessments"], {})
        self.assertEqual(next_state["evidence"], {})
        self.assertEqual(next_state["revision"]["base_revision"], source["integration_head"])
        self.assertEqual(next_state["revision"]["source_ledger_digest"], canonical_digest(store.read("original")))
        self.assertEqual(accounted_tokens(next_state), accounted_tokens(source))
        self.assertEqual(usage_report(store.read("successor"))["totals"]["inherited_accounted_tokens"], 300)
        self.assertEqual(store.latest_run_id(), "successor")
        with self.assertRaisesRegex(ValueError, "sealed"):
            store.append(new_event("original", "RUN_STARTED", "orchestrator", {}))
        final = asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal("successor"))
        self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 2)
        self.assertEqual(accounted_tokens(final), 600)
        self.assertEqual(project(store.read("successor")), final)
        lineage = collect_revision_lineage(store, "successor")
        verify_revision_lineage(store.read("successor"), lineage)
        with self.assertRaisesRegex(ArtifactError, "complete source lineage"):
            RunArchive.export("successor", store.read("successor"), ArtifactStore(self.state), self.state / "incomplete-export")
        manifest = RunArchive.export("successor", store.read("successor"), ArtifactStore(self.state), self.state / "export", lineage_events=lineage)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(RunArchive.replay(self.state / "export"), final)
        with self.assertRaisesRegex(RevisionError, "digest"):
            verify_revision_lineage(store.read("successor"), {"original": lineage["original"][:-1]})

    def test_wrong_owner_or_stale_proposal_never_partially_seals_source(self):
        orchestrator, store = self.harness(execute=True)
        proposal = orchestrator.propose_revision("original", v5_plan("successor"), "Explicit change")
        before = store.read("original")
        for actor in ("builder", "another-person", ""):
            with self.subTest(actor=actor), self.assertRaisesRegex(RevisionError, "human owner"):
                orchestrator.apply_revision("original", proposal["proposal_digest"], actor)
            self.assertEqual(store.read("original"), before)
            self.assertFalse(store.has_run("successor"))
        orchestrator.accept_run("original", "human-owner", acceptance_digest(orchestrator.state("original")))
        with self.assertRaisesRegex(RevisionError, "advanced"):
            orchestrator.apply_revision("original", proposal["proposal_digest"], "human-owner")
        self.assertEqual(orchestrator.state("original")["status"], "completed")
        self.assertFalse(store.has_run("successor"))

    def test_atomic_transaction_rolls_back_source_seal_when_destination_insert_fails(self):
        orchestrator, store = self.harness()
        before = store.read("original")
        existing_id = before[0]["event_id"]
        destination = new_event("successor", "RUN_STARTED", "orchestrator", {})
        destination["event_id"] = existing_id
        with self.assertRaises(sqlite3.IntegrityError):
            store.append_transaction({"original": [new_event("original", "RUN_SUPERSEDED", "human-owner", {})], "successor": [destination]},
                                     expected_sequences={"original": len(before), "successor": 0})
        self.assertEqual(store.read("original"), before)
        self.assertFalse(store.has_run("successor"))
        with self.assertRaises(ConcurrentAppendError):
            store.append_transaction({"original": [new_event("original", "RUN_SUPERSEDED", "human-owner", {})]}, expected_sequences={"original": 0})
        self.assertEqual(store.read("original"), before)

    def test_impact_closure_and_global_changes_are_explicit(self):
        old = v5_plan()
        second = copy.deepcopy(old["tasks"][0])
        second.update(id="dependent", depends_on=["change"])
        old["tasks"].append(second)
        new = copy.deepcopy(old)
        new["tasks"][0]["goal"] = "Different output contract"
        result = _impact(old, new)
        self.assertEqual(result["changed_tasks"], ["change"])
        self.assertEqual(result["invalidated_tasks"], ["change", "dependent"])
        new["run"]["objective"] = "Amended global goal"
        self.assertTrue(_impact(old, new)["global_contract_changed"])

    def test_quiescence_rejects_every_unsettled_runtime_surface(self):
        orchestrator, _ = self.harness()
        state = orchestrator.state("original")
        cases = []
        item = copy.deepcopy(state)
        item["tasks"]["change"]["lease_id"] = "lease-a"
        cases.append(item)
        item = copy.deepcopy(state)
        item["reservations"] = {"reservation-a": {}}
        cases.append(item)
        item = copy.deepcopy(state)
        item["effects"] = {"effect-a": {"state": "EFFECT_UNKNOWN"}}
        cases.append(item)
        item = copy.deepcopy(state)
        item["watchers"] = {"watch-a": {"status": "waiting"}}
        cases.append(item)
        item = copy.deepcopy(state)
        item["gate_assessments"] = {"candidate-a:integration": {"phase": "integration", "candidate_id": "candidate-a"}}
        cases.append(item)
        item = copy.deepcopy(state)
        item["debug_cases"] = {"case-a": {"status": "verified", "executions": {"exec-a": {"status": "running"}}}}
        cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(RevisionError):
                _quiescent(item)

    def test_nested_proposal_contract_is_strict_even_with_recomputed_digest(self):
        orchestrator, _ = self.harness()
        proposal = orchestrator.propose_revision("original", v5_plan("successor"), "Review")
        cases = []
        item = copy.deepcopy(proposal)
        item["inherited_usage"]["accounted_tokens"] = True
        cases.append(item)
        item = copy.deepcopy(proposal)
        item["impact"]["extra"] = "weakening"
        cases.append(item)
        item = copy.deepcopy(proposal)
        item["integration_head"] = "main"
        cases.append(item)
        for item in cases:
            item["proposal_digest"] = canonical_digest({key: value for key, value in item.items() if key != "proposal_digest"})
            with self.subTest(item=item), self.assertRaises(RevisionError):
                _validate_proposal(item)

    def test_effect_identity_cannot_be_changed_or_renamed_to_authorize_a_replay(self):
        prior = dict(effect_id="effect-a", task_id="change", state="EFFECT_CONFIRMED", provider="cloud",
                     operation="deploy", target="staging", request_digest="sha256:" + "a" * 64, idempotency_key="deployment-a")
        state = {"revision": {"prior_effects": [prior], "effect_reruns": [{"effect_id": "effect-a"}]}}
        arguments = dict(provider="cloud", operation="deploy", target="staging", request_digest=prior["request_digest"], idempotency_key="deployment-a")
        self.assertEqual(prior_effect_reuse(state, "change", **arguments), prior)
        with self.assertRaisesRegex(RevisionError, "exact confirmed"):
            prior_effect_reuse(state, "change", **dict(arguments, request_digest="sha256:" + "b" * 64))
        with self.assertRaisesRegex(RevisionError, "renamed"):
            prior_effect_reuse(state, "renamed-task", **arguments)

    def test_effect_rerun_policy_requires_exact_explicit_readback(self):
        digest = "sha256:" + "a" * 64
        request = EffectRequest("effect-a", "original", "change", "lease-a", digest, "deployment-a", "cloud", "deploy", "staging", digest, digest,
                                "human-owner", "2026-09-07T10:00:00+00:00")
        previous = request.to_dict()
        previous.update({name: value for name, value in outcome_payload("effect-a", "EFFECT_CONFIRMED", resolved_at="2026-09-07T10:01:00+00:00",
                        outcome={"deployed": True}, readback={"revision": "a"}, reconciled=True).items() if name not in {"schema", "schema_version"}})
        state = {"effects": {"effect-a": previous}}
        with self.assertRaisesRegex(RevisionError, "explicit per-effect"):
            _effect_policy(state, v5_plan("successor"), [])
        policy = dict(effect_id="effect-a", strategy="reuse_confirmed", request_digest=digest, readback_digest=previous["readback_digest"])
        _, selected = _effect_policy(state, v5_plan("successor"), [policy])
        self.assertEqual(selected, [policy])
        with self.assertRaisesRegex(RevisionError, "exact prior"):
            _effect_policy(state, v5_plan("successor"), [dict(policy, readback_digest="sha256:" + "b" * 64)])

    def test_unknown_provider_charge_remains_unknown_across_revision_accounting(self):
        receipt = usage_fixture.receipt(provenance="unknown", outcome="error", input_tokens=None, output_tokens=None,
                                        cost_usd_micros=None, cache_read_tokens=None, cache_creation_tokens=None)
        snapshot = _usage_snapshot({"evidence": {"receipt": usage_fixture.evidence(receipt)}, "total_tokens": 0})
        self.assertEqual(snapshot, {"accounted_tokens": 1000, "provider_cost_usd_micros": 100000, "unknown_usage": True})
        report = usage_report([{"run_id": "successor", "type": "REVISION_LINKED", "payload": {"proposal": {"inherited_usage": snapshot}}}])
        self.assertEqual(report["totals"]["accounted_tokens"], 1000)
        self.assertEqual(report["totals"]["observed_cost_usd_micros"], 0)
        self.assertTrue(report["coverage"]["unknown_usage"])
