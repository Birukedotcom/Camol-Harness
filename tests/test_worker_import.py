import copy
import contextlib
import io
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from camol.artifacts import ArtifactStore, RunArchive, artifact_refs
from camol.box_inspection import BoxInspector
from camol.cli import main
from camol.orchestrator import Orchestrator
from camol.schema import canonical_digest
from camol.state import project
from camol.store import ConcurrentAppendError
from camol.worker_delivery import DeliveryError
from camol.worker_enrollment import WorkerEnrollment
from tests import test_worker_enrollment as fixture_module
from tests import test_worker_tls as tls_fixture
from camol.worker_tls import WorkerTLSClient


class WorkerImportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.WorkerEnrollmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.value = self.fixture.approved()
        self.service = self.fixture.service
        self.scope = self.value["scope"]
        self.producer = self.fixture.producer(self.value)
        self.orch = self.fixture.orch
        self.run_id = self.fixture.fixture.run_id
        self.state_dir = self.fixture.fixture.state_dir

    def deliver(self, count=1, data=None):
        for i in range(count):
            self.producer.queue("tool", data if data is not None else {"message": "worker-claim-" + str(i)}, occurred_at=self.orch._now())
        while self.producer.inspect()["unacknowledged"]:
            self.producer.acknowledge(self.service.receive(self.scope, self.producer.batch()))

    def capture(self, request_id="capture-1", limit=6):
        return self.service.import_received(self.scope, by="test-owner", request_id=request_id, limit=limit)

    def test_paged_import_restart_box_history_and_export_without_success_or_bill(self):
        self.deliver(7, {"claimed_status": "succeeded", "claimed_tokens": 99999,
                         "nested": {"schema": "camol.artifact_ref", "path": "/outside-untrusted"}})
        before = self.orch.state(self.run_id)
        first = self.capture(limit=4)
        self.assertEqual((first["cursor"], first["imported_count"], first["more"]), (4, 4, True))
        self.assertEqual(self.capture(limit=4), first)
        second = self.capture("capture-2", 4)
        self.assertEqual((second["cursor"], second["imported_count"], second["more"]), (7, 3, False))
        after = self.orch.state(self.run_id)
        for key in ("tasks", "agents", "evidence", "total_tokens", "heartbeats", "lease_authorizations", "gate_assessments"):
            self.assertEqual(after[key], before[key], key)
        restarted = WorkerEnrollment(Orchestrator(self.fixture.fixture.store, clock=self.fixture.fixture.clock), self.run_id, self.state_dir)
        self.assertEqual(restarted.import_received(self.scope, by="test-owner", request_id="capture-2", limit=4), second)
        rows = restarted.records(self.scope, after=3, limit=2)
        self.assertEqual([row["seq"] for row in rows["records"]], [4, 5])
        self.assertTrue(rows["more"])
        events = self.fixture.fixture.store.read(self.run_id)
        self.assertEqual(project(events), after)
        imported = [event for event in events if event["type"] == "WORKER_STREAM_IMPORTED"]
        self.assertEqual(len(imported), 2)
        self.assertEqual(artifact_refs(imported), ())
        observed = BoxInspector(self.state_dir, database=self.fixture.fixture.store.path).read(self.run_id, "strategist")
        self.assertIn("WORKER_STREAM_IMPORTED", json.dumps(observed))
        self.assertFalse(observed["artifacts"])
        archive = self.state_dir.parent / "import-export"
        RunArchive.export(self.run_id, events, ArtifactStore(self.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), after)

    def test_lost_reply_receipt_recovered_after_revoke_and_key_loss_without_importing_more(self):
        self.deliver()
        original = self.fixture.fixture.store.append
        def lose_reply(event, **kwargs):
            original(event, **kwargs)
            raise OSError("fixture lost reply after kernel commit")
        with patch.object(self.fixture.fixture.store, "append", side_effect=lose_reply):
            with self.assertRaises(OSError):
                self.capture()
        expected = self.capture()
        self.deliver()
        self.service.revoke(self.scope, by="test-owner", reason="fixture shutdown")
        (self.service._directory(self.scope) / "key").rename(self.state_dir / "lost-key")
        self.assertEqual(self.capture(), expected)
        self.assertEqual(self.service.records(self.scope)["imported_cursor"], 1)
        with self.assertRaises(DeliveryError):
            self.capture("new-request")
        with self.assertRaises(DeliveryError):
            self.capture(limit=1)

    def test_append_failure_and_concurrent_revocation_never_advance_cursor(self):
        self.deliver()
        store = self.fixture.fixture.store
        with patch.object(store, "append", side_effect=OSError("fixture journal failure")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertNotIn("worker_imports", self.orch.state(self.run_id))
        original = store.append
        def revoke_first(event, **kwargs):
            if event["type"] == "WORKER_STREAM_IMPORTED":
                self.service.revoke(self.scope, by="test-owner", reason="concurrent revoke")
            return original(event, **kwargs)
        with patch.object(store, "append", side_effect=revoke_first):
            with self.assertRaises(ConcurrentAppendError):
                self.capture()
        self.assertNotIn("worker_imports", self.orch.state(self.run_id))

    def test_owner_expiry_and_run_bounds_fail_without_discarding_received_rows(self):
        self.deliver()
        with self.assertRaises(DeliveryError):
            self.service.import_received(self.scope, by="strategist", request_id="forged")
        for limit in (True, 0, 7):
            with self.assertRaises(DeliveryError):
                self.capture(limit=limit)
        with patch("camol.worker_import.MAX_CAPTURE_BYTES", 1):
            with self.assertRaises(DeliveryError):
                self.capture()
        self.assertNotIn("worker_imports", self.orch.state(self.run_id))
        self.fixture.fixture.clock.advance(31)
        with self.assertRaises(DeliveryError):
            self.capture()
        self.assertEqual(self.producer.inspect()["count"], 1)

    def test_received_source_gap_is_not_silently_skipped(self):
        self.deliver(2)
        path = self.service._directory(self.scope) / "receiver" / "worker-delivery.sqlite3"
        with sqlite3.connect(str(path)) as database:
            database.execute("DELETE FROM records WHERE seq=1")
        with self.assertRaises(DeliveryError):
            self.capture()
        self.assertNotIn("worker_imports", self.orch.state(self.run_id))

    def test_replay_rejects_changed_owner_cursor_scope_claim_and_capture_bytes(self):
        self.deliver()
        self.capture()
        events = self.fixture.fixture.store.read(self.run_id)
        index = next(i for i, event in enumerate(events) if event["type"] == "WORKER_STREAM_IMPORTED")
        changes = [lambda event: event.update(actor_id="strategist"),
                   lambda event: event["payload"].update(base_cursor=1),
                   lambda event: event["payload"].update(source_count=True),
                   lambda event: event["payload"].update(meaning="verified"),
                   lambda event: event["payload"].update(scope="sha256:" + "f" * 64),
                   lambda event: event["payload"]["records"][0]["data"].update(message="changed"),
                   lambda event: event["payload"]["records"][0].update(seq=True)]
        for change in changes:
            altered = copy.deepcopy(events)
            change(altered[index])
            with self.assertRaises(ValueError):
                project(altered)

    def test_capture_redaction_is_stable_on_replay_and_current_view_is_redacted(self):
        self.deliver(data={"message": "newly-protected-value"})
        with patch.dict(os.environ, {"CAMOL_NEW_TOKEN": "newly-protected-value"}):
            self.capture()
        events = self.fixture.fixture.store.read(self.run_id)
        imported = next(row for row in events if row["type"] == "WORKER_STREAM_IMPORTED")
        self.assertNotIn("newly-protected-value", json.dumps(imported))
        baseline = project(events)
        with patch.dict(os.environ, {"CAMOL_NEW_TOKEN": "worker-claim"}):
            self.assertEqual(project(events), baseline)
        self.deliver(data={"message": "later-protected-value"})
        self.capture("capture-2")
        with patch.dict(os.environ, {"CAMOL_NEW_TOKEN": "later-protected-value"}):
            self.assertNotIn("later-protected-value", json.dumps(self.service.records(self.scope)))
        key = (self.service._directory(self.scope) / "key").read_bytes()
        with self.assertRaises(DeliveryError):
            self.capture(key.hex())

    def test_empty_request_is_frozen_and_offline_cli_uses_ledger_only(self):
        empty = self.capture()
        self.assertEqual(empty["imported_count"], 0)
        self.deliver()
        self.assertEqual(self.capture(), empty)
        common = ["--state-dir", str(self.state_dir), "--db", str(self.fixture.fixture.store.path), "--run-id", self.run_id,
                  "--scope", self.scope]
        with patch.object(Orchestrator, "_now", return_value=self.orch._now()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["worker-enrollment", "import", *common, "--workspace", str(self.fixture.fixture.source),
                "--by", "test-owner", "--request-id", "cli-import"]), 0)
        self.assertEqual(json.loads(output.getvalue())["cursor"], 1)
        (self.service._directory(self.scope) / "key").rename(self.state_dir / "no-key")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["worker-enrollment", "records", *common]), 0)
        self.assertEqual(json.loads(output.getvalue())["imported_cursor"], 1)


class WorkerImportTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        tls_fixture.WorkerTLSTests.setUpClass.__func__(cls)

    async def asyncSetUp(self):
        await tls_fixture.WorkerTLSTests.asyncSetUp(self)

    async def test_real_tls_delivery_then_kernel_capture_survives_listener_shutdown(self):
        receipt = await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        self.assertEqual(receipt["cursor"], 1)
        self.assertNotIn("worker_imports", self.fixture.orch.state(self.fixture.fixture.run_id))
        captured = self.fixture.service.import_received(self.value["scope"], by="test-owner", request_id="tls-capture")
        self.assertEqual(captured["cursor"], 1)
        await self.server.close()
        restarted = WorkerEnrollment(Orchestrator(self.fixture.fixture.store, clock=self.fixture.fixture.clock),
            self.fixture.fixture.run_id, self.fixture.fixture.state_dir)
        self.assertEqual(restarted.records(self.value["scope"])["records"][0]["kind"], "checkpoint")
        self.assertFalse(restarted.records(self.value["scope"])["verified"])
