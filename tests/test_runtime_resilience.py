"""Adversarial long-turn, recovery, and scheduler regression fixtures."""

import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from camol.leases import effective_expiry
from camol.orchestrator import StateTransitionError
from camol.runner import HarnessRunner
from camol.state import project
from camol.usage import provider_cost_used
from camol.admission import AdmissionController
from tests import test_admission_scheduler as fixtures
from tests import test_evaluation as evaluation_fixtures
from tests.test_gate_runtime import v5_plan


class RuntimeResilienceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdmissionSchedulerTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def active(self):
        f = self.fixture
        f.admit()
        assignment = f.frame_assignment()
        f.orchestrator.start_task(f.run_id, assignment)
        return assignment

    def test_fresh_reproof_survives_original_receipt_expiry_and_replays(self):
        f = self.fixture
        assignment = self.active()
        original_packet = f.orchestrator.context_packet(f.run_id, assignment)
        for _ in range(17):
            f.clock.advance(20)
            bundle, _ = f.prepare()
            f.orchestrator.refresh_active_lease(f.run_id, assignment, bundle)
            f.orchestrator.heartbeat(f.run_id, assignment)
        state = f.orchestrator.state(f.run_id)
        self.assertEqual(state["tasks"]["frame"]["fence_digest"], assignment["fence_digest"])
        self.assertEqual(f.orchestrator.context_packet(f.run_id, assignment)["lease"], original_packet["lease"])
        self.assertGreater(effective_expiry(state, state["tasks"]["frame"]), assignment["fence"]["expires_at"])
        self.assertEqual(len(f.orchestrator.active_reservation_ids(f.run_id)), 1)
        self.assertEqual(project(f.store.read(f.run_id)), state)

    def test_legacy_packet_directory_write_authority_cannot_resume_a_worker(self):
        f = self.fixture
        original = AdmissionController._sandbox_policy
        def legacy(controller, handle, task, agent, **kwargs):
            policy = original(controller, handle, task, agent, **kwargs)
            output = next(value for value in policy.write_paths if value.endswith("worker-output"))
            return replace(policy, write_paths=tuple(value for value in policy.write_paths if value != output) + (str(Path(output).parent),))
        with patch.object(AdmissionController, "_sandbox_policy", legacy):
            f.admit()
        assignment = f.frame_assignment()
        runner = HarnessRunner(f.orchestrator, f.source, state_dir=f.state_dir)
        async def forbidden(*args, **kwargs):
            self.fail("legacy broad write grant launched a worker")
        with patch("camol.adapter.ProcessAgentAdapter.execute_turn", forbidden):
            asyncio.run(runner._run_assignment(f.run_id, assignment))
        state = f.orchestrator.state(f.run_id)
        self.assertEqual(state["tasks"]["frame"]["status"], "waiting")
        self.assertEqual(state["tasks"]["frame"]["waiting"]["code"], "POLICY_DENIED")
        self.assertEqual(state["total_tokens"], 0)

    def test_reproof_rejects_authority_expansion_and_expired_lease(self):
        f = self.fixture
        assignment = self.active()
        f.clock.advance(20)
        bundle, _ = f.prepare()
        altered = replace(bundle, sandbox_policy=replace(bundle.sandbox_policy, network_destinations=("*",)))
        with self.assertRaisesRegex(StateTransitionError, "changed sandbox_policy"):
            f.orchestrator.refresh_active_lease(f.run_id, assignment, altered)
        f.clock.advance(11)
        with self.assertRaisesRegex(StateTransitionError, "expired"):
            f.orchestrator.refresh_active_lease(f.run_id, assignment, bundle)

    def test_replay_rejects_extended_expiry_and_wrong_fence(self):
        f = self.fixture
        assignment = self.active()
        f.clock.advance(20)
        bundle, _ = f.prepare()
        f.orchestrator.refresh_active_lease(f.run_id, assignment, bundle)
        events = f.store.read(f.run_id)
        altered = copy.deepcopy(events)
        altered[-1]["payload"]["expires_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "outlives"):
            project(altered)
        altered = copy.deepcopy(events)
        altered[-1]["payload"]["fence_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "active fenced"):
            project(altered)

    def test_expired_turn_is_salvaged_and_visible_instead_of_stranding_running(self):
        f = self.fixture
        f.admit()
        assignment = f.frame_assignment()
        runner = HarnessRunner(f.orchestrator, f.source, state_dir=f.state_dir)

        async def expired_result(*args, **kwargs):
            f.clock.advance(31)
            return {"status": "continue", "checkpoint": "completed late", "completed_step_ids": [],
                    "input_tokens": 1, "output_tokens": 1, "evidence": [],
                    "_camol_observed_evidence": [{
                        "kind": "model_usage", "data": {"cost_usd_micros": 1234},
                        "producer": "adapter", "epistemic_status": "OBSERVED", "artifact_refs": [],
                    }]}

        with patch("camol.adapter.ProcessAgentAdapter.execute_turn", expired_result):
            asyncio.run(runner._run_assignment(f.run_id, assignment))
        state = f.orchestrator.state(f.run_id)
        self.assertEqual(state["tasks"]["frame"]["status"], "waiting")
        self.assertEqual(state["tasks"]["frame"]["waiting"]["code"], "READINESS_STALE")
        self.assertEqual(len(state["salvages"]), 1)
        self.assertEqual(f.orchestrator.active_reservation_ids(f.run_id), [])
        self.assertEqual(provider_cost_used(state), 1234)
        self.assertEqual(state["total_tokens"], 0)

    def test_verifier_restart_reloads_worktree_and_uses_current_lease(self):
        f = self.fixture
        assignment = self.active()
        handle = f.workspaces.prepare_task(f.run_id, "frame", "strategist")
        # Use the actual runbook path; this fixture predates camol-boxes.
        state = f.orchestrator.state(f.run_id)
        box = handle.path / state["agents"]["strategist"]["box"]
        box.mkdir(parents=True)
        (box / "contract.txt").write_text("objective exclusions evidence completion")
        f.orchestrator.record_turn(f.run_id, assignment, {
            "status": "complete", "checkpoint": "candidate ready", "completed_step_ids": ["write-contract"],
            "input_tokens": 1, "output_tokens": 1,
        })
        f.orchestrator.submit_task(f.run_id, assignment, "candidate ready")
        runner = HarnessRunner(f.orchestrator, f.source, state_dir=f.state_dir)
        runner._ensure_evaluator(f.run_id)
        # No in-memory handles survive this new runner instance.
        self.assertEqual(runner._handles, {})
        asyncio.run(runner._resume_verification(f.run_id, assignment))
        state = f.orchestrator.state(f.run_id)
        self.assertTrue(state["tasks"]["frame"]["verification_history"])
        verification = state["tasks"]["frame"]["verification_history"][-1]
        self.assertEqual(verification["lease_id"], assignment["lease_id"])
        self.assertEqual(verification["fence_digest"], assignment["fence_digest"])

    def test_long_operation_refreshes_repeatedly_without_changing_packet_identity(self):
        f = self.fixture
        assignment = self.active()
        runner = HarnessRunner(f.orchestrator, f.source, state_dir=f.state_dir)
        calls = []

        async def run():
            complete = asyncio.Event()
            actual_wait = asyncio.wait
            ticks = []

            async def long_operation(run_id, current):
                await complete.wait()
                calls.append(current["fence_digest"])

            async def accelerated_wait(pending, **kwargs):
                ticks.append(True)
                if len(ticks) == 4:
                    complete.set()
                    return await actual_wait(pending)
                f.clock.advance(20)
                return await actual_wait(pending, timeout=0.001)

            with patch.object(runner, "_execute_assignment", long_operation), patch("camol.runner.asyncio.wait", accelerated_wait):
                await runner._run_assignment(f.run_id, assignment)

        asyncio.run(run())
        state = f.orchestrator.state(f.run_id)
        self.assertEqual(calls, [assignment["fence_digest"]])
        self.assertEqual(len([event for event in f.store.read(f.run_id) if event["type"] == "LEASE_AUTHORIZATION_REFRESHED"]), 3)


class HumanGateRecoveryTests(unittest.TestCase):
    setUp = evaluation_fixtures.EvaluationLoopTests.setUp
    tearDown = evaluation_fixtures.EvaluationLoopTests.tearDown

    def test_two_expired_human_gates_resume_same_candidate_without_worker_turns(self):
        clock = fixtures.MutableClock()
        clock.value = datetime.now(timezone.utc)
        store = fixtures.SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = fixtures.Orchestrator(store, clock=clock, lease_ttl_seconds=30)
        state = orchestrator.initialize(v5_plan("human-expired-recovery", human=True))
        run_id = state["run_id"]
        orchestrator.approve_plan(run_id, "human-owner", state["plan_digest"])

        def drive():
            return asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal(run_id))

        state = drive()
        attempts, turns = state["tasks"]["change"]["attempts"], state["tasks"]["change"]["turn_count"]
        candidate_id = state["tasks"]["change"]["gate_wait"]["candidate_id"]
        revisions = []
        for phase in ("candidate", "integration"):
            waiting = state["tasks"]["change"]["gate_wait"]
            self.assertEqual(waiting["phase"], phase)
            if phase == "integration":
                revisions.append(state["gate_assessments"][candidate_id + ":integration"]["integration_receipt"]["revision"])
            clock.advance(301)
            orchestrator.approve_task_gate(run_id, "change", "human-owner", waiting["assessment_digest"])
            with patch("camol.adapter.ProcessAgentAdapter.execute_turn", side_effect=AssertionError("human review must not rerun worker")):
                state = drive()
        self.assertEqual(state["status"], "awaiting_acceptance", state.get("terminal"))
        self.assertEqual(state["tasks"]["change"]["attempts"], attempts)
        self.assertEqual(state["tasks"]["change"]["turn_count"], turns)
        self.assertEqual(state["integrations"][0]["candidate_id"], candidate_id)
        self.assertEqual(state["integrations"][0]["revision"], revisions[0])
        self.assertEqual(len(state["lease_authorizations"]), 1)
        self.assertEqual(project(store.read(run_id)), state)


if __name__ == "__main__":
    unittest.main()
