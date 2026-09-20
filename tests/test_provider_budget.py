import asyncio
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.adapter import AdapterError
from camol.invocations import InvocationJournal
from camol.provider_budget import ProviderBudgetError, reserve_hosted, budget_baseline
from camol.providers import ModelProfile
from camol.usage import UsageRecord
from tests import test_claude_adapter as fixture
from tests.test_providers import profile_payload


def competing_reservation(root, task, barrier, output):
    case = BudgetTests()
    case.state = Path(root)
    case.profile = ModelProfile.from_dict(profile_payload(max_turn_usd_cents=20, max_task_usd_cents=30))
    pair = case.invocation(task)
    barrier.wait(timeout=10)
    try:
        output.put(case.reserve(pair))
    except ProviderBudgetError:
        output.put(0)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.profile = ModelProfile.from_dict(profile_payload(max_turn_usd_cents=20, max_task_usd_cents=30))

    def invocation(self, task="task", turn=1, run="run"):
        folder = self.state / "packets" / run / task
        folder.mkdir(parents=True, exist_ok=True)
        journal = InvocationJournal(folder / (str(turn) + ".charge-pending.json"), packet_sha256="a" * 64,
            profile_digest=self.profile.digest(), assignment=dict(task_id=task, agent_id="worker", lease_id="lease"),
            run_id=run, turn_number=turn, workspace=self.state)
        def receipt(ceiling, cost=None):
            record = UsageRecord(invocation_id="{}-{}-{}".format(run, task, turn), run_id=run, task_id=task,
                agent_id="worker", lease_id="lease", turn_number=turn, provider="anthropic", model=None, phase="worker",
                provenance="unknown" if cost is None else "provider_observed", outcome="unknown" if cost is None else "success",
                input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_creation_tokens=None,
                cost_usd_micros=cost, reserved_tokens=100, reserved_cost_usd_micros=ceiling * 10000,
                started_at="2026-09-07T00:00:00+00:00", finished_at="2026-09-07T00:00:00+00:00")
            return [dict(kind="model_usage", producer="adapter", epistemic_status="OBSERVED", artifact_refs=[], data=record.to_dict())]
        return journal, receipt

    def reserve(self, pair, **kwargs):
        journal, receipt = pair
        return reserve_hosted(journal, state_dir=self.state, profile=self.profile, plan_digest="sha256:" + "b" * 64,
                              requested_cents=20, evidence_factory=receipt, **kwargs)

    def test_pending_intents_share_run_ceiling_and_survive_reopen(self):
        self.assertEqual(self.reserve(self.invocation("one")), 20)
        self.assertEqual(self.reserve(self.invocation("two")), 10)
        with self.assertRaisesRegex(ProviderBudgetError, "exhausted"):
            self.reserve(self.invocation("three"))

    def test_observed_settlement_releases_only_unused_budget_and_deduplicates_baseline(self):
        pair = self.invocation("one")
        self.reserve(pair)
        evidence = pair[1](20, 50000)
        pair[0].record_outcome(evidence)
        row = evidence[0]
        bound = dict(row, run_id="run", task_id="one", agent_id="worker", lease_id="lease")
        baseline = budget_baseline(dict(run_id="run", evidence={"one": bound}, tasks={"one": {}}))
        self.assertEqual(self.reserve(self.invocation("two"), baselines=[baseline]), 20)
        self.assertEqual(self.reserve(self.invocation("three"), baselines=[baseline]), 5)

    def test_terminal_unknown_holds_even_when_projection_has_no_receipt(self):
        pair = self.invocation("one")
        self.reserve(pair)
        pair[0].record_outcome(pair[1](20))
        with self.assertRaisesRegex(ProviderBudgetError, "unknown provider charge"):
            self.reserve(self.invocation("two"))

    def test_actual_overspend_is_not_clamped_to_reservation(self):
        pair = self.invocation("one")
        self.reserve(pair)
        pair[0].record_outcome(pair[1](20, 310000))
        with self.assertRaisesRegex(ProviderBudgetError, "exhausted"):
            self.reserve(self.invocation("two"))

    def test_revision_cannot_escape_unreported_ancestor_intent(self):
        self.reserve(self.invocation("one", run="parent"))
        baseline = dict(run_id="parent", records=[], other_cost=0, inherited_unknown=False)
        with self.assertRaisesRegex(ProviderBudgetError, "unknown provider charge"):
            self.reserve(self.invocation("two", run="successor"), baselines=[baseline])

    def test_revision_known_ancestor_charge_and_legacy_task_charge_are_preserved(self):
        pair = self.invocation("one", run="parent")
        self.reserve(pair)
        pair[0].record_outcome(pair[1](20, 230000))
        baseline = dict(run_id="parent", records=[], other_cost=0, inherited_unknown=False)
        self.assertEqual(self.reserve(self.invocation("two", run="successor"), baselines=[baseline]), 7)

    def test_legacy_known_task_bill_is_charged_and_unknown_legacy_bill_blocks(self):
        legacy = dict(kind="model_usage", producer="adapter", epistemic_status="OBSERVED", task_id="task",
                      run_id="run", agent_id="worker", lease_id="old", data=dict(cost_usd_micros=240000))
        state = dict(run_id="run", evidence={"bill": legacy}, tasks={"task": {}})
        baseline = budget_baseline(state)
        self.assertEqual(self.reserve(self.invocation(), baselines=[baseline]), 6)
        legacy["data"]["cost_usd_micros"] = None
        with self.assertRaisesRegex(ProviderBudgetError, "unknown provider usage"):
            self.reserve(self.invocation("second"), baselines=[budget_baseline(state)])

    def test_corrupt_or_linked_journal_denies_admission(self):
        pair = self.invocation("one")
        self.reserve(pair)
        pair[0].path.write_text('{"schema":1,"schema":2}')
        with self.assertRaises(ProviderBudgetError):
            self.reserve(self.invocation("two"))
        pair[0].path.unlink()
        pair[0].path.symlink_to(self.state / "absent")
        with self.assertRaises(ProviderBudgetError):
            self.reserve(self.invocation("three"))

    def test_budget_change_and_duplicate_invocation_are_denied(self):
        pair = self.invocation()
        self.reserve(pair)
        with self.assertRaisesRegex(AdapterError, "already launched"):
            self.reserve(pair)
        self.profile = ModelProfile.from_dict(profile_payload(max_turn_usd_cents=20, max_run_usd_cents=40))
        with self.assertRaisesRegex(ProviderBudgetError, "disagree"):
            self.reserve(self.invocation("two"))

    def test_independent_processes_cannot_oversubscribe_the_same_run(self):
        context = multiprocessing.get_context("spawn")
        barrier, output = context.Barrier(4), context.Queue()
        children = [context.Process(target=competing_reservation, args=(str(self.state), "task-" + str(i), barrier, output)) for i in range(4)]
        try:
            for child in children:
                child.start()
            budgets = [output.get(timeout=15) for _ in children]
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
            self.assertGreaterEqual(sum(budgets), 20)
            self.assertLessEqual(sum(budgets), 30)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(timeout=5)
            output.close()

    def test_symlinked_lock_and_task_tree_cannot_hide_prior_reservations(self):
        pair = self.invocation("one")
        self.reserve(pair)
        lock = self.state / "provider-budget.lock"
        lock.unlink()
        lock.symlink_to(self.state / "missing")
        with self.assertRaises(ProviderBudgetError):
            self.reserve(self.invocation("two"))
        lock.unlink()
        target = pair[0].path.parent
        moved = self.state / "moved"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ProviderBudgetError):
            self.reserve(self.invocation("three"))


class ConcurrentProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_overlapping_providers_receive_only_aggregate_available_budget(self):
        case = fixture.ClaudeAdapterTests()
        await case.asyncSetUp()
        try:
            case.profile.write_text(json.dumps(profile_payload(max_turn_usd_cents=20, max_task_usd_cents=30)))
            source = case.executable.read_text().replace("import hashlib, json, re, sys", "import hashlib, json, re, sys, time\ntime.sleep(0.2)")
            source = source.replace("0.03", "float(sys.argv[sys.argv.index('--max-budget-usd') + 1])")
            source = source.replace("prompt = sys.stdin.read()", """Path('started-' + sys.argv[sys.argv.index('--max-budget-usd') + 1]).touch()
deadline = time.monotonic() + 5
while len(list(Path('.').glob('started-*'))) < 2:
    if time.monotonic() > deadline: raise RuntimeError('providers were serialized')
    time.sleep(0.01)
prompt = sys.stdin.read()""")
            case.executable.write_text(source)
            assignments = [dict(case.assignment, task_id="task-" + str(i), lease_id="lease-" + str(i)) for i in range(2)]
            with patch.dict(os.environ, {"PATH": str(case.bin) + os.pathsep + os.environ["PATH"]}):
                results = await asyncio.gather(*(case.adapter().execute_turn(case.agent, assignment, case.packet, 1,
                                                 cost_budget_cents=20) for assignment in assignments))
            records = [next(item["data"] for item in result["_camol_observed_evidence"] if item["kind"] == "model_usage") for result in results]
            self.assertEqual(sorted(record["reserved_cost_usd_micros"] for record in records), [100000, 200000])
            self.assertEqual(sum(record["cost_usd_micros"] for record in records), 300000)
            # Cached replay cannot allocate or launch again, even at zero budget.
            adapter = case.adapter()
            with patch.object(adapter.sandbox_backend, "run", side_effect=AssertionError("cache must not launch")):
                repeated = await adapter.execute_turn(case.agent, assignments[0], case.packet, 1, cost_budget_cents=0)
            self.assertEqual(repeated["_camol_usage_invocation_id"], results[0]["_camol_usage_invocation_id"])
        finally:
            await case.asyncTearDown()
