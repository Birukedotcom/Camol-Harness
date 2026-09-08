import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from camol.adapter import ProcessAgentAdapter
from camol.budget_wait import apply
from camol.invocations import InvocationJournal
from camol.orchestrator import Orchestrator
from camol.provider_budget import ProviderBudgetError, ProviderBudgetPending, reserve_hosted
from camol.providers import ModelProfile
from camol.runner import HarnessRunner
from camol.runbook import load_runbook
from camol.schema import canonical_digest
from camol.state import project
from camol.usage import UsageRecord
from tests import test_provider_budget, test_runner
from tests.test_providers import profile_payload


class BudgetWaitTests(unittest.TestCase):
    def state_and_event(self):
        state = dict(run_id="run", tasks={
            "waiting": dict(id="waiting", status="running", agent_id="worker-a", lease_id="lease-a", turn_count=0),
            "other": dict(id="other", status="running", agent_id="worker-b", lease_id="lease-b", turn_count=0)})
        pending = dict(run_id="run", task_id="other", agent_id="worker-b", lease_id="lease-b", turn_number=1, invocation_id="call-b")
        payload = dict(task_id="waiting", agent_id="worker-a", lease_id="lease-a", code="BUDGET_RESERVED", pending=[pending])
        payload["wait_digest"] = canonical_digest(payload)
        return state, dict(type="PROVIDER_BUDGET_WAITING", run_id="run", payload=payload)

    def test_wait_replay_is_within_same_lease_and_requires_exact_clear(self):
        state, event = self.state_and_event()
        before = copy.deepcopy(state)
        apply(state, event)
        self.assertEqual(state["tasks"]["waiting"]["status"], "running")
        self.assertEqual(state["tasks"]["waiting"]["turn_count"], 0)
        with self.assertRaises(ValueError):
            apply(state, event)
        cleared = dict(type="PROVIDER_BUDGET_WAIT_CLEARED", run_id="run", payload=dict(
            task_id="waiting", agent_id="worker-a", lease_id="lease-a", wait_digest=event["payload"]["wait_digest"], reason="settlement_recheck"))
        broken = copy.deepcopy(cleared)
        broken["payload"]["lease_id"] = "old-lease"
        with self.assertRaises(ValueError):
            apply(state, broken)
        apply(state, cleared)
        self.assertEqual(state, before)

    def test_wait_denies_self_foreign_stale_duplicate_and_unknown_subjects(self):
        for field, value in (("run_id", "other-run"), ("task_id", "waiting"), ("lease_id", "stale"),
                             ("turn_number", 2), ("turn_number", True), ("agent_id", "missing")):
            state, event = self.state_and_event()
            event["payload"]["pending"][0][field] = value
            event["payload"]["wait_digest"] = canonical_digest({key: item for key, item in event["payload"].items() if key != "wait_digest"})
            with self.assertRaises(ValueError):
                apply(state, event)
        state, event = self.state_and_event()
        event["payload"]["pending"] *= 2
        event["payload"]["wait_digest"] = canonical_digest({key: item for key, item in event["payload"].items() if key != "wait_digest"})
        with self.assertRaises(ValueError):
            apply(state, event)

    def test_pending_classification_does_not_promote_known_exhaustion_or_unknown_cost(self):
        case = test_provider_budget.BudgetTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        first, second = case.invocation("first"), case.invocation("second")
        case.reserve(first)
        case.reserve(second)
        with self.assertRaises(ProviderBudgetPending) as caught:
            case.reserve(case.invocation("third"))
        self.assertEqual({item["task_id"] for item in caught.exception.pending}, {"first", "second"})
        first[0].record_outcome(first[1](20, 310000))
        with self.assertRaises(ProviderBudgetError) as exhausted:
            case.reserve(case.invocation("fourth"))
        self.assertNotIsInstance(exhausted.exception, ProviderBudgetPending)

    def test_unowned_pending_reservation_never_enters_auto_wait(self):
        async def scenario():
            state, event = self.state_and_event()
            runner = HarnessRunner.__new__(HarnessRunner)
            runner._provider_turns = {}
            self.assertFalse(await runner._wait_for_provider_budget("run", event["payload"], ProviderBudgetPending(event["payload"]["pending"])))
            # An active logical turn is insufficient if its physical invocation
            # differs from the reservation (for example after a crash/restart).
            runner._provider_turns = {("run", "other", "worker-b", "lease-b", 1): SimpleNamespace(hosted_invocation_id="different-call")}
            self.assertFalse(await runner._wait_for_provider_budget("run", event["payload"], ProviderBudgetPending(event["payload"]["pending"])))
        asyncio.run(scenario())

    def test_adapter_cannot_understate_its_allocated_intent(self):
        case = test_provider_budget.BudgetTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        for task, known in (("too-small", False), ("pretend-settled", True)):
            journal, receipt = case.invocation(task)
            with self.assertRaises(ProviderBudgetError):
                reserve_hosted(journal, state_dir=case.state, profile=case.profile, plan_digest="sha256:" + "b" * 64,
                    requested_cents=20, evidence_factory=lambda ceiling: receipt(ceiling if known else 1, 0 if known else None))
            self.assertFalse(journal.path.exists())

    def test_cancellation_clears_wait_without_granting_or_spending(self):
        async def scenario():
            state, event = self.state_and_event()
            class Control:
                def state(self, run_id):
                    return copy.deepcopy(state)
                def _emit(self, run_id, kind, payload):
                    apply(state, dict(run_id=run_id, type=kind, payload=payload))
            runner = HarnessRunner.__new__(HarnessRunner)
            runner.orchestrator = Control()
            runner._provider_turns = {("run", "other", "worker-b", "lease-b", 1): SimpleNamespace(hosted_invocation_id="call-b")}
            waiting = asyncio.create_task(runner._wait_for_provider_budget("run", event["payload"], ProviderBudgetPending(event["payload"]["pending"])))
            await asyncio.sleep(0)
            self.assertIn("runtime_wait", state["tasks"]["waiting"])
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertNotIn("runtime_wait", state["tasks"]["waiting"])
            self.assertEqual(state["tasks"]["waiting"]["turn_count"], 0)
        asyncio.run(scenario())

    def test_real_n_box_run_wakes_on_settlement_without_new_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, state_dir, store = test_runner.RunnerTests()._workspace_and_store(temporary)
            runner = None
            profile = ModelProfile.from_dict(profile_payload(max_turn_usd_cents=30, max_task_usd_cents=30, max_run_usd_cents=30))
            calls = []
            class BudgetedProcess(ProcessAgentAdapter):
                async def execute_turn(self, agent, assignment, packet, turn_number, **kwargs):
                    await self._authorize_launch(assignment, turn_number)
                    folder = self.state_dir / "packets" / self.run_id / assignment["task_id"]
                    folder.mkdir(parents=True, exist_ok=True)
                    journal = InvocationJournal(folder / (str(turn_number) + ".charge-pending.json"),
                        packet_sha256="a" * 64, profile_digest=profile.digest(), assignment=assignment,
                        run_id=self.run_id, turn_number=turn_number, workspace=self.workspace)
                    invocation_id = assignment["task_id"] + "-" + str(turn_number)
                    def evidence(ceiling, cost=None):
                        record = UsageRecord(invocation_id=invocation_id, run_id=self.run_id, task_id=assignment["task_id"],
                            agent_id=assignment["agent_id"], lease_id=assignment["lease_id"], turn_number=turn_number,
                            provider="anthropic", model=None, phase="worker", provenance="unknown" if cost is None else "provider_observed",
                            outcome="unknown" if cost is None else "success", input_tokens=None if cost is None else 0, output_tokens=None if cost is None else 0,
                            cache_read_tokens=None if cost is None else 0, cache_creation_tokens=None if cost is None else 0, cost_usd_micros=cost, reserved_tokens=100,
                            reserved_cost_usd_micros=ceiling * 10000, started_at="2026-09-07T00:00:00+00:00", finished_at="2026-09-07T00:00:00+00:00")
                        return [dict(kind="model_usage", producer="adapter", epistemic_status="OBSERVED", artifact_refs=[], data=record.to_dict())]
                    ceiling = reserve_hosted(journal, state_dir=self.state_dir, profile=profile,
                        plan_digest=packet["run"]["plan_digest"], requested_cents=30, evidence_factory=evidence)
                    self.hosted_invocation_id = invocation_id
                    calls.append(invocation_id)
                    await asyncio.sleep(0.15)
                    result = await super().execute_turn(agent, assignment, packet, turn_number, **kwargs)
                    observed = evidence(ceiling, 0)
                    journal.record_outcome(observed)
                    result.setdefault("_camol_observed_evidence", []).extend(observed)
                    return result
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(load_runbook(Path(test_runner.ROOT) / "examples/local-n-box-runbook.json"))
                orchestrator.approve_plan(state["run_id"], "owner", state["plan_digest"])
                runner = HarnessRunner(orchestrator, workspace, state_dir=state_dir)
                with patch("camol.runner.create_agent_adapter", side_effect=lambda kind, *args, **kwargs: BudgetedProcess(*args, **kwargs)):
                    final = asyncio.run(runner.run_until_terminal(state["run_id"]))
                self.assertEqual(final["status"], "completed", (final.get("terminal"), {key: value.get("waiting") for key, value in final["tasks"].items()}))
                events = store.read(state["run_id"])
                waits = [event for event in events if event["type"] == "PROVIDER_BUDGET_WAITING"]
                self.assertTrue(waits)
                from camol.overview import fleet_overview
                from camol.runner import summary
                waiting_state = project(events[:waits[0]["seq"]])
                waiting_task = waits[0]["payload"]["task_id"]
                self.assertEqual(summary(waiting_state)["tasks"][waiting_task]["runtime_wait"]["code"], "BUDGET_RESERVED")
                self.assertTrue(any(task["waiting_code"] == "BUDGET_RESERVED" for task in fleet_overview(waiting_state, attention=True)["tasks"]))
                self.assertEqual(len(waits), sum(event["type"] == "PROVIDER_BUDGET_WAIT_CLEARED" for event in events))
                self.assertTrue(all(task["attempts"] == 1 for task in final["tasks"].values()))
                self.assertEqual(len(calls), len(set(calls)))
                self.assertEqual(project(events), final)
            finally:
                if runner:
                    runner.close()
                store.close()
