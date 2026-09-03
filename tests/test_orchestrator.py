import json
import tempfile
import unittest
from pathlib import Path

from camol.orchestrator import Orchestrator, StateTransitionError
from camol.runbook import load_runbook
from camol.store import SQLiteEventStore


ROOT = Path(__file__).resolve().parents[1]


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "events.sqlite3"
        self.store = SQLiteEventStore(self.db_path)
        self.orchestrator = Orchestrator(self.store)
        self.runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        state = self.orchestrator.initialize(self.runbook)
        self.run_id = state["run_id"]
        self.orchestrator.approve_plan(self.run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(self.run_id)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_scheduler_fills_three_boxes_and_waits_on_dependencies(self):
        assignments = self.orchestrator.lease_ready_tasks(self.run_id)
        self.assertEqual(len(assignments), 3)
        self.assertEqual(
            {assignment["task_id"] for assignment in assignments},
            {"frame", "inventory", "challenge"},
        )
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["tasks"]["integrate"]["status"], "pending")

    def test_lease_token_prevents_a_different_agent_from_starting_work(self):
        assignment = self.orchestrator.lease_ready_tasks(self.run_id)[0]
        stolen = dict(assignment, agent_id="builder" if assignment["agent_id"] != "builder" else "verifier")
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, stolen)

    def test_context_packet_compacts_to_checkpoint_and_verifier_delta(self):
        assignment = next(
            item
            for item in self.orchestrator.lease_ready_tasks(self.run_id)
            if item["task_id"] == "frame"
        )
        self.orchestrator.start_task(self.run_id, assignment)
        packet = self.orchestrator.context_packet(self.run_id, assignment)
        self.assertNotIn("transcript", packet)
        self.orchestrator.record_turn(
            self.run_id,
            assignment,
            {
                "status": "continue",
                "checkpoint": "The objective is frozen; exclusions remain to be recorded.",
                "completed_step_ids": [],
                "input_tokens": 300,
                "output_tokens": 120,
            },
        )
        resumed = self.orchestrator.context_packet(self.run_id, assignment)
        self.assertEqual(
            resumed["last_checkpoint"],
            "The objective is frozen; exclusions remain to be recorded.",
        )
        self.assertEqual(resumed["token_budget"]["run_remaining"], 30000 - 420)

    def test_agent_messages_are_routed_through_the_orchestrator(self):
        assignment = next(
            item
            for item in self.orchestrator.lease_ready_tasks(self.run_id)
            if item["task_id"] == "frame"
        )
        self.orchestrator.start_task(self.run_id, assignment)
        self.orchestrator.route_message(
            self.run_id,
            assignment,
            "integrate",
            "proposal",
            "Preserve the frozen plan digest in the integration receipt.",
        )
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["messages"][0]["from_task_id"], "frame")
        self.assertEqual(state["messages"][0]["to_task_id"], "integrate")

    def test_state_replays_after_store_reopen(self):
        self.orchestrator.lease_ready_tasks(self.run_id)
        expected = self.orchestrator.state(self.run_id)
        self.store.close()
        self.store = SQLiteEventStore(self.db_path)
        self.orchestrator = Orchestrator(self.store)
        restored = self.orchestrator.state(self.run_id)
        self.assertEqual(restored["last_seq"], expected["last_seq"])
        self.assertEqual(restored["tasks"], expected["tasks"])

    def test_debug_case_requires_evidence_before_it_can_become_an_eval(self):
        self.orchestrator.open_debug_case(
            self.run_id,
            "case-1",
            "The command exits successfully but does not create the artifact.",
            "A successful command creates the artifact and its verification passes.",
            ["run the pinned command", "inspect the artifact", "run verification"],
            ["command", "artifact", "test_result"],
        )
        self.orchestrator.record_debug_evidence(
            self.run_id, "case-1", "command", {"exit_code": 0}, "builder"
        )
        with self.assertRaisesRegex(StateTransitionError, "missing required evidence"):
            self.orchestrator.verify_debug_case(self.run_id, "case-1", "target observed")
        self.orchestrator.record_debug_evidence(
            self.run_id, "case-1", "artifact", {"sha256": "abc"}, "builder"
        )
        self.orchestrator.record_debug_evidence(
            self.run_id, "case-1", "test_result", {"passed": True}, "verifier"
        )
        self.orchestrator.verify_debug_case(self.run_id, "case-1", "target observed")
        self.orchestrator.promote_eval(
            self.run_id,
            "case-1",
            "artifact-created",
            {"fixture": "fixtures/case-1.json", "oracle": "artifact exists"},
        )
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["debug_cases"]["case-1"]["status"], "eval_promoted")


if __name__ == "__main__":
    unittest.main()
