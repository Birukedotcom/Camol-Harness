import asyncio
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.benchmark import BenchmarkError, BenchmarkTrial
from camol.campaign import BenchmarkCampaign, CampaignStore, _trial_identity, validate_campaign
from camol.schema import canonical_digest


def manifest():
    digest = canonical_digest("fixture")
    return dict(schema="camol.benchmark_campaign", schema_version=1, campaign_id="canary-one",
                suite="native-fixture", suite_version="one", dataset_digest=digest,
                selection=dict(method="explicit_pinned", seed=42, cohort="canary"),
                tasks=[dict(task_id="addition", task_digest=digest, source_revision="a" * 40,
                            evaluator_digest=digest, environment_digest=digest, public_tests_digest=digest, family="python")],
                arms=["claude_direct", "camol_one", "camol_adaptive"], repetitions=2,
                configuration=dict(model="fixture", model_version="one", effort="low", sampling_digest=digest,
                                   context_digest=digest, tool_policy_digest=digest, network_policy_digest=digest,
                                   secrets_policy_digest=digest, hardware_digest=digest,
                                   harness_revisions={arm: "b" * 40 for arm in ("claude_direct", "camol_one", "camol_adaptive")}),
                budgets=dict(max_tokens=100, max_cost_usd_micros=0, max_elapsed_ms=10000),
                promotion=dict(minimum_repetitions=3, minimum_accepted_gain=1, forbid_task_regressions=True))


