import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from camol.admission import AdmissionController
from camol.orchestrator import Orchestrator, StateTransitionError
from camol.runbook import load_runbook
from camol.store import SQLiteEventStore
from camol.workspace import WorkspaceManager


ROOT = Path(__file__).resolve().parents[1]


class OrchestratorTests(unittest.TestCase):
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
        self.db_path = self.state_dir / "events.sqlite3"
        self.store = SQLiteEventStore(self.db_path)
        self.orchestrator = Orchestrator(self.store)
        self.workspaces = WorkspaceManager(self.source, self.state_dir)
        self.runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        state = self.orchestrator.initialize(self.runbook)
        self.run_id = state["run_id"]
        self.orchestrator.approve_plan(self.run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(self.run_id)

    def admit_ready_tasks(self, run_id=None):
        run_id = run_id or self.run_id
        state = self.orchestrator.state(run_id)
        controller = AdmissionController(state["runbook"], self.workspaces, clock=self.orchestrator.clock)
        available = list(state["agents"].values())
        remaining = state["runbook"]["run"].get(
            "max_concurrency", state["runbook"]["run"].get("max_agents")
        )
        for task in state["tasks"].values():
            if remaining == 0:
                break
            if task["status"] not in {"pending", "waiting"}:
                continue
            if not all(state["tasks"][item]["status"] == "succeeded" for item in task["depends_on"]):
                continue
            eligible = [
                agent for agent in available
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            if not eligible:
                continue
            agent = sorted(eligible, key=lambda item: self.orchestrator._agent_order(item, task))[0]
            bundle, _ = controller.prepare(
                plan_digest=state["plan_digest"], task=task, agent=agent, granted_by=state["approved_by"]
            )
            decision = self.orchestrator.record_admission(run_id, bundle)
            self.assertTrue(decision.ready, [reason.detail for reason in decision.reasons])
            available.remove(agent)
            remaining -= 1

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_scheduler_fills_three_boxes_and_waits_on_dependencies(self):
        self.admit_ready_tasks()
        assignments = self.orchestrator.lease_ready_tasks(self.run_id)
        self.assertEqual(len(assignments), 3)
        self.assertEqual(
            {assignment["task_id"] for assignment in assignments},
            {"frame", "inventory", "challenge"},
        )
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["tasks"]["integrate"]["status"], "pending")

    def test_scheduler_leaves_slots_idle_when_only_one_task_is_ready(self):
        raw = json.loads(
            (ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8")
        )
        raw["run"]["id"] = "one-ready-task-demo"
        raw["tasks"] = raw["tasks"][:1]
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        self.admit_ready_tasks(run_id)

        assignments = self.orchestrator.lease_ready_tasks(run_id)

        self.assertEqual(len(assignments), 1)
        projected = self.orchestrator.state(run_id)
        self.assertEqual(
            sum(agent["status"] == "idle" for agent in projected["agents"].values()),
            2,
        )

    def test_scheduler_can_lease_an_n_worker_pool(self):
        raw = json.loads(
            (ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8")
        )
        raw["run"]["id"] = "four-worker-demo"
        raw["run"]["max_concurrency"] = 4
        fourth_agent = copy.deepcopy(raw["agents"][1])
        fourth_agent.update(id="builder-2", box=".camol/boxes/builder-2")
        raw["agents"].append(fourth_agent)
        fourth_task = copy.deepcopy(raw["tasks"][1])
        fourth_task["id"] = "inventory-2"
        raw["tasks"].insert(3, fourth_task)
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        self.admit_ready_tasks(run_id)

        assignments = self.orchestrator.lease_ready_tasks(run_id)

        self.assertEqual(len(assignments), 4)
        self.assertEqual(len({assignment["agent_id"] for assignment in assignments}), 4)

    def test_scheduler_enforces_concurrency_across_existing_leases(self):
        raw = json.loads(
            (ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8")
        )
        raw["run"]["id"] = "two-concurrent-demo"
        raw["run"]["max_concurrency"] = 2
        state = self.orchestrator.initialize(raw)
        run_id = state["run_id"]
        self.orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(run_id)
        self.admit_ready_tasks(run_id)

        first_wave = self.orchestrator.lease_ready_tasks(run_id)
        second_wave = self.orchestrator.lease_ready_tasks(run_id)

        self.assertEqual(len(first_wave), 2)
        self.assertEqual(second_wave, [])

    def test_lease_token_prevents_a_different_agent_from_starting_work(self):
        self.admit_ready_tasks()
        assignment = self.orchestrator.lease_ready_tasks(self.run_id)[0]
        stolen = dict(assignment, agent_id="builder" if assignment["agent_id"] != "builder" else "verifier")
        with self.assertRaisesRegex(StateTransitionError, "active lease"):
            self.orchestrator.start_task(self.run_id, stolen)

    def test_context_packet_compacts_to_checkpoint_and_verifier_delta(self):
        self.admit_ready_tasks()
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
        self.admit_ready_tasks()
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
        self.admit_ready_tasks()
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
