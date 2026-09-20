import asyncio
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camol.app import InteractiveController, InteractiveError
from camol.artifacts import ArtifactStore
from camol.box_inspection import BoxInspector, BoxInspectionError, box_snapshot, read_artifact_preview
from camol.cli import main
from camol.orchestrator import Orchestrator
from camol.runbook import load_runbook
from camol.runner import HarnessRunner
from camol.schema import canonical_digest
from camol.store import SQLiteEventStore
from camol.supervisor import SupervisorError
from tests import test_runner


ROOT = Path(__file__).resolve().parents[1]


class BoxInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state_dir = self.root / "state"
        self.database = self.state_dir / "camol.sqlite3"
        self.store = SQLiteEventStore(self.database)
        self.addCleanup(lambda: self.store.close() if self.store else None)
        self.orchestrator = Orchestrator(self.store)
        self.document = load_runbook(ROOT / "examples/local-n-box-runbook.json")
        self.state = self.orchestrator.initialize(self.document)
        self.run_id = self.state["run_id"]
        self.worker = self.document["agents"][0]["id"]
        self.inspector = BoxInspector(self.state_dir)

    def close(self):
        self.store.close()
        self.store = None

    def assertDigest(self, report):
        self.assertEqual(report["snapshot_digest"], canonical_digest({key: value for key, value in report.items() if key != "snapshot_digest"}))

    def test_live_then_cold_exact_run_and_no_initialization(self):
        before = self.inspector.list(self.run_id)
        self.assertFalse(before["live_connection_proven"])
        self.orchestrator.approve_plan(self.run_id, "owner", self.state["plan_digest"])
        live = self.inspector.list(self.run_id)
        self.assertGreater(live["event_cursor"], before["event_cursor"])
        other = copy.deepcopy(self.document)
        other["run"]["id"] = "other-run"
        self.orchestrator.initialize(other)
        self.assertEqual(self.inspector.list(self.run_id), live)
        self.close()
        files = sorted(str(path.relative_to(self.state_dir)) for path in self.state_dir.rglob("*"))
        self.assertEqual(self.inspector.list(self.run_id), live)
        self.assertEqual(files, sorted(str(path.relative_to(self.state_dir)) for path in self.state_dir.rglob("*")))
        self.assertFalse((self.state_dir / "artifacts").exists())
        for report in (live, self.inspector.resolve(self.run_id, self.worker), self.inspector.read(self.run_id, self.worker)):
            self.assertDigest(report)
        with self.assertRaises(BoxInspectionError):
            self.inspector.list("missing")
        for box in ("1", self.worker[:-1], "absent"):
            with self.assertRaises(BoxInspectionError):
                self.inspector.resolve(self.run_id, box)

    def test_absent_state_is_not_created(self):
        absent = self.root / "absent"
        with self.assertRaises(BoxInspectionError):
            BoxInspector(absent).read(self.run_id, self.worker)
        self.assertFalse(absent.exists())

    def test_linked_special_and_over_limit_ledgers_are_rejected(self):
        self.orchestrator.approve_plan(self.run_id, "owner", self.state["plan_digest"])
        for options in ({"max_events": 1}, {"max_event_bytes": 1}, {"max_total_bytes": 1}):
            with self.assertRaises(BoxInspectionError):
                BoxInspector(self.state_dir, **options).list(self.run_id)
        with self.assertRaises(BoxInspectionError):
            BoxInspector(Path("relative"))
        with self.assertRaises(BoxInspectionError):
            BoxInspector(self.state_dir, database=self.root / "outside")
        self.close()
        linked = self.root / "linked"
        linked.symlink_to(self.state_dir, target_is_directory=True)
        with self.assertRaises(BoxInspectionError):
            BoxInspector(linked)
        target = self.state_dir / "saved.sqlite3"
        self.database.rename(target)
        self.database.symlink_to(target)
        with self.assertRaises(BoxInspectionError):
            self.inspector.list(self.run_id)
        self.database.unlink()
        os.mkfifo(self.database)
        with self.assertRaises(BoxInspectionError):
            self.inspector.list(self.run_id)

    def test_invalid_payload_and_metadata_are_not_replayed(self):
        for column, value in (("payload_json", '{"x": 1, "x": 2}'), ("actor_id", "bad\x1bactor"), ("occurred_at", "yesterday")):
            original = self.store.connection.execute("SELECT " + column + " FROM events WHERE seq=1").fetchone()[0]
            self.store.connection.execute("UPDATE events SET " + column + "=? WHERE seq=1", (value,))
            self.store.connection.commit()
            with self.assertRaises(BoxInspectionError):
                self.inspector.read(self.run_id, self.worker)
            self.store.connection.execute("UPDATE events SET " + column + "=? WHERE seq=1", (original,))
            self.store.connection.commit()

    def test_cursor_and_preview_option_types(self):
        for options in ({"after_seq": True}, {"after_seq": -1}, {"limit": 0}, {"limit": 1001}, {"tail": 1}, {"previews": 1}):
            with self.assertRaises(BoxInspectionError):
                self.inspector.read(self.run_id, self.worker, **options)

    def test_assignment_history_not_actor_name_or_other_workers(self):
        document = copy.deepcopy(self.document)
        document["agents"][0]["id"] = "orchestrator"
        task = document["tasks"][0]["id"]
        state = dict(self.state, tasks={task: dict(agent_id="different", status="running")})
        events = [
            dict(seq=1, type="TASK_LEASED", actor_id="owner", payload=dict(task_id=task, agent_id="orchestrator")),
            dict(seq=2, type="NOTE", actor_id="orchestrator", payload=dict(task_id=task)),
            dict(seq=3, type="NOTE", actor_id="orchestrator", payload=dict(unrelated=True)),
            dict(seq=4, type="TASK_LEASED", actor_id="owner", payload=dict(task_id=task, agent_id="different")),
            dict(seq=5, type="NOTE", actor_id="orchestrator", payload=dict(task_id=task)),
        ]
        report = box_snapshot(self.run_id, document, state, events, "orchestrator", after_seq=1)
        self.assertEqual([event["seq"] for event in report["events"]], [2])
        self.assertEqual(report["observation"]["historical_task_ids"], [task])
        self.assertEqual(report["task_ids"], [])
        tail = box_snapshot(self.run_id, document, state, events, "orchestrator", limit=1, tail=True)
        self.assertEqual([event["seq"] for event in tail["events"]], [2])
        self.assertTrue(tail["observation"]["more_events"])

    def test_bounded_redacted_hash_verified_previews(self):
        task = self.document["tasks"][0]["id"]
        producer = dict(run_id=self.run_id, task_id=task, agent_id=self.worker, lease_id="lease-1", channel="stdout", role="adapter")
        artifacts = ArtifactStore(self.state_dir)
        reference = artifacts.put_bytes(b"\x1b" * 5000, producer=producer, redact=True)
        events = [dict(seq=1, type="EVIDENCE_RECORDED", payload=dict(task_id=task, agent_id=self.worker, artifact=reference.to_dict()))]
        def snapshot():
            return box_snapshot(self.run_id, self.document, self.state, events, self.worker,
                preview_reader=lambda ref: read_artifact_preview(self.state_dir, ref, [8 << 20]))["artifacts"][reference.digest]
        preview = snapshot()
        self.assertNotIn("\x1b", preview["preview"])
        self.assertTrue(preview["preview_truncated"])
        artifacts.path_for(reference.digest).write_bytes(b"changed")
        self.assertIn("error", snapshot())
        events[0]["payload"]["artifact"]["redacted"] = False
        self.assertIn("raw artifact withheld", snapshot()["error"])
        events[0]["payload"]["artifact"]["redacted"] = True
        events[0]["payload"]["artifact"]["producer"]["run_id"] = "other-run"
        self.assertIn("outside", snapshot()["error"])

    def test_preview_does_not_follow_symlinks_or_open_special_files(self):
        producer = dict(run_id=self.run_id, task_id=self.document["tasks"][0]["id"], agent_id=self.worker, channel="stdout")
        artifacts = ArtifactStore(self.state_dir)
        reference = artifacts.put_bytes(b"safe", producer=producer, redact=True)
        path = artifacts.path_for(reference.digest)
        outside = self.root / "outside"
        outside.write_bytes(b"safe")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            read_artifact_preview(self.state_dir, reference, [8 << 20])
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            read_artifact_preview(self.state_dir, reference, [8 << 20])

    def test_cli_exact_read_and_errors(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["box", "read", self.worker, "--state-dir", str(self.state_dir), "--run-id", self.run_id, "--no-previews"])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue()), self.inspector.read(self.run_id, self.worker, previews=False))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["box", "resolve", "1", "--state-dir", str(self.state_dir), "--run-id", self.run_id]), 2)

    def test_fresh_controller_offline_and_corruption_never_uses_old_cache(self):
        workspace = self.root / "repo"
        workspace.mkdir()
        controller = InteractiveController(workspace, state_root=self.root / "interactive")
        plan = dict(schema="camol.product_plan", schema_version=2, proposal=None,
            run_id=self.run_id, runbook=self.document, execution_status="ready", execution_limitation="fixture",
            source=dict(workspace=str(workspace), revision="a" * 40))
        controller.session = controller.store.update(controller.session, plan=plan, plan_digest=canonical_digest(plan),
            run_id=self.run_id, state_dir=str(self.state_dir))
        fresh = InteractiveController(workspace, state_root=self.root / "interactive")
        with patch.object(fresh, "_control", side_effect=SupervisorError("offline")):
            self.assertIn("RETAINED", fresh.inspect_box(self.worker))
            self.assertTrue(all(box["basis"] == "retained_ledger" for box in fresh.box_summaries()))
            self.close()
            self.database.write_bytes(b"not sqlite")
            self.assertIn("invalid or mismatched", fresh.inspect_box(self.worker))
            self.assertTrue(all(box["status"] == "unavailable" for box in fresh.box_summaries()))

    def test_client_cache_cannot_cross_selected_run_or_plan(self):
        workspace = self.root / "repo"
        workspace.mkdir()
        controller = InteractiveController(workspace, state_root=self.root / "interactive")
        view = self.inspector.read(self.run_id, self.worker)
        controller.session = controller.store.update(controller.session, run_id=self.run_id)
        with patch.object(controller, "_control", return_value=view):
            self.assertIn("Read-only snapshot", controller.inspect_box(self.worker))
        controller.session = controller.store.update(controller.session, run_id="new-run")
        with patch.object(controller, "_control", side_effect=SupervisorError("offline")):
            self.assertNotIn("STALE", controller.inspect_box(self.worker))
        with patch.object(controller, "_control", return_value=view):
            with self.assertRaisesRegex(InteractiveError, "selected run"):
                controller.inspect_box(self.worker)

    def test_real_local_build_retains_artifacts_after_runner_and_store_close(self):
        workspace, state_dir, store = test_runner.RunnerTests()._workspace_and_store(self.root / "build")
        runner = None
        try:
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(self.document)
            orchestrator.approve_plan(state["run_id"], "owner", state["plan_digest"])
            runner = HarnessRunner(orchestrator, workspace, state_dir=state_dir)
            final = asyncio.run(runner.run_until_terminal(state["run_id"]))
            self.assertEqual(final["status"], "completed", final.get("terminal"))
        finally:
            if runner:
                runner.close()
            store.close()
        inspector = BoxInspector(state_dir.resolve(), database=(state_dir / "events.sqlite3").resolve())
        report = inspector.read(state["run_id"], self.worker, limit=1000)
        self.assertTrue(report["workspaces"])
        self.assertTrue(any("preview" in value for value in report["artifacts"].values()), report["artifacts"])
        self.assertTrue(all(item["run_id"] == self.run_id for item in report["workspaces"]))
        self.assertFalse(report["observation"]["live_connection_proven"])