class FixtureSuite:
    def __init__(self):
        self.calls = []
        self.cleanups = []
        self.bad_gold = False
        self.fail = False
        self.bad_cleanup = False
        self.pending = None
        self.result_mutation = None
        self.last_result = None

    async def validate_gold(self, task, campaign):
        gold = subprocess.run([sys.executable, "-I", "-c", "assert 1 + 1 == 2"], capture_output=True)
        noop = subprocess.run([sys.executable, "-I", "-c", "assert 1 + 1 == 3"], capture_output=True)
        return dict(**{key: task[key] for key in ("task_id", "task_digest", "evaluator_digest", "environment_digest")},
                    manifest_digest=canonical_digest(campaign), gold_accepted=gold.returncode == 0 and not self.bad_gold,
                    noop_rejected=noop.returncode != 0, gold_evidence_digest=canonical_digest(gold.returncode),
                    noop_evidence_digest=canonical_digest(noop.returncode), budget_enforced=True)

    async def run_trial(self, task, arm, seed, campaign):
        self.calls.append((task["task_id"], arm, seed))
        if self.pending:
            await self.pending.wait()
        if self.fail:
            raise RuntimeError("private token body should not be logged")
        actual = subprocess.run([sys.executable, "-I", "-c", "print(1 + 1)"], capture_output=True, text=True)
        repetition = seed - campaign["selection"]["seed"]
        trial = BenchmarkTrial(
            trial_id=_trial_identity(campaign, task, repetition, arm), arm=arm,
            **{name: task[name] for name in ("task_id", "task_digest", "source_revision", "evaluator_digest")},
            **{name: campaign["configuration"][name] for name in ("model", "model_version", "effort", "tool_policy_digest")},
            budget_digest=canonical_digest(campaign["budgets"]), outcome="accepted" if actual.stdout == "2\n" else "rejected",
            accepted_behavior=1, invariant_violations=0, regressions=0, tokens=0, cost_usd_micros=0,
            elapsed_ms=20, retries=0, human_interventions=0, tool_calls=1, recovery_success=True,
            evidence_complete=True, recorded_at="2026-09-07T00:00:00+00:00")
        result = dict(trial=trial.to_dict(), candidate_digest=canonical_digest(actual.stdout),
                      artifact_manifest_digest=canonical_digest([actual.stdout, actual.stderr]),
                      environment_digest=task["environment_digest"], manifest_digest=canonical_digest(campaign),
                      seed=seed, usage_observed=True)
        if self.result_mutation:
            self.result_mutation(result)
        self.last_result = result
        return result

    async def teardown(self, trial_id):
        self.cleanups.append(trial_id)
        if self.bad_cleanup:
            raise RuntimeError("cleanup did not finish")


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "campaign.sqlite3"
        self.store = CampaignStore(self.path)
        self.plan = manifest()
        self.campaign = BenchmarkCampaign.create(self.store, self.plan, approved_by="owner", manifest_digest=canonical_digest(self.plan))
        self.suite = FixtureSuite()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_real_fixture_campaign_pairs_rotates_and_resumes_without_reexecution(self):
        result = await self.campaign.run(self.suite)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["trials"]), 6)
        self.assertEqual([arm for _, arm, _ in self.suite.calls],
                         ["claude_direct", "camol_one", "camol_adaptive", "camol_one", "camol_adaptive", "claude_direct"])
        report = self.campaign.report()
        self.assertEqual(len(report["pairs"]), 4)
        self.assertFalse(report["statistical_claim"])
        self.assertFalse(report["promotion_review"]["camol_adaptive"]["eligible_for_human_review"])
        self.assertTrue(all(pair["delta_camol_minus_direct"]["accepted_behavior"] == 0 for pair in report["pairs"]))
        self.store.close()
        self.store = CampaignStore(self.path)
        resumed = BenchmarkCampaign(self.store, self.plan["campaign_id"])
        self.assertEqual(await resumed.run(self.suite), result)
        self.assertEqual(len(self.suite.calls), 6)
        self.assertEqual(resumed.report(), report)

    async def test_gold_failure_is_quarantined_not_a_model_failure(self):
        self.suite.bad_gold = True
        result = await self.campaign.run(self.suite)
        self.assertEqual(result["status"], "completed_with_quarantine")
        self.assertEqual(self.suite.calls, [])
        self.assertEqual(self.campaign.report()["pairs"], [])
        self.assertIn("addition", self.campaign.report()["quarantined_tasks"])

    async def test_failed_trial_reservation_survives_restart_no_automatic_respend(self):
        self.suite.fail = True
        result = await self.campaign.run(self.suite)
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reserved_unknown"]["max_tokens"], 100)
        self.assertEqual(len(self.suite.calls), 1)
        self.suite.fail = False
        await BenchmarkCampaign(self.store, self.plan["campaign_id"]).run(self.suite)
        self.assertEqual(len(self.suite.calls), 1)
        self.assertNotIn("private token", str(self.store.events(self.plan["campaign_id"])))
        trial_id = next(iter(result["trials"]))
        reconciled = await self.suite.run_trial(self.plan["tasks"][0], "claude_direct", 42, self.plan)
        before = self.store.events(self.plan["campaign_id"])
        with self.assertRaises(BenchmarkError):
            self.campaign.reconcile(trial_id, reconciled, approved_by="worker")
        self.assertEqual(before, self.store.events(self.plan["campaign_id"]))
        self.campaign.reconcile(trial_id, reconciled, approved_by="owner")
        final = await self.campaign.run(self.suite)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(self.suite.calls), 7)  # original unknown + external reconciliation fixture + five missing

    async def test_cancellation_cleans_and_retains_unknown_identity(self):
        self.suite.pending = asyncio.Event()
        pending = asyncio.create_task(self.campaign.run(self.suite))
        for _ in range(100):
            if self.suite.calls:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.suite.calls)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        state = self.campaign.state()
        self.assertEqual(state["status"], "attention")
        self.assertEqual(len(self.suite.cleanups), 1)
        self.assertEqual(state["reserved_unknown"]["max_tokens"], 100)

    async def test_cleanup_failure_cannot_hide_or_rerun_a_finished_trial(self):
        self.suite.bad_cleanup = True
        result = await self.campaign.run(self.suite)
        self.assertEqual(result["status"], "attention")
        identity = next(iter(result["trials"]))
        self.assertEqual(result["trials"][identity]["status"], "finished")
        self.assertFalse(result["trials"][identity]["cleanup_complete"])
        self.suite.bad_cleanup = False
        await self.campaign.cleanup(self.suite, identity)
        self.assertEqual((await self.campaign.run(self.suite))["status"], "completed")
        self.assertEqual(len(self.suite.calls), 6)

    async def test_wrong_environment_candidate_identity_or_unknown_usage_rejected(self):
        self.suite.result_mutation = lambda result: result.update(environment_digest=canonical_digest("other"))
        state = await self.campaign.run(self.suite)
        self.assertEqual(state["status"], "attention")
        self.assertEqual(self.campaign.report()["pairs"], [])
        self.assertEqual(len(self.suite.calls), 1)

    async def test_rejected_overbudget_result_preserves_unverified_usage_above_reservation(self):
        self.suite.result_mutation = lambda result: result["trial"].update(tokens=200, cost_usd_micros=12000)
        state = await self.campaign.run(self.suite)
        self.assertEqual(state["status"], "attention")
        self.assertEqual(state["conservative_unresolved_usage"]["tokens"], 200)
        self.assertEqual(state["conservative_unresolved_usage"]["cost_usd_micros"], 12000)
        self.assertEqual(next(iter(state["trials"].values()))["reported_usage"]["tokens"], 200)

    async def test_active_reconcile_cleanup_and_second_driver_are_denied_without_poisoning(self):
        self.suite.pending = asyncio.Event()
        pending = asyncio.create_task(self.campaign.run(self.suite))
        while not self.suite.calls:
            await asyncio.sleep(0.001)
        identity = next(iter(self.campaign.state()["trials"]))
        result = await FixtureSuite().run_trial(self.plan["tasks"][0], "claude_direct", 42, self.plan)
        other_store = CampaignStore(self.path)
        other = BenchmarkCampaign(other_store, self.plan["campaign_id"])
        try:
            before = self.store.events(self.plan["campaign_id"])
            with self.assertRaisesRegex(BenchmarkError, "active execution owner"):
                other.reconcile(identity, result, approved_by="owner")
            with self.assertRaisesRegex(BenchmarkError, "active execution owner"):
                await other.cleanup(self.suite, identity)
            with self.assertRaisesRegex(BenchmarkError, "active execution owner"):
                await other.run(FixtureSuite())
            self.assertEqual(self.store.events(self.plan["campaign_id"]), before)
            self.suite.pending.set()
            self.assertEqual((await pending)["status"], "completed")
            self.assertEqual(len(self.suite.calls), 6)
        finally:
            self.suite.pending.set()
            await asyncio.gather(pending, return_exceptions=True)
            other_store.close()

    async def test_invalid_append_is_rejected_before_corrupting_ledger(self):
        await self.campaign.run(self.suite)
        state = self.campaign.state()
        identity, trial = next(iter(state["trials"].items()))
        before = self.store.events(self.plan["campaign_id"])
        with self.assertRaises(BenchmarkError):
            self.store.append(self.plan["campaign_id"], "FINISHED", dict(trial_id=identity, result=trial["result"]), expected_seq=state["last_seq"])
        self.assertEqual(before, self.store.events(self.plan["campaign_id"]))
        self.assertEqual(self.campaign.state()["status"], "completed")

    async def test_crashed_running_trial_requires_explicit_owner_recovery(self):
        task = self.plan["tasks"][0]
        readiness = await self.suite.validate_gold(task, self.plan)
        self.campaign._append("READY", readiness, self.campaign.state())
        identity = _trial_identity(self.plan, task, 0, "claude_direct")
        self.campaign._append("STARTED", dict(trial_id=identity, task_id=task["task_id"], repetition=0, arm="claude_direct"), self.campaign.state())
        result = await FixtureSuite().run_trial(task, "claude_direct", 42, self.plan)
        with self.assertRaisesRegex(BenchmarkError, "declare crashed"):
            self.campaign.reconcile(identity, result, approved_by="owner")
        with self.assertRaisesRegex(BenchmarkError, "running trial"):
            await self.campaign.cleanup(self.suite, identity)
        with self.assertRaises(BenchmarkError):
            self.campaign.recover_interrupted(identity, approved_by="worker")
        self.campaign.recover_interrupted(identity, approved_by="owner")
        self.campaign.reconcile(identity, result, approved_by="owner")
        await self.campaign.cleanup(self.suite, identity)
        self.assertEqual((await self.campaign.run(self.suite))["status"], "completed")
        self.assertEqual(len(self.suite.calls), 5)

    async def test_known_overspend_can_close_as_failed_without_erasing_actual_usage(self):
        self.suite.result_mutation = lambda result: result["trial"].update(tokens=200, cost_usd_micros=12000)
        state = await self.campaign.run(self.suite)
        identity = next(iter(state["trials"]))
        returned = self.suite.last_result
        with self.assertRaises(BenchmarkError):
            self.campaign.reconcile(identity, returned, approved_by="owner")
        self.campaign.reconcile_failure(identity, returned, approved_by="owner")
        self.suite.result_mutation = None
        state = await self.campaign.run(self.suite)
        self.assertEqual(state["status"], "completed_with_failures")
        self.assertEqual(state["reserved_unknown"]["max_tokens"], 0)
        self.assertEqual(state["trials"][identity]["result"]["trial"]["cost_usd_micros"], 12000)
        report = self.campaign.report()
        self.assertEqual(report["policy_violated_trials"], [identity])
        self.assertFalse(any(item["eligible_for_human_review"] for item in report["promotion_review"].values()))

    def test_freeze_rejects_mutability_unbounded_and_unknown_contracts(self):
        for mutate in (lambda p: p.update(schema_version=True), lambda p: p.update(extra=True),
                       lambda p: p["arms"].append("claude_direct"), lambda p: p["tasks"].append(p["tasks"][0]),
                       lambda p: p["budgets"].update(max_tokens=0), lambda p: p["promotion"].update(forbid_task_regressions=False)):
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(ValueError):
                validate_campaign(plan)
        with self.assertRaises(BenchmarkError):
            BenchmarkCampaign.create(self.store, self.plan, approved_by="owner", manifest_digest=canonical_digest("stale"))
        reader = CampaignStore(self.path, read_only=True)
        try:
            with self.assertRaises(BenchmarkError):
                reader.append("canary-one", "STARTED", {}, expected_seq=1)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
