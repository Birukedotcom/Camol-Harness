import asyncio
import copy
import json
import time
import unittest
from unittest.mock import patch

from camol.debug_execution import source_identity
from camol.orchestrator import Orchestrator, StateTransitionError
from camol.runner import HarnessRunner
from camol.schema import canonical_digest
from camol.source_binding import SourceBindingError, binding_path, load_binding, make_binding, persist_binding
from camol.state import project
from camol.store import SQLiteEventStore
from camol.supervisor import Supervisor, SupervisorError, SupervisorPaths, send_control, send_control_v2, spawn_supervisor
from camol.workspace import WorkspaceError, WorkspaceManager
from unittest.mock import Mock, AsyncMock
from tests import test_evaluation as fixture
from tests.test_gate_runtime import v5_plan


class SourceBindingTests(unittest.TestCase):
    setUp = fixture.EvaluationLoopTests.setUp
    tearDown = fixture.EvaluationLoopTests.tearDown

    def harness(self, run_id="source-bound"):
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(v5_plan(run_id))
        source = source_identity(self.source)
        orchestrator.bind_source(run_id, source)
        orchestrator.approve_plan(run_id, "human-owner", state["plan_digest"])
        runner = HarnessRunner(orchestrator, self.source, state_dir=self.state)
        self.addCleanup(runner.close)
        return orchestrator, store, runner, source

    def change_source(self):
        (self.source / "agent.py").write_text("raise SystemExit('unapproved executable')\n")
        fixture.git(self.source, "add", "agent.py")
        fixture.git(self.source, "commit", "-qm", "unapproved source")

    def test_source_bound_approval_replay_rejects_wrong_plan_and_worker_owner(self):
        orchestrator, store, runner, source = self.harness()
        original = store.read("source-bound")
        for mutation in ({"plan_digest": "sha256:" + "0" * 64}, {"approved_by": "builder"}):
            events = copy.deepcopy(original)
            events[-1]["payload"].update(mutation)
            with self.assertRaisesRegex(ValueError, "human owner"):
                project(events)
        events = copy.deepcopy(original)
        events[-1]["actor_id"] = "builder"
        with self.assertRaisesRegex(ValueError, "human owner"):
            project(events)
        draft = orchestrator.initialize(v5_plan("worker-denied"))
        orchestrator.bind_source("worker-denied", source)
        before = store.read("worker-denied")
        with self.assertRaisesRegex(StateTransitionError, "human owner"):
            orchestrator.approve_plan("worker-denied", "builder", draft["plan_digest"])
        self.assertEqual(store.read("worker-denied"), before)

    def test_spawn_never_accepts_a_different_existing_daemon_as_bound_startup(self):
        source = source_identity(self.source)
        plan_path = self.source.parent / "runbook.json"
        plan_path.write_text(json.dumps(v5_plan("spawn-bound")))
        paths = SupervisorPaths.under(self.state)
        paths.control_dir.mkdir(parents=True)
        paths.socket.touch()
        paths.token.touch()
        child = Mock(pid=999999, poll=Mock(return_value=None))
        original_popen = fixture.subprocess.Popen
        def launch(argv, **kwargs):
            return child if len(argv) > 2 and argv[1:3] == ["-I", "-c"] else original_popen(argv, **kwargs)
        with patch("camol.supervisor.subprocess.Popen", side_effect=launch), patch("camol.supervisor.send_control", AsyncMock(return_value={"ok": True, "pid": 424242})), patch("camol.supervisor.send_control_v2", AsyncMock()) as plan_rpc:
            with self.assertRaisesRegex(SupervisorError, "did not become ready"):
                spawn_supervisor(plan_path, self.source, self.state, expected_source=source, timeout=0.05)
        plan_rpc.assert_not_called()
        child.terminate.assert_called_once()
        with patch("camol.supervisor.subprocess.Popen", side_effect=launch), patch("camol.supervisor.send_control", AsyncMock(return_value={"ok": True, "pid": child.pid})), patch("camol.supervisor.send_control_v2", AsyncMock(return_value={"ok": True, "result": {"run_id": "spawn-bound", "plan_digest": "sha256:" + "0" * 64}})):
            with self.assertRaisesRegex(SupervisorError, "did not become ready"):
                spawn_supervisor(plan_path, self.source, self.state, expected_source=source, timeout=0.05)

    def test_bound_execution_refines_integrates_replays_and_reconstructs_missing_sidecar(self):
        orchestrator, store, runner, source = self.harness()
        final = asyncio.run(runner.run_until_terminal("source-bound"))
        self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
        self.assertEqual(final["source_binding"]["source"], source)
        self.assertEqual(project(store.read("source-bound")), final)
        record = binding_path(self.state, "source-bound")
        record.unlink()
        runner._ensure_source_binding("source-bound")
        self.assertEqual(load_binding(self.state, "source-bound"), final["source_binding"])
        events = store.read("source-bound")
        without_binding = [event for event in events if event["type"] != "SOURCE_BASELINE_BOUND"]
        with self.assertRaises(ValueError):
            project(without_binding)

    def test_changed_clean_source_is_denied_before_admission_lease_or_worker_launch(self):
        orchestrator, store, runner, source = self.harness()
        self.change_source()
        with self.assertRaises(WorkspaceError):
            asyncio.run(runner.run_until_terminal("source-bound"))
        state = orchestrator.state("source-bound")
        self.assertFalse(state["admissions"])
        self.assertFalse(state["evidence"])
        self.assertEqual(state["total_tokens"], 0)
        self.assertFalse(any(event["type"] == "TASK_LEASED" for event in store.read("source-bound")))

    def test_worktree_materialization_never_follows_changed_head_after_check(self):
        orchestrator, _, runner, source = self.harness()
        runner._ensure_source_binding("source-bound")
        manager = runner.workspaces
        original = manager._assert_bound_source
        triggered = []
        def changed_after_check(run_id):
            result = original(run_id)
            if not triggered:
                triggered.append(True)
                self.change_source()
            return result
        with patch.object(manager, "_assert_bound_source", side_effect=changed_after_check):
            with self.assertRaises(WorkspaceError):
                manager.prepare_task("source-bound", "change", "builder")
        for path in (self.state / "worktrees").rglob("agent.py"):
            self.assertNotIn("unapproved executable", path.read_text())
        self.assertFalse(orchestrator.state("source-bound")["admissions"])

    def test_successor_explicitly_inherits_source_and_has_independent_sidecar(self):
        orchestrator, store, runner, source = self.harness()
        proposal = orchestrator.propose_revision("source-bound", v5_plan("successor"), "Reviewed contract amendment")
        successor = orchestrator.apply_revision("source-bound", proposal["proposal_digest"], "human-owner")
        self.assertEqual(successor["source_binding"]["source"], source)
        self.assertEqual(successor["source_binding"]["run_id"], "successor")
        runner._ensure_source_binding("successor")
        self.assertEqual(load_binding(self.state, "successor"), successor["source_binding"])
        self.assertEqual(project(store.read("successor")), successor)

    def test_missing_or_malformed_required_launch_binding_fails_before_approval(self):
        plan_path = self.source.parent / "runbook.json"
        plan_path.write_text(json.dumps(v5_plan("required-source")))
        for supplied in ("sha256:" + "0" * 64, "invalid"):
            supervisor = Supervisor(plan_path, self.source, self.state, approve_by="human-owner", source_binding_digest=supplied)
            with self.assertRaisesRegex(SupervisorError, "missing or changed"):
                asyncio.run(supervisor.serve())
        store = SQLiteEventStore(SupervisorPaths.under(self.state).database)
        try:
            state = project(store.read("required-source"))
        finally:
            store.close()
        self.assertIsNone(state["approved_by"])
        self.assertFalse(state["admissions"])

    def test_actual_detached_launch_persists_hidden_source_flag_and_restart_enforces_event(self):
        sentinel = self.source.parent / "daemon-import-sentinel"
        malicious = "from pathlib import Path; Path({!r}).write_text('source executed')\n".format(str(sentinel))
        (self.source / "camol").mkdir()
        (self.source / "camol" / "__init__.py").write_text(malicious)
        (self.source / "camol" / "__main__.py").write_text(malicious)
        (self.source / "sitecustomize.py").write_text(malicious)
        fixture.git(self.source, "add", ".")
        fixture.git(self.source, "commit", "-qm", "daemon import traps")
        source = source_identity(self.source)
        plan_path = self.source.parent / "runbook.json"
        plan_path.write_text(json.dumps(v5_plan("spawn-bound")))
        launched = spawn_supervisor(plan_path, self.source, self.state, expected_source=source)
        self.assertTrue(launched["started"])
        self.assertFalse(sentinel.exists(), "authoritative daemon must not import source modules or sitecustomize")
        try:
            plan = asyncio.run(send_control_v2(self.state, "plan"))["result"]
            self.assertEqual(plan["source_binding"]["source"], source)
            self.assertTrue(asyncio.run(send_control(self.state, "drain"))["ok"])
            self.assertTrue(asyncio.run(send_control(self.state, "approve", requested_by="human-owner"))["ok"])
        finally:
            asyncio.run(send_control(self.state, "stop", requested_by="human-owner"))
        paths = SupervisorPaths.under(self.state)
        deadline = time.monotonic() + 5
        while paths.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(paths.socket.exists())
        binding_path(self.state, "spawn-bound").unlink()
        self.change_source()
        with self.assertRaises(SourceBindingError):
            asyncio.run(Supervisor(plan_path, self.source, self.state).serve())
        store = SQLiteEventStore(SupervisorPaths.under(self.state).database)
        try:
            events = store.read("spawn-bound")
        finally:
            store.close()
        approved = next(event for event in events if event["type"] == "PLAN_APPROVED")
        bound = next(event for event in events if event["type"] == "SOURCE_BASELINE_BOUND")
        self.assertEqual(approved["payload"]["source_binding_digest"], canonical_digest(bound["payload"]))
        self.assertLess(bound["seq"], approved["seq"])
        self.assertFalse(any(event["type"] == "TASK_LEASED" for event in events))
