import copy
import base64
import contextlib
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from camol.readiness import LeaseFence
from camol.schema import canonical_digest
from camol.worker_delivery import DeliveryError, WorkerDelivery, authorize_state, binding, deliver_once, read_enrollment_key, FRAME_BYTES
from tests import test_admission_scheduler as admission_fixture


NOW = "2026-09-03T12:00:00+00:00"


def stream():
    fence = LeaseFence(lease_id="lease", run_id="run", task_id="task", box_id="box", worker_id="worker",
        target_id="target", epoch=1, issued_at=NOW, expires_at="2026-09-03T12:05:00+00:00",
        **{name: "sha256:" + "a" * 64 for name in LeaseFence.BOUND_DIGESTS})
    return dict(schema="camol.worker_stream", schema_version=1, control_plane_id="controller", worker_generation="generation-1",
                stream_id="stream-1", runtime_id="runtime", fence=fence.to_dict())


class WorkerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.key = secrets.token_bytes(32)
        self.stream = stream()
        self.producer = WorkerDelivery(self.root / "producer", self.stream, self.key, role="producer", create=True)
        self.receiver = WorkerDelivery(self.root / "receiver", self.stream, self.key, role="receiver", create=True)

    def queue(self, count=1):
        return [self.producer.queue("checkpoint", {"message": "bounded progress " + str(i)}, occurred_at=NOW) for i in range(count)]

    def resign(self, message, domain="batch"):
        message = copy.deepcopy(message)
        message.pop("mac", None)
        message["mac"] = self.producer._mac(domain, message)
        return json.dumps(message).encode()

    def test_durable_ack_loss_reconnect_pagination_and_no_duplicate_ingestion(self):
        self.queue(9)
        observed = []
        def guard(value):
            observed.append(value)
            return True
        def lost(raw):
            self.receiver.accept(raw, authorize=guard)
            raise ConnectionError("fixture lost the acknowledgment after commit")
        with self.assertRaises(ConnectionError):
            deliver_once(self.producer, lost)
        self.assertEqual(self.receiver.inspect()["count"], 6)
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        original = self.producer.batch()
        reopened = WorkerDelivery(self.root / "producer", self.stream, self.key, role="producer")
        receiver = WorkerDelivery(self.root / "receiver", self.stream, self.key, role="receiver")
        self.assertEqual(reopened.batch(), original)
        # Duplicate receipts may be recovered after expiry, but cannot append.
        denied = Mock(side_effect=AssertionError("exact receipt retry must not reauthorize or execute"))
        self.assertEqual(deliver_once(reopened, lambda raw: receiver.accept(raw, authorize=denied)), 6)
        denied.assert_not_called()
        self.assertEqual(deliver_once(reopened, lambda raw: receiver.accept(raw, authorize=guard)), 9)
        self.assertEqual(len(observed), 9)
        self.assertEqual(reopened.inspect()["unacknowledged"], 0)
        page = receiver.inspect(after=6, limit=2)
        self.assertEqual([r["seq"] for r in page["records"]], [7, 8])
        self.assertTrue(page["more"])
        self.assertFalse(page["execution_authority"])
        self.assertFalse(page["kernel_promoted"])

    def test_authentication_wrong_subject_unknown_fields_and_limits_never_call_guard(self):
        self.queue()
        original = json.loads(self.producer.batch())
        invalid = [dict(original, mac="x"), dict(original, mac="é" * 64), dict(original, extra=True),
                   dict(original, schema_version=True), dict(original, scope="sha256:" + "b" * 64)]
        wrong_key = WorkerDelivery(self.root / "wrong-key", self.stream, secrets.token_bytes(32), role="producer", create=True)
        wrong_key.queue("heartbeat", {}, occurred_at=NOW)
        guard = Mock(return_value=True)
        for raw in [json.dumps(v).encode() for v in invalid] + [wrong_key.batch(), b"x" * (FRAME_BYTES + 1), b'{"schema":1,"schema":2}']:
            with self.assertRaises(ValueError):
                self.receiver.accept(raw, authorize=guard)
        guard.assert_not_called()
        self.assertEqual(self.receiver.inspect()["count"], 0)
        with self.assertRaises(DeliveryError):
            self.receiver.accept(self.producer.batch(), authorize=None)

    def test_gap_reorder_changed_retry_and_batch_authority_failure_are_atomic(self):
        self.queue(3)
        original = json.loads(self.producer.batch())
        for records in (original["records"][1:], list(reversed(original["records"])), [original["records"][0], original["records"][2]]):
            with self.assertRaises(DeliveryError):
                self.receiver.accept(self.resign(dict(original, records=records)), authorize=lambda _: True)
        guard = Mock(side_effect=[True, False])
        with self.assertRaises(DeliveryError):
            self.receiver.accept(self.producer.batch(), authorize=guard)
        self.assertEqual(self.receiver.inspect()["count"], 0)
        self.receiver.accept(self.producer.batch(), authorize=lambda _: True)
        changed = copy.deepcopy(original["records"][0])
        changed["data"] = {"message": "changed"}
        changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
        with self.assertRaises(DeliveryError):
            self.receiver.accept(self.resign(dict(original, records=[changed])), authorize=lambda _: True)
        self.assertEqual(self.receiver.inspect()["count"], 3)

    def test_ack_bound_to_queued_digest_cannot_advance_or_rollback_and_domains_differ(self):
        self.queue()
        receipt = self.receiver.accept(self.producer.batch(), authorize=lambda _: True)
        good = json.loads(receipt)
        for change in (dict(cursor=2), dict(digest="sha256:" + "b" * 64), dict(cursor=True), dict(mac="é" * 64)):
            bad = dict(good, **change)
            raw = json.dumps(bad).encode() if "mac" in change else self.resign(bad, "ack")
            with self.assertRaises(DeliveryError):
                self.producer.acknowledge(raw)
        with self.assertRaises(DeliveryError):
            self.producer.acknowledge(self.resign(good, "batch"))
        self.assertEqual(self.producer.acknowledge(receipt), 1)
        zero = self.resign(dict(good, cursor=0, digest="sha256:" + "0" * 64), "ack")
        self.assertEqual(self.producer.acknowledge(zero), 1)

    def test_redaction_before_signing_and_new_receiver_input_denial_without_history_rewrite(self):
        secret = "private-test-credential-123456"
        with patch.dict(os.environ, {"CAMOL_TEST_TOKEN": secret}):
            self.producer.queue("tool", {"output": "echo " + secret}, occurred_at=NOW)
            raw = self.producer.batch()
            self.assertNotIn(secret.encode(), raw)
            self.receiver.accept(raw, authorize=lambda _: True)
        # Future environment changes cannot alter duplicate receipt recovery.
        with patch.dict(os.environ, {"CAMOL_OTHER_TOKEN": "echo"}):
            again = self.receiver.accept(raw, authorize=Mock(side_effect=AssertionError("no new evidence")))
        self.producer.acknowledge(again)
        record = self.producer.queue("diagnostic", {"note": secret}, occurred_at=NOW)
        with patch.dict(os.environ, {"CAMOL_TEST_TOKEN": secret}):
            with self.assertRaises(DeliveryError):
                self.receiver.accept(self.producer.batch(), authorize=lambda _: True)
        self.assertEqual(self.receiver.inspect()["count"], 1)
        self.assertNotIn(secret.encode(), self.receiver.path.read_bytes())
        self.assertEqual(record["seq"], 2)

    def test_enrollment_key_echo_is_removed_and_interrupt_rolls_back_before_ack(self):
        forms = [self.key.hex(), self.key.hex().upper(), base64.b64encode(self.key).decode(),
                 base64.urlsafe_b64encode(self.key).decode().rstrip("=")]
        self.producer.queue("tool", {"output": " ".join(forms)}, occurred_at=NOW)
        raw = self.producer.batch()
        self.assertTrue(all(value.encode() not in raw for value in forms))
        with self.assertRaises(KeyboardInterrupt):
            self.receiver.accept(raw, authorize=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(self.receiver.inspect()["count"], 0)
        self.receiver.accept(raw, authorize=lambda _: True)
        self.assertTrue(all(value.encode() not in self.receiver.path.read_bytes() for value in forms))
        original = json.loads(raw)
        forged = dict(original["records"][0], seq=2, previous=original["records"][0]["digest"], data={"output": self.key.hex()})
        forged["digest"] = canonical_digest({k: v for k, v in forged.items() if k != "digest"})
        with self.assertRaises(DeliveryError):
            self.receiver.accept(self.resign(dict(original, records=[forged])), authorize=lambda _: True)
        self.assertEqual(self.receiver.inspect()["count"], 1)

    def test_storage_identity_private_paths_contention_and_backpressure(self):
        for change in ({"runtime_id": "other"}, {"worker_generation": "generation-2"}):
            with self.assertRaises(DeliveryError):
                WorkerDelivery(self.root / "producer", dict(self.stream, **change), self.key, role="producer")
        with self.assertRaises(DeliveryError):
            WorkerDelivery(self.root / "producer", self.stream, secrets.token_bytes(32), role="producer")
        with self.assertRaises(DeliveryError):
            WorkerDelivery(self.root / "producer", self.stream, self.key, role="receiver")
        for directory, kind in ((self.root / "linked", "symlink"), (self.root / "hard", "hardlink")):
            directory.mkdir(mode=0o700)
            path = directory / "worker-delivery.sqlite3"
            path.symlink_to(self.producer.path) if kind == "symlink" else os.link(self.producer.path, path)
            try:
                with self.assertRaises(DeliveryError):
                    WorkerDelivery(directory, self.stream, self.key, role="producer")
            finally:
                path.unlink()
        with patch("camol.worker_delivery.MAX_UNACKNOWLEDGED", 2):
            self.queue(2)
            with self.assertRaisesRegex(DeliveryError, "backpressure"):
                self.queue()
            self.assertEqual(self.producer.inspect()["count"], 2)
            deliver_once(self.producer, lambda raw: self.receiver.accept(raw, authorize=lambda _: True))
            self.queue()
        db = sqlite3.connect(str(self.receiver.path), isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(DeliveryError):
                self.receiver.accept(self.producer.batch(), authorize=Mock(side_effect=AssertionError("storage unavailable")))
        finally:
            db.close()
        self.assertEqual(self.receiver.inspect()["count"], 2)
        self.producer.path.chmod(0o644)
        with self.assertRaises(DeliveryError):
            self.producer.inspect()

    def test_concurrent_producers_and_receivers_serialize_exact_cursor(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda n: self.producer.queue("heartbeat", {"n": n}, occurred_at=NOW), range(20)))
        self.assertEqual(sorted(r["seq"] for r in records), list(range(1, 21)))
        while self.producer.inspect()["unacknowledged"]:
            raw = self.producer.batch()
            with ThreadPoolExecutor(max_workers=4) as pool:
                replies = list(pool.map(lambda _: self.receiver.accept(raw, authorize=lambda _: True), range(4)))
            self.assertEqual(len(set(replies)), 1)
            self.producer.acknowledge(replies[0])
        self.assertEqual(self.receiver.inspect()["count"], 20)

    def test_real_child_producer_durable_before_exit_and_reopened_receiver(self):
        # Fixture enrollment goes through private stdin; no credential argv/env.
        program = '''import json,sys
from camol.worker_delivery import WorkerDelivery
v=json.loads(sys.stdin.buffer.read())
p=WorkerDelivery(v["root"],v["stream"],bytes.fromhex(v["key"]),role="producer")
p.queue("checkpoint",{"source":"child-process"},occurred_at=v["now"])
sys.stdout.buffer.write(p.batch())
'''
        request = dict(root=str(self.root / "producer"), stream=self.stream, key=self.key.hex(), now=NOW)
        child = subprocess.run([sys.executable, "-c", program], input=json.dumps(request).encode(), capture_output=True, timeout=15, check=True)
        self.assertNotIn(self.key.hex().encode(), child.stdout + child.stderr)
        ack = self.receiver.accept(child.stdout, authorize=lambda _: True)
        reopened = WorkerDelivery(self.root / "receiver", self.stream, self.key, role="receiver")
        self.assertEqual(reopened.inspect()["records"][0]["data"], {"source": "child-process"})
        self.assertEqual(self.producer.acknowledge(ack), 1)

    def test_actual_kernel_lease_guard_expiry_and_reassignment_never_make_evidence_authority(self):
        fixture = admission_fixture.AdmissionSchedulerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        bundle, _ = fixture.admit()
        assignment = fixture.frame_assignment()
        fixture.orchestrator.start_task(fixture.run_id, assignment)
        actual = dict(self.stream, fence=assignment["fence"], runtime_id=bundle.receipt.runtime_id)
        producer = WorkerDelivery(self.root / "actual-producer", actual, self.key, role="producer", create=True)
        receiver = WorkerDelivery(self.root / "actual-receiver", actual, self.key, role="receiver", create=True)
        def guard(value):
            return authorize_state(fixture.orchestrator.state(fixture.run_id), value, now=fixture.clock().isoformat())
        before = fixture.orchestrator.state(fixture.run_id)
        producer.queue("turn_result", {"claim": "succeeded", "tokens": 123}, occurred_at=NOW)
        deliver_once(producer, lambda raw: receiver.accept(raw, authorize=guard))
        self.assertEqual(fixture.orchestrator.state(fixture.run_id), before)
        producer.queue("heartbeat", {}, occurred_at=NOW)
        fixture.clock.advance(31)
        with self.assertRaises(DeliveryError):
            receiver.accept(producer.batch(), authorize=guard)
        self.assertEqual(receiver.inspect()["count"], 1)
        for change in (dict(plan_digest="sha256:" + "b" * 64), dict(lease_epochs={"frame": 2}), dict(terminal={"status": "stopped"})):
            with self.assertRaises(DeliveryError):
                authorize_state(dict(before, **change), actual, now=NOW)
        with self.assertRaises(DeliveryError):
            authorize_state(before, actual, now="2026-09-03T11:59:59+00:00")

    def test_binding_and_record_validation_no_execution_or_unbounded_payload(self):
        for value in (dict(self.stream, schema_version=True), dict(self.stream, unexpected=True), dict(self.stream, worker_generation="x" * 129)):
            with self.assertRaises(ValueError):
                binding(value)
        for kind, data in (("execute", {}), ("tool", {"output": "x" * 40000}), ("usage", {"value": float("inf")}), ("heartbeat", [])):
            with self.assertRaises(ValueError):
                self.producer.queue(kind, data, occurred_at=NOW)
        self.assertEqual(self.producer.inspect()["count"], 0)
        self.assertFalse((self.root / "missing").exists())
        with self.assertRaises((OSError, DeliveryError)):
            WorkerDelivery(self.root / "missing", self.stream, self.key, role="producer")
        self.assertFalse((self.root / "missing").exists())

    def test_cli_offline_inspection_and_explicit_private_key_boundary(self):
        from camol.cli import main
        key = self.root / "enrollment.key"
        descriptor = os.open(str(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(self.key)
        manifest = self.root / "binding.json"
        manifest.write_text(json.dumps(self.stream))
        self.queue()
        output = io.StringIO()
        argv = ["worker-delivery", "inspect", "--root", str(self.root / "producer"), "--binding", str(manifest),
                "--key-file", str(key), "--role", "producer"]
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(argv), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["unacknowledged"], 1)
        self.assertNotIn(self.key.hex(), output.getvalue())
        self.assertEqual(read_enrollment_key(key), self.key)
        alias = self.root / "alias"
        alias.symlink_to(key)
        with self.assertRaises(DeliveryError):
            read_enrollment_key(alias)
        alias.unlink()
        os.link(key, alias)
        with self.assertRaises(DeliveryError):
            read_enrollment_key(key)
        alias.unlink()
        key.chmod(0o644)
        with self.assertRaises(DeliveryError):
            read_enrollment_key(key)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(argv), 2)
        self.assertNotIn(self.key.hex(), errors.getvalue())


if __name__ == "__main__":
    unittest.main()
