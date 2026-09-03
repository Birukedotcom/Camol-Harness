import hashlib
import tempfile
import unittest
from pathlib import Path

from camol.artifacts import ArtifactError, ArtifactRef, ArtifactStore, RunArchive
from camol.probes import REDACTED, Redactor


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.secret = "super-secret-value-12345"
        self.store = ArtifactStore(
            self.state,
            redactor=Redactor({"CAMOL_TEST_SECRET": self.secret}),
            max_retained_bytes=64,
        )
        self.producer = {
            "run_id": "run-1",
            "task_id": "task-1",
            "agent_id": "agent-1",
            "lease_id": "lease-1",
            "channel": "stdout",
            "role": "adapter",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_text_is_redacted_before_content_addressed_persistence(self):
        raw = ("prefix {} suffix".format(self.secret)).encode()
        reference = self.store.put_bytes(raw, producer=self.producer, redact=True)
        stored = self.store.read(reference)
        self.assertNotIn(self.secret.encode(), stored)
        self.assertIn(REDACTED.encode(), stored)
        self.assertEqual(reference.source_sha256, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(reference.source_bytes, len(raw))
        self.assertEqual(ArtifactRef.from_dict(reference.to_dict()), reference)
        self.assertEqual(oct(self.store.path_for(reference.digest).stat().st_mode & 0o777), "0o600")

    def test_seeded_secret_never_appears_in_portable_archive(self):
        reference = self.store.put_bytes(
            ("token={}".format(self.secret)).encode(), producer=self.producer, redact=True
        )
        event = {
            "run_id": "run-1",
            "seq": 1,
            "event_id": "event-1",
            "type": "EVIDENCE_RECORDED",
            "actor_id": "adapter",
            "occurred_at": "2026-09-03T12:00:00+00:00",
            "payload": {"artifact_refs": [reference.to_dict()]},
        }
        destination = self.root / "archive"
        RunArchive.export("run-1", [event], self.store, destination)
        exported = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
        self.assertNotIn(self.secret.encode(), exported)
        RunArchive.verify(destination)

    def test_large_stream_metadata_preserves_full_hash_while_content_is_bounded(self):
        raw = b"x" * 4096
        reference = self.store.put_bytes(raw, producer=self.producer, redact=False)
        self.assertTrue(reference.truncated)
        self.assertEqual(reference.stored_bytes, 64)
        self.assertEqual(reference.source_bytes, 4096)
        self.assertEqual(reference.source_sha256, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.read(reference), b"x" * 64)

    def test_existing_blob_corruption_and_symlinked_sink_are_rejected(self):
        reference = self.store.put_bytes(b"evidence", producer=self.producer, redact=False)
        self.store.path_for(reference.digest).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ArtifactError, "verification"):
            self.store.read(reference)
        other_state = self.root / "other-state"
        other_state.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (other_state / "artifacts").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactError, "symlink"):
            ArtifactStore(other_state)

    def test_reference_rejects_unknown_fields(self):
        reference = self.store.put_bytes(b"evidence", producer=self.producer, redact=False)
        payload = reference.to_dict()
        payload["url"] = "https://example.invalid/blob"
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            ArtifactRef.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
