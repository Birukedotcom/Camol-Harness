import copy
import contextlib
import io
import hashlib
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from camol.artifacts import ArtifactStore, RunArchive
from camol.box_inspection import BoxInspector
from camol.orchestrator import Orchestrator
from camol.state import project
from camol.store import ConcurrentAppendError, SQLiteEventStore
from camol.worker_delivery import DeliveryError, deliver_once, read_enrollment_key
from camol.worker_enrollment import WorkerEnrollment
from tests import test_admission_scheduler as fixture_module


class WorkerEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.AdmissionSchedulerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.state_dir = self.fixture.state_dir.resolve()
        self.bundle, _ = self.fixture.admit()
        assignment = self.fixture.frame_assignment()
        self.fixture.orchestrator.start_task(self.fixture.run_id, assignment)
        self.orch = self.fixture.orchestrator
        self.service = WorkerEnrollment(self.orch, self.fixture.run_id, self.fixture.state_dir)
        self.stream = dict(schema="camol.worker_stream", schema_version=1, control_plane_id="controller-1",
            worker_generation="generation-1", stream_id="stream-1", runtime_id=self.bundle.receipt.runtime_id,
            fence=assignment["fence"])

    def prepared(self):
        return self.service.prepare(self.stream, by="test-owner")

    def approved(self):
        value = self.prepared()
        self.service.approve(value, by="test-owner", review_digest=value["digest"])
        return value

    def producer(self, value):
        return self.service.producer(value["scope"], self.fixture.state_dir / "test-producer", by="test-owner")

    def test_owner_review_receive_restart_export_and_box_association_without_key_or_success(self):
        before = self.orch.state(self.fixture.run_id)
        value = self.prepared()
        self.assertNotIn("worker_streams", self.orch.state(self.fixture.run_id))
        with self.assertRaises(DeliveryError):
            self.service.receive(value["scope"], b"{}")
        self.service.approve(value, by="test-owner", review_digest=value["digest"])
        producer = self.producer(value)
        producer.queue("turn_result", {"claimed_status": "succeeded", "claimed_tokens": 600}, occurred_at=self.orch._now())
        self.assertEqual(deliver_once(producer, lambda raw: self.service.receive(value["scope"], raw)), 1)
        current = self.orch.state(self.fixture.run_id)
        for field in ("tasks", "agents", "total_tokens", "evidence", "gate_assessments"):
            self.assertEqual(current[field], before[field])
        events = self.fixture.store.read(self.fixture.run_id)
        key = read_enrollment_key(self.service._directory(value["scope"]) / "key")
        encoded = json.dumps(events)
        self.assertNotIn(key.hex(), encoded)
        self.assertIn("sha256:" + hashlib.sha256(key).hexdigest(), encoded)
        self.assertEqual(project(events), current)
        restarted = WorkerEnrollment(Orchestrator(self.fixture.store, clock=self.fixture.clock), self.fixture.run_id, self.fixture.state_dir)
        self.assertEqual(restarted.inspect(value["scope"]), self.service.inspect(value["scope"]))
        self.assertEqual(deliver_once(producer, lambda raw: restarted.receive(value["scope"], raw)), 1)
        snapshot = BoxInspector(self.fixture.state_dir, database=self.fixture.store.path).read(self.fixture.run_id, "strategist")
        self.assertIn("WORKER_STREAM_ENROLLED", json.dumps(snapshot))
        archive = self.fixture.state_dir.parent / "archive"
        RunArchive.export(self.fixture.run_id, events, ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), current)
        self.assertFalse(any(path.name == "key" for path in archive.rglob("*")))

    def test_review_mutations_worker_approval_key_replacement_and_duplicate_generation_deny(self):
        value = self.prepared()
        for by, digest in (("strategist", value["digest"]), ("test-owner", "sha256:" + "f" * 64)):
            with self.assertRaises(DeliveryError):
                self.service.approve(value, by=by, review_digest=digest)
        changed = copy.deepcopy(value)
        changed["execution_authority"] = True
        with self.assertRaises(DeliveryError):
            self.service.approve(changed, by="test-owner", review_digest=value["digest"])
        path = self.service._directory(value["scope"]) / "key"
        original = path.read_bytes()
        path.write_bytes(b"x" * 32)
        with self.assertRaises(DeliveryError):
            self.service.approve(value, by="test-owner", review_digest=value["digest"])
        path.write_bytes(original)
        self.service.approve(value, by="test-owner", review_digest=value["digest"])
        self.assertEqual(self.service.approve(value, by="test-owner", review_digest=value["digest"]), self.service.inspect(value["scope"]))
        replacement = self.service.prepare(dict(self.stream, worker_generation="generation-2", stream_id="stream-2"), by="test-owner")
        with self.assertRaises(DeliveryError):
            self.service.approve(replacement, by="test-owner", review_digest=replacement["digest"])
        self.assertEqual(len(self.orch.state(self.fixture.run_id)["worker_streams"]), 1)

    def test_revocation_is_durable_prevents_old_receipts_and_never_resurrects(self):
        value = self.approved()
        producer = self.producer(value)
        producer.queue("heartbeat", {}, occurred_at=self.orch._now())
        self.service.receive(value["scope"], producer.batch())
        with self.assertRaises(DeliveryError):
            self.service.revoke(value["scope"], by="strategist", reason="forged")
        self.service.revoke(value["scope"], by="test-owner", reason="replace this generation")
        with self.assertRaises(DeliveryError):
            self.service.receive(value["scope"], producer.batch())
        with self.assertRaises(DeliveryError):
            self.service.approve(value, by="test-owner", review_digest=value["digest"])
        self.assertEqual(self.service.revoke(value["scope"], by="test-owner", reason="ignored retry")["reason"], "replace this generation")
        replacement = self.service.prepare(dict(self.stream, worker_generation="generation-2", stream_id="stream-2"), by="test-owner")
        self.service.approve(replacement, by="test-owner", review_digest=replacement["digest"])
        events = self.fixture.store.read(self.fixture.run_id)
        self.assertEqual(project(events), self.orch.state(self.fixture.run_id))

    def test_expiry_stale_snapshot_and_append_failure_grant_no_receiver_authority(self):
        value = self.prepared()
        with patch.object(self.orch, "_emit", side_effect=ConcurrentAppendError("fixture race")):
            with self.assertRaises(ConcurrentAppendError):
                self.service.approve(value, by="test-owner", review_digest=value["digest"])
        self.assertNotIn("worker_streams", self.orch.state(self.fixture.run_id))
        with self.assertRaises(DeliveryError):
            self.service.receive(value["scope"], b"{}")
        self.service.approve(value, by="test-owner", review_digest=value["digest"])
        producer = self.producer(value)
        producer.queue("heartbeat", {}, occurred_at=self.orch._now())
        self.fixture.clock.advance(31)
        with self.assertRaises(DeliveryError):
            self.service.receive(value["scope"], producer.batch())
        self.assertEqual(producer.inspect()["acknowledged"], 0)

    def test_kernel_write_lock_prevents_concurrent_lease_mutation_and_releases_after_error(self):
        value = self.approved()
        producer = self.producer(value)
        producer.queue("heartbeat", {}, occurred_at=self.orch._now())
        independent = sqlite3.connect(str(self.fixture.store.path), isolation_level=None, timeout=.01)
        self.addCleanup(independent.close)
        from camol.worker_delivery import WorkerDelivery
        original = WorkerDelivery.accept
        def while_locked(receiver, raw, *, authorize):
            with self.assertRaises(sqlite3.OperationalError):
                independent.execute("BEGIN IMMEDIATE")
            return original(receiver, raw, authorize=authorize)
        with patch.object(WorkerDelivery, "accept", while_locked):
            self.service.receive(value["scope"], producer.batch())
        independent.execute("BEGIN IMMEDIATE")
        independent.rollback()
        with patch.object(WorkerDelivery, "accept", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.receive(value["scope"], producer.batch())
        self.assertFalse(self.fixture.store.connection.in_transaction)
        independent.execute("BEGIN IMMEDIATE")
        independent.rollback()
        self.assertEqual(self.fixture.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)

    def test_partial_setup_symlink_and_existing_kernel_transaction_fail_closed(self):
        value = self.prepared()
        with self.assertRaises(FileExistsError):
            self.prepared()
        directory = self.service._directory(value["scope"])
        proposal_path = directory / "proposal.json"
        proposal_path.rename(directory / "saved.json")
        proposal_path.symlink_to(directory / "saved.json")
        with self.assertRaises((DeliveryError, OSError)):
            self.service.approve(value, by="test-owner", review_digest=value["digest"])
        proposal_path.unlink()
        (directory / "saved.json").rename(proposal_path)
        self.service.approve(value, by="test-owner", review_digest=value["digest"])
        producer = self.producer(value)
        producer.queue("heartbeat", {}, occurred_at=self.orch._now())
        self.fixture.store.connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(DeliveryError):
                self.service.receive(value["scope"], producer.batch())
            self.assertTrue(self.fixture.store.connection.in_transaction)
        finally:
            self.fixture.store.connection.rollback()

    def test_unauthenticated_batch_cannot_take_kernel_lock(self):
        value = self.approved()
        producer = self.producer(value)
        producer.queue("heartbeat", {}, occurred_at=self.orch._now())
        raw = json.loads(producer.batch())
        raw["mac"] = "0" * 64
        with patch.object(self.service, "_lease_lock", side_effect=AssertionError("unauthenticated kernel contention")):
            with self.assertRaises(DeliveryError):
                self.service.receive(value["scope"], json.dumps(raw).encode())

    def test_forged_events_and_later_environment_cannot_rewrite_review_history(self):
        value = self.approved()
        self.service.revoke(value["scope"], by="test-owner", reason="stable-owner-explanation")
        events = self.fixture.store.read(self.fixture.run_id)
        for kind in ("WORKER_STREAM_ENROLLED", "WORKER_STREAM_REVOKED"):
            altered = copy.deepcopy(events)
            index = next(i for i, row in enumerate(altered) if row["type"] == kind)
            altered[index]["actor_id"] = "strategist"
            with self.assertRaises(DeliveryError):
                project(altered)
        altered = copy.deepcopy(events)
        enrolled = next(row for row in altered if row["type"] == "WORKER_STREAM_ENROLLED")
        enrolled["payload"]["proposal"]["schema_version"] = True
        with self.assertRaises(DeliveryError):
            project(altered)
        with patch.dict(os.environ, {"CAMOL_NEW_TOKEN": "stable-owner-explanation"}):
            self.assertEqual(project(events), self.orch.state(self.fixture.run_id))

    def test_key_echo_redaction_revocation_without_key_and_owner_api(self):
        from camol.api import Harness
        value = self.approved()
        key_path = self.service._directory(value["scope"]) / "key"
        key = read_enrollment_key(key_path)
        with Harness(self.fixture.source, self.fixture.state_dir, database=self.fixture.store.path) as harness:
            harness.orchestrator.clock = self.fixture.clock
            self.assertEqual(harness.worker_streams.inspect(value["scope"]), self.service.inspect(value["scope"]))
            harness.worker_streams.revoke(value["scope"], by="test-owner", reason="echo " + key.hex())
        self.assertNotIn(key.hex(), json.dumps(self.fixture.store.read(self.fixture.run_id)))
        replacement = self.service.prepare(dict(self.stream, stream_id="replacement"), by="test-owner")
        self.service.approve(replacement, by="test-owner", review_digest=replacement["digest"])
        (self.service._directory(replacement["scope"]) / "key").rename(self.fixture.state_dir / "lost-key-fixture")
        result = self.service.revoke(replacement["scope"], by="test-owner", reason="private free-text withheld")
        self.assertEqual(result["status"], "revoked")
        self.assertIn("Reason withheld", result["reason"])
        self.assertNotIn("private free-text withheld", json.dumps(self.fixture.store.read(self.fixture.run_id)))

    def test_cli_review_and_offline_inspection_share_exact_owner_ledger(self):
        from camol.cli import main
        value = self.prepared()
        path = self.fixture.state_dir / "review.json"
        path.write_text(json.dumps(value))
        common = ["--state-dir", str(self.fixture.state_dir), "--db", str(self.fixture.store.path), "--run-id", self.fixture.run_id]
        approve = ["worker-enrollment", "approve", *common, "--workspace", str(self.fixture.source), "--by", "test-owner",
                   "--proposal", str(path), "--review-digest", value["digest"]]
        with patch.object(Orchestrator, "_now", return_value=self.orch._now()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(approve), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "active")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["worker-enrollment", "inspect", *common, "--scope", value["scope"]]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.service.inspect(value["scope"]))
        missing = self.fixture.state_dir.parent / "never-created-state"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["worker-enrollment", "inspect", "--state-dir", str(missing), "--run-id", self.fixture.run_id, "--scope", value["scope"]]), 2)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
