import asyncio
import copy
import unittest
from datetime import datetime, timezone

from camol.capacity import CapacityBroker, CapacityError
from camol.orchestrator import Orchestrator
from camol.runner import HarnessRunner
from camol.state import project
from camol.store import SQLiteEventStore
from tests import test_evaluation as fixture
from tests import test_capacity as capacity_fixture


class CapacityRuntimeTests(unittest.TestCase):
    setUp = fixture.EvaluationLoopTests.setUp
    tearDown = fixture.EvaluationLoopTests.tearDown

    def harness(self, *, publish=True, identifier="capacity-run"):
        broker = CapacityBroker(self.state / "shared-capacity.sqlite3")
        self.addCleanup(broker.close)
        if publish:
            now = datetime.now(timezone.utc)
            for kind in ("target", "runtime"):
                broker.publish(capacity_fixture.supply(kind, kind, now=now))
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(capacity_fixture.v6_plan(identifier))
        orchestrator.approve_plan(identifier, "human-owner", state["plan_digest"])
        runner = HarnessRunner(orchestrator, self.source, state_dir=self.state, capacity_broker=broker)
        self.addCleanup(runner.close)
        return runner, orchestrator, store, broker

    def test_real_v6_run_reserves_before_lease_refines_releases_and_replays(self):
        runner, orchestrator, store, broker = self.harness()
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["status"], "awaiting_acceptance", {"terminal": final.get("terminal"), "waiting": final["tasks"]["change"].get("waiting"), "capacity_waits": final.get("capacity_waits")})
        self.assertEqual(final["tasks"]["change"]["attempts"], 2)
        reservations = broker.reservations()
        self.assertEqual(len(reservations), 2)
        self.assertTrue(all(item["status"] == "released" for item in reservations))
        self.assertTrue(all(item["status"] == "released" for item in final["global_capacity"].values()))
        events = store.read("capacity-run")
        first_capacity = next(index for index, event in enumerate(events) if event["type"] == "GLOBAL_CAPACITY_RESERVED")
        first_lease = next(index for index, event in enumerate(events) if event["type"] == "TASK_LEASED")
        self.assertLess(first_capacity, first_lease)
        self.assertEqual(project(events), final)
        without_capacity = [event for event in events if event["type"] != "GLOBAL_CAPACITY_RESERVED"]
        with self.assertRaises(CapacityError):
            project(without_capacity)
        forged = copy.deepcopy(events)
        target = next(event for event in forged if event["type"] == "GLOBAL_CAPACITY_RESERVED")
        target["payload"]["reservation"]["request"]["needs"][0]["resources"]["memory_bytes"] = 0
        with self.assertRaises(CapacityError):
            project(forged)

    def test_missing_supply_stays_visible_waiting_without_spending_then_resumes(self):
        runner, orchestrator, store, broker = self.harness(publish=False)
        state = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(state["total_tokens"], 0)
        self.assertEqual(state["tasks"]["change"]["attempts"], 0)
        self.assertIn("change", state["capacity_waits"])
        self.assertFalse(any(event["type"] == "TASK_LEASED" for event in store.read("capacity-run")))
        now = datetime.now(timezone.utc)
        for kind in ("target", "runtime"):
            broker.publish(capacity_fixture.supply(kind, kind, now=now))
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["status"], "awaiting_acceptance", {"terminal": final.get("terminal"), "waiting": final["tasks"]["change"].get("waiting"), "capacity_waits": final.get("capacity_waits")})
        self.assertNotIn("change", final.get("capacity_waits", {}))

    def test_runs_in_separate_state_dirs_share_one_slot_and_take_turns(self):
        self._shared_run_test()

    def test_same_named_runs_have_isolated_worktrees_but_share_physical_capacity(self):
        self._shared_run_test(same_identity=True)

    def _shared_run_test(self, *, same_identity=False):
        broker = CapacityBroker(self.state / "shared.sqlite3")
        self.addCleanup(broker.close)
        now = datetime.now(timezone.utc)
        for kind in ("target", "runtime"):
            broker.publish(capacity_fixture.supply(kind, kind, now=now, slots=1))
        runners, orchestrators, identifiers = [], [], []
        for folder in ("one", "two"):
            directory = self.state / folder
            store = SQLiteEventStore(directory / "events.sqlite3")
            self.addCleanup(store.close)
            orchestrator = Orchestrator(store)
            identifier = "same-run-id" if same_identity else "capacity-" + folder
            state = orchestrator.initialize(capacity_fixture.v6_plan(identifier))
            orchestrator.approve_plan(state["run_id"], "human-owner", state["plan_digest"])
            runner = HarnessRunner(orchestrator, self.source, state_dir=directory, capacity_broker=broker)
            self.addCleanup(runner.close)
            runners.append(runner)
            orchestrators.append(orchestrator)
            identifiers.append(identifier)
        async def drive():
            for _ in range(5):
                active = [runner.run_until_terminal(identifier) for runner, orchestrator, identifier in zip(runners, orchestrators, identifiers)
                          if orchestrator.state(identifier)["status"] != "awaiting_acceptance"]
                if not active:
                    return
                await asyncio.gather(*active)
        asyncio.run(drive())
        outcomes = [item.state(identifier) for item, identifier in zip(orchestrators, identifiers)]
        self.assertTrue(all(item["status"] == "awaiting_acceptance" for item in outcomes),
                        [{"status": item["status"], "task": item["tasks"]["change"]["status"], "waits": item.get("capacity_waits")} for item in outcomes])
        reservations = broker.reservations()
        self.assertEqual(len({item["reservation"]["request"]["controller_id"] for item in reservations}), 2)
        self.assertEqual(len(reservations), 4)
        self.assertTrue(all(item["status"] == "released" for item in reservations))

    def test_canceled_process_releases_shared_slot_after_explicit_stop_reconciliation(self):
        script = self.source / "agent.py"
        script.write_text("import time\ntime.sleep(5)\n" + script.read_text())
        fixture.git(self.source, "add", "agent.py")
        fixture.git(self.source, "commit", "-m", "slow cancellation fixture")
        runner, orchestrator, _, broker = self.harness()
        async def cancel():
            job = asyncio.create_task(runner.run_until_terminal("capacity-run"))
            for _ in range(100):
                if orchestrator.state("capacity-run")["tasks"]["change"]["status"] == "running":
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("worker never entered running state")
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            runner.force_interrupt("capacity-run", requested_by="human-owner")
        asyncio.run(cancel())
        self.assertTrue(all(item["status"] == "released" for item in broker.reservations()))
        self.assertTrue(all(item["status"] == "released" for item in orchestrator.state("capacity-run")["global_capacity"].values()))
        self.assertEqual(broker.reserve(capacity_fixture.request("next-run"), local_reservation_id="next-local")["status"], "granted")

    def test_scheduler_uses_next_suitable_worker_when_first_pool_is_exhausted(self):
        broker = CapacityBroker(self.state / "shared-capacity.sqlite3")
        self.addCleanup(broker.close)
        now = datetime.now(timezone.utc)
        for kind in ("target", "runtime"):
            broker.publish(capacity_fixture.supply(kind, kind, now=now))
        blocked = capacity_fixture.supply("blocked-target", "target", now=now)
        blocked["outside_usage"]["slots"] = blocked["limits"]["slots"]
        broker.publish(blocked)
        plan = capacity_fixture.v6_plan()
        alternative = copy.deepcopy(plan["agents"][0])
        alternative.update(id="other-worker", box="other-box")
        plan["agents"][0]["capacity_pools"]["target"] = "blocked-target"
        plan["agents"].append(alternative)
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        initial = orchestrator.initialize(plan)
        orchestrator.approve_plan("capacity-run", "human-owner", initial["plan_digest"])
        runner = HarnessRunner(orchestrator, self.source, state_dir=self.state, capacity_broker=broker)
        self.addCleanup(runner.close)
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["status"], "awaiting_acceptance", {"terminal": final.get("terminal"), "waiting": final["tasks"]["change"].get("waiting"), "capacity_waits": final.get("capacity_waits"), "attempts": final["tasks"]["change"]["attempts"]})
        self.assertEqual(final["tasks"]["change"]["agent_id"], "other-worker")
        self.assertFalse(broker.connection.execute("SELECT 1 FROM capacity_queue WHERE status='waiting'").fetchone())

    def test_rate_reservation_is_bound_and_replayable_without_a_paid_provider_call(self):
        broker = CapacityBroker(self.state / "shared-capacity.sqlite3")
        self.addCleanup(broker.close)
        now = datetime.now(timezone.utc)
        for kind in ("target", "runtime", "provider"):
            supplied = capacity_fixture.supply(kind, kind, now=now)
            if kind == "provider":
                supplied["rate_limit"]["max_tokens"] = 10000
            broker.publish(supplied)
        plan = capacity_fixture.v6_plan()
        plan["agents"][0]["capacity_pools"]["provider"] = "provider"
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        initial = orchestrator.initialize(plan)
        orchestrator.approve_plan("capacity-run", "human-owner", initial["plan_digest"])
        runner = HarnessRunner(orchestrator, self.source, state_dir=self.state, capacity_broker=broker)
        self.addCleanup(runner.close)
        runner._ensure_evaluator("capacity-run")
        orchestrator.start("capacity-run")
        runner._ensure_admissions("capacity-run")
        assignment = orchestrator.lease_ready_tasks("capacity-run")[0]
        self.assertTrue(orchestrator.start_task("capacity-run", assignment))
        first = runner.capacity.before_turn("capacity-run", assignment, 1)
        second = runner.capacity.before_turn("capacity-run", assignment, 1)
        self.assertEqual(first, second)
        state = orchestrator.state("capacity-run")
        self.assertEqual(len(state["capacity_calls"]), 1)
        self.assertEqual(state["total_tokens"], 0)
        self.assertEqual(project(store.read("capacity-run")), state)
        forged = copy.deepcopy(store.read("capacity-run"))
        event = next(item for item in forged if item["type"] == "CAPACITY_CALL_RESERVED")
        event["payload"]["call"]["max_requests"] = True
        with self.assertRaises(CapacityError):
            project(forged)
        runner.force_interrupt("capacity-run", requested_by="human-owner")
