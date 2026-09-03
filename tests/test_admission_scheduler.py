import asyncio
import copy
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from camol.adapter import ProcessAgentAdapter
from camol.admission import AdmissionController
from camol.orchestrator import Orchestrator, StateTransitionError
from camol.readiness import LeaseFence, WaitingReason
from camol.runbook import load_runbook
from camol.runner import HarnessRunner
from camol.store import SQLiteEventStore
from camol.workspace import WorkspaceManager


ROOT = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class AdmissionSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.state_dir = root / "state"
        (self.source / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", self.source / "examples/fake_agent.py")
        subprocess.run(["git", "-C", str(self.source), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Camol Test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "camol@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-q", "-m", "fixture"], check=True)
        self.clock = MutableClock()
        self.store = SQLiteEventStore(self.state_dir / "events.sqlite3")
        self.orchestrator = Orchestrator(self.store, clock=self.clock, lease_ttl_seconds=30)
        self.runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        state = self.orchestrator.initialize(self.runbook)
        self.run_id = state["run_id"]
        self.orchestrator.approve_plan(self.run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(self.run_id)
        self.workspaces = WorkspaceManager(self.source, self.state_dir)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def prepare(self, run_id=None, task_id="frame", agent_id="strategist"):
        run_id = run_id or self.run_id
        state = self.orchestrator.state(run_id)
        controller = AdmissionController(state["runbook"], self.workspaces, clock=self.clock)
        bundle, handle = controller.prepare(
            plan_digest=state["plan_digest"],
            task=state["tasks"][task_id],
            agent=state["agents"][agent_id],
            granted_by=state["approved_by"],
        )
        return bundle, handle

    def admit(self, run_id=None, task_id="frame", agent_id="strategist"):
        bundle, handle = self.prepare(run_id, task_id, agent_id)
        decision = self.orchestrator.record_admission(run_id or self.run_id, bundle)
        self.assertTrue(decision.ready, [reason.detail for reason in decision.reasons])
        return bundle, handle

    def frame_assignment(self):
        return next(
            item for item in self.orchestrator.lease_ready_tasks(self.run_id)
            if item["task_id"] == "frame"
        )

    def test_green_admission_leases_once_and_binds_every_digest(self):
        bundle, _ = self.admit()
        assignment = self.frame_assignment()
        fence = LeaseFence.from_dict(assignment["fence"])
        self.assertEqual(assignment["fence_digest"], fence.digest())
        self.assertEqual(assignment["admission_digest"], bundle.digest())
        self.assertEqual(
            fence.bound_digests(),
            {
                "plan_digest": self.orchestrator.state(self.run_id)["plan_digest"],
                "box_binding_digest": bundle.binding.digest(),
                "evaluator_digest": bundle.evaluator_digest,
                "workspace_digest": bundle.workspace.digest(),
                "authority_digest": bundle.authority_policy.digest(),
                "probe_policy_digest": bundle.probe_policy.digest(),
                "readiness_digest": bundle.receipt.digest(),
                "grant_digest": bundle.grant.digest(),
                "reservation_digest": bundle.reservation.digest(),
            },
        )
        self.assertTrue(self.orchestrator.start_task(self.run_id, assignment))
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, assignment)
        self.assertEqual(
            len([event for event in self.store.read(self.run_id) if event["type"] == "TASK_STARTED"]),
            1,
        )

    def test_missing_or_wrong_fence_never_starts(self):
        self.admit()
        assignment = self.frame_assignment()
        missing = dict(assignment)
        missing.pop("fence_digest")
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, missing)
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, dict(assignment, fence_digest="sha256:" + "0" * 64))
        self.assertFalse(any(event["type"] == "TASK_STARTED" for event in self.store.read(self.run_id)))

    def test_expiry_between_lease_and_exec_rejects_without_starting(self):
        bundle, _ = self.admit()
        assignment = self.frame_assignment()
        self.clock.advance(31)
        self.assertFalse(self.orchestrator.start_task(self.run_id, assignment))
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["tasks"]["frame"]["status"], "waiting")
        self.assertEqual(state["tasks"]["frame"]["waiting"]["code"], "READINESS_STALE")
        self.assertIn(bundle.reservation.reservation_id, state["released_reservation_ids"])
        self.assertFalse(any(event["type"] == "TASK_STARTED" for event in self.store.read(self.run_id)))

    def test_renewal_fences_out_the_previous_assignment(self):
        self.admit()
        assignment = self.frame_assignment()
        self.assertTrue(self.orchestrator.start_task(self.run_id, assignment))
        self.clock.advance(10)
        renewed = self.orchestrator.renew_lease(self.run_id, assignment)
        self.assertNotEqual(renewed["fence_digest"], assignment["fence_digest"])
        self.assertEqual(renewed["fence"]["epoch"], assignment["fence"]["epoch"])
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.heartbeat(self.run_id, assignment)
        self.orchestrator.heartbeat(self.run_id, renewed)

    def test_revoke_and_reassign_increments_the_fencing_epoch(self):
        first_bundle, _ = self.admit()
        first = self.frame_assignment()
        self.orchestrator.revoke_lease(
            self.run_id,
            first,
            WaitingReason(
                code="OPERATOR_ATTENTION",
                detail="operator requested reassignment",
                wake_condition="new admission is recorded",
                task_id="frame",
                box_id="strategist",
            ),
        )
        self.assertIn(
            first_bundle.reservation.reservation_id,
            self.orchestrator.state(self.run_id)["released_reservation_ids"],
        )
        self.admit()
        second = self.frame_assignment()
        self.assertNotEqual(first["lease_id"], second["lease_id"])
        self.assertEqual(second["fence"]["epoch"], first["fence"]["epoch"] + 1)
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, first)

    def test_operator_cancellation_releases_capacity_and_fences_worker(self):
        bundle, _ = self.admit()
        assignment = self.frame_assignment()
        self.assertTrue(self.orchestrator.start_task(self.run_id, assignment))
        self.orchestrator.cancel_lease(self.run_id, assignment, "test-owner")
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["tasks"]["frame"]["status"], "waiting")
        self.assertIn("cancelled by test-owner", state["tasks"]["frame"]["waiting"]["detail"])
        self.assertIn(bundle.reservation.reservation_id, state["released_reservation_ids"])
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.heartbeat(self.run_id, assignment)

    def test_red_hosted_candidate_waits_and_never_receives_a_lease(self):
        raw = copy.deepcopy(self.runbook)
        raw["run"]["id"] = "hosted-red-admission"
        raw["agents"][0]["adapter"]["argv"] = ["claude"]
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        bundle, _ = self.prepare(run_id, "frame", "strategist")
        decision = self.orchestrator.record_admission(run_id, bundle)
        self.assertFalse(decision.ready)
        self.assertIn("READINESS_STALE", {reason.code for reason in decision.reasons})
        provider = next(probe for probe in bundle.receipt.probes if probe.kind == "provider")
        self.assertEqual(provider.reason_code, "AUTH_REQUIRED")
        assignments = self.orchestrator.lease_ready_tasks(run_id)
        self.assertNotIn("frame", {item["task_id"] for item in assignments})
        projected = self.orchestrator.state(run_id)
        self.assertEqual(projected["tasks"]["frame"]["status"], "waiting")
        self.assertFalse(any(event["type"] == "TASK_STARTED" for event in self.store.read(run_id)))

    def test_capacity_is_reserved_before_lease_and_enforced_by_slots(self):
        raw = copy.deepcopy(self.runbook)
        raw["run"]["id"] = "one-slot-admission"
        raw["run"]["max_concurrency"] = 1
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        first, _ = self.prepare(run_id, "frame", "strategist")
        second, _ = self.prepare(run_id, "inventory", "builder")
        self.assertTrue(self.orchestrator.record_admission(run_id, first).ready)
        with self.assertRaisesRegex(StateTransitionError, "capacity reservation ceiling"):
            self.orchestrator.record_admission(run_id, second)
        assignments = self.orchestrator.lease_ready_tasks(run_id)
        self.assertEqual([item["task_id"] for item in assignments], ["frame"])

    def test_concurrent_capacity_race_commits_only_one_admission(self):
        raw = copy.deepcopy(self.runbook)
        raw["run"]["id"] = "concurrent-one-slot"
        raw["run"]["max_concurrency"] = 1
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        first, _ = self.prepare(run_id, "frame", "strategist")
        second, _ = self.prepare(run_id, "inventory", "builder")
        barrier = threading.Barrier(2)
        database = self.state_dir / "events.sqlite3"

        class BarrierStore(SQLiteEventStore):
            def append_many(inner_self, events, *, expected_seq=None):
                if any(event["type"] == "ADMISSION_RECORDED" for event in events):
                    barrier.wait(timeout=10)
                return super().append_many(events, expected_seq=expected_seq)

        def attempt(bundle):
            store = BarrierStore(database)
            try:
                Orchestrator(store, clock=self.clock).record_admission(run_id, bundle)
                return "committed"
            except StateTransitionError:
                return "raced"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (first, second)))
        self.assertEqual(sorted(outcomes), ["committed", "raced"])
        projected = self.orchestrator.state(run_id)
        self.assertEqual(len(projected["admissions"]), 1)
        self.assertEqual(len(self.orchestrator.active_reservation_ids(run_id)), 1)

    def test_concurrent_schedulers_issue_only_one_fence_for_a_task(self):
        raw = copy.deepcopy(self.runbook)
        raw["run"]["id"] = "concurrent-one-lease"
        raw["run"]["max_concurrency"] = 1
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        first, _ = self.prepare(run_id, "frame", "strategist")
        self.assertTrue(self.orchestrator.record_admission(run_id, first).ready)
        barrier = threading.Barrier(2)
        database = self.state_dir / "events.sqlite3"

        class BarrierStore(SQLiteEventStore):
            def append(inner_self, event, *, expected_seq=None):
                if event["type"] == "TASK_LEASED":
                    barrier.wait(timeout=10)
                return super().append(event, expected_seq=expected_seq)

        def schedule():
            store = BarrierStore(database)
            try:
                return Orchestrator(store, clock=self.clock).lease_ready_tasks(run_id)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            waves = list(pool.map(lambda _: schedule(), range(2)))
        assignments = [assignment for wave in waves for assignment in wave]
        self.assertEqual(len(assignments), 1)
        events = [event for event in self.store.read(run_id) if event["type"] == "TASK_LEASED"]
        self.assertEqual(len(events), 1)

    def test_concurrent_duplicate_start_has_one_winner(self):
        self.admit()
        assignment = self.frame_assignment()
        barrier = threading.Barrier(2)
        database = self.state_dir / "events.sqlite3"

        class BarrierStore(SQLiteEventStore):
            def append(inner_self, event, *, expected_seq=None):
                if event["type"] == "TASK_STARTED":
                    barrier.wait(timeout=10)
                return super().append(event, expected_seq=expected_seq)

        def start():
            store = BarrierStore(database)
            try:
                Orchestrator(store, clock=self.clock).start_task(self.run_id, assignment)
                return "started"
            except StateTransitionError:
                return "fenced"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sorted(outcomes), ["fenced", "started"])
        events = [event for event in self.store.read(self.run_id) if event["type"] == "TASK_STARTED"]
        self.assertEqual(len(events), 1)

    def test_expired_reservation_does_not_consume_capacity_forever(self):
        raw = copy.deepcopy(self.runbook)
        raw["run"]["id"] = "expired-slot-admission"
        raw["run"]["max_concurrency"] = 1
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        first, _ = self.prepare(run_id, "frame", "strategist")
        self.assertTrue(self.orchestrator.record_admission(run_id, first).ready)
        self.clock.advance(301)
        second, _ = self.prepare(run_id, "inventory", "builder")
        self.assertTrue(self.orchestrator.record_admission(run_id, second).ready)

    def test_workspace_change_in_admission_to_exec_gap_makes_zero_agent_calls(self):
        async def scenario():
            runner = HarnessRunner(self.orchestrator, self.source, state_dir=self.state_dir)
            runner.workspaces.prepare_integration(self.run_id)
            runner._ensure_admissions(self.run_id)
            assignment = next(
                item for item in self.orchestrator.lease_ready_tasks(self.run_id)
                if item["task_id"] == "frame"
            )
            handle = runner._handles[("frame", assignment["agent_id"])]
            (handle.path / "changed-after-admission.txt").write_text("changed", encoding="utf-8")
            fake = AsyncMock()
            with patch.object(ProcessAgentAdapter, "execute_turn", fake):
                await runner._execute_assignment(self.run_id, assignment)
            self.assertEqual(fake.await_count, 0)
            state = self.orchestrator.state(self.run_id)
            self.assertEqual(state["tasks"]["frame"]["status"], "waiting")
            self.assertEqual(state["tasks"]["frame"]["waiting"]["code"], "WORKSPACE_CONFLICT")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
