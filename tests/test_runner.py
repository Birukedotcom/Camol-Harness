import asyncio
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

from camol.orchestrator import Orchestrator
from camol.adapter import ProcessAgentAdapter
from camol.artifacts import ArtifactError, RunArchive
from camol.runner import HarnessRunner
from camol.runbook import load_runbook
from camol.store import SQLiteEventStore
from camol.sandbox import select_backend


ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def _workspace_and_store(self, temporary):
        root = Path(temporary)
        workspace = root / "source"
        state_dir = root / "state"
        (workspace / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", workspace / "examples/fake_agent.py")
        subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Camol Test"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.email", "camol@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "fixture"], check=True)
        return workspace, state_dir, SQLiteEventStore(state_dir / "events.sqlite3")

    def test_long_running_loop_reaches_only_declared_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, state_dir, store = self._workspace_and_store(temporary)
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
                orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
                final = asyncio.run(
                    HarnessRunner(orchestrator, workspace, state_dir=state_dir).run_until_terminal(state["run_id"])
                )
                self.assertEqual(final["status"], "completed", (final.get("terminal"), final["tasks"]))
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

    def test_shipped_local_n_box_example_completes_with_repository_ignores(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, state_dir, store = self._workspace_and_store(temporary)
            shutil.copy(ROOT / ".gitignore", workspace / ".gitignore")
            subprocess.run(["git", "-C", str(workspace), "add", ".gitignore"], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "commit", "-q", "-m", "repository ignores"],
                check=True,
            )
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(
                    load_runbook(ROOT / "examples/local-n-box-runbook.json")
                )
                orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
                final = asyncio.run(
                    HarnessRunner(orchestrator, workspace, state_dir=state_dir).run_until_terminal(
                        state["run_id"]
                    )
                )
                self.assertEqual(final["status"], "completed", (final.get("terminal"), final["tasks"]))
                self.assertTrue(all(task["status"] == "succeeded" for task in final["tasks"].values()))
                first_wave_leases = [
                    event for event in store.read(state["run_id"]) if event["type"] == "TASK_LEASED"
                ][:3]
                self.assertEqual(len({event["payload"]["agent_id"] for event in first_wave_leases}), 3)
            finally:
                store.close()

    def test_runner_resumes_active_leases_and_reuses_an_unconsumed_result(self):
        async def scenario(workspace, state_dir, store):
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
            run_id = state["run_id"]
            orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
            orchestrator.start(run_id)
            runner = HarnessRunner(orchestrator, workspace, state_dir=state_dir)
            runner.workspaces.prepare_integration(run_id)
            runner._ensure_admissions(run_id)
            assignments = orchestrator.lease_ready_tasks(run_id)
            frame = next(item for item in assignments if item["task_id"] == "frame")
            orchestrator.start_task(run_id, frame)
            active_state = orchestrator.state(run_id)
            packet = orchestrator.context_packet(run_id, frame)
            execution_workspace = runner._execution_workspace(run_id, frame)
            bundle = runner._admission_for(active_state, frame)
            adapter = ProcessAgentAdapter(
                execution_workspace,
                run_id,
                state_dir=state_dir,
                sandbox_backend=select_backend(bundle.sandbox_policy),
                sandbox_policy=bundle.sandbox_policy,
            )
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
            final = await runner.run_until_terminal(run_id)
            self.assertEqual(final["status"], "completed", (final.get("terminal"), final["tasks"]))
            recovered = [
                evidence
                for evidence in final["evidence"].values()
                if evidence.get("task_id") == "frame"
                and evidence["kind"] == "command"
                and evidence["data"].get("recovered_unconsumed_result")
            ]
            self.assertEqual(len(recovered), 1)

        with tempfile.TemporaryDirectory() as temporary:
            workspace, state_dir, store = self._workspace_and_store(temporary)
            try:
                asyncio.run(scenario(workspace, state_dir, store))
            finally:
                store.close()

    def test_completed_run_exports_and_replays_without_terminal_scrollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, state_dir, store = self._workspace_and_store(temporary)
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
                orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
                runner = HarnessRunner(orchestrator, workspace, state_dir=state_dir)
                final = asyncio.run(runner.run_until_terminal(state["run_id"]))
                self.assertEqual(final["status"], "completed")
                archive = Path(temporary) / "export"
                manifest = RunArchive.export(state["run_id"], store.read(state["run_id"]), runner.artifacts, archive)
                verified, events = RunArchive.verify(archive)
                self.assertEqual(verified, manifest)
                replayed = RunArchive.replay(archive)
                self.assertEqual(replayed["status"], "completed")
                self.assertEqual(replayed["tasks"], final["tasks"])
                self.assertEqual(replayed["evidence"], final["evidence"])
                self.assertGreater(len(manifest["artifact_digests"]), 0)
                first = manifest["artifact_digests"][0].split(":", 1)[1]
                blob = archive / "blobs" / "sha256" / first[:2] / first[2:]
                blob.write_bytes(blob.read_bytes() + b"corrupt")
                with self.assertRaisesRegex(ArtifactError, "corrupt"):
                    RunArchive.verify(archive)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
