import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from camol.orchestrator import Orchestrator
from camol.adapter import ProcessAgentAdapter
from camol.runner import HarnessRunner
from camol.runbook import load_runbook
from camol.store import SQLiteEventStore


ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def _workspace_and_store(self, temporary):
        workspace = Path(temporary)
        (workspace / "examples").mkdir()
        shutil.copy(ROOT / "examples/fake_agent.py", workspace / "examples/fake_agent.py")
        return workspace, SQLiteEventStore(workspace / ".camol/events.sqlite3")

    def test_long_running_loop_reaches_only_declared_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, store = self._workspace_and_store(temporary)
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
                orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
                final = asyncio.run(
                    HarnessRunner(orchestrator, workspace).run_until_terminal(state["run_id"])
                )
                self.assertEqual(final["status"], "completed")
                self.assertTrue(all(task["status"] == "succeeded" for task in final["tasks"].values()))
                self.assertGreater(final["total_tokens"], 0)
                first_wave_leases = [
                    event
                    for event in store.read(state["run_id"])
                    if event["type"] == "TASK_LEASED"
                ][:3]
                self.assertEqual(len({event["payload"]["agent_id"] for event in first_wave_leases}), 3)
            finally:
                store.close()

    def test_runner_resumes_active_leases_and_reuses_an_unconsumed_result(self):
        async def scenario(workspace, store):
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
            run_id = state["run_id"]
            orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
            orchestrator.start(run_id)
            assignments = orchestrator.lease_ready_tasks(run_id)
            frame = next(item for item in assignments if item["task_id"] == "frame")
            orchestrator.start_task(run_id, frame)
            active_state = orchestrator.state(run_id)
            packet = orchestrator.context_packet(run_id, frame)
            adapter = ProcessAgentAdapter(workspace, run_id)
            await adapter.execute_turn(
                active_state["agents"][frame["agent_id"]], frame, packet, 1
            )
            inventory = next(item for item in assignments if item["task_id"] == "inventory")
            orchestrator.start_task(run_id, inventory)
            orchestrator.record_turn(
                run_id,
                inventory,
                {
                    "status": "continue",
                    "checkpoint": "Inventory boundary identified; command execution remains.",
                    "completed_step_ids": [],
                    "input_tokens": 200,
                    "output_tokens": 80,
                },
            )
            final = await HarnessRunner(orchestrator, workspace).run_until_terminal(run_id)
            self.assertEqual(final["status"], "completed")
            recovered = [
                evidence
                for evidence in final["evidence"].values()
                if evidence.get("task_id") == "frame"
                and evidence["kind"] == "command"
                and evidence["data"].get("recovered_unconsumed_result")
            ]
            self.assertEqual(len(recovered), 1)

        with tempfile.TemporaryDirectory() as temporary:
            workspace, store = self._workspace_and_store(temporary)
            try:
                asyncio.run(scenario(workspace, store))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
