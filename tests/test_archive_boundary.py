import hashlib
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camol.archive_io import ArchiveIOError, ArchiveRoot
from camol.artifacts import ArtifactError, ArtifactStore, RunArchive
from camol.schema import canonical_digest


class ArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.store = ArtifactStore(self.state)
        self.reference = self.store.put_bytes(b"retained evidence", producer={"run_id": "run"})
        self.events = [{"run_id": "run", "payload": {"artifact": self.reference.to_dict()}}]
        self.archive = self.root / "archive"
        self.manifest = RunArchive.export("run", self.events, self.store, self.archive)

    def rewrite(self, *, manifest=None, raw_events=None):
        value = dict(self.manifest if manifest is None else manifest)
        if raw_events is not None:
            (self.archive / "events.jsonl").write_bytes(raw_events)
            value["events_sha256"] = "sha256:" + hashlib.sha256(raw_events).hexdigest()
        value["manifest_digest"] = canonical_digest({k: v for k, v in value.items() if k != "manifest_digest"})
        (self.archive / "manifest.json").write_text(json.dumps(value))

    def test_export_is_private_and_original_format_still_verifies(self):
        self.assertEqual(RunArchive.verify(self.archive), (self.manifest, self.events))
        for path in [self.archive] + list(self.archive.rglob("*")):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_linked_root_manifest_event_and_blob_ancestors_are_denied(self):
        alias = self.root / "alias"
        alias.symlink_to(self.archive, target_is_directory=True)
        with self.assertRaises(ArtifactError):
            RunArchive.verify(alias)
        for name in ("manifest.json", "events.jsonl", "blobs", "blobs/sha256"):
            with self.subTest(name=name):
                member = self.archive / name
                outside = self.root / "moved"
                member.rename(outside)
                member.symlink_to(outside, target_is_directory=outside.is_dir())
                try:
                    with self.assertRaises(ArtifactError):
                        RunArchive.verify(self.archive)
                finally:
                    member.unlink()
                    outside.rename(member)

    def test_hard_links_and_fifo_are_denied_without_blocking(self):
        member = self.archive / "events.jsonl"
        member.unlink()
        os.mkfifo(str(member))
        with self.assertRaises(ArtifactError):
            RunArchive.verify(self.archive)
        member.unlink()
        os.link(str(self.archive / "manifest.json"), str(member))
        with self.assertRaises(ArtifactError):
            RunArchive.verify(self.archive)

    def test_duplicate_keys_nonfinite_and_nonobject_events_are_denied(self):
        for raw in (b'{"run_id":"run","run_id":"run"}\n',
                    b'{"run_id":"run","value":NaN}\n', b'[]\n', b'null\n'):
            with self.subTest(raw=raw):
                self.rewrite(raw_events=raw)
                with self.assertRaises(ArtifactError):
                    RunArchive.verify(self.archive)

    def test_duplicate_manifest_and_nonobject_manifest_are_denied(self):
        for raw in (b'[]', b'null', b'{"schema":1,"schema":2}'):
            (self.archive / "manifest.json").write_bytes(raw)
            with self.assertRaises(ArtifactError):
                RunArchive.verify(self.archive)

    def test_boolean_count_is_not_an_integer_count(self):
        self.rewrite(manifest=dict(self.manifest, event_count=True))
        with self.assertRaises(ArtifactError):
            RunArchive.verify(self.archive)

    def test_lineage_traversal_is_rejected_before_reading_external_bytes(self):
        escaped = self.root / "outside.jsonl"
        escaped.write_text("sensitive fixture")
        for identifier in ("../outside", "a/../../outside", "nested/run"):
            value = dict(self.manifest, schema_version=2, lineage={identifier: {
                "event_count": 0, "events_sha256": "sha256:" + "0" * 64}})
            self.rewrite(manifest=value)
            with self.assertRaises(ArtifactError):
                RunArchive.verify(self.archive)
        self.assertEqual(escaped.read_text(), "sensitive fixture")

    def test_export_invalid_lineage_does_not_create_destination(self):
        destination = self.root / "refused"
        with self.assertRaises(ArtifactError):
            RunArchive.export("run", self.events, self.store, destination, lineage_events={"a/../../outside": []})
        self.assertFalse(destination.exists())

    def test_manifest_event_blob_and_inventory_bounds_fail_closed(self):
        for name, limit in (("MAX_MANIFEST_BYTES", 32), ("MAX_EVENT_BYTES", 32),
                            ("MAX_EVENT_TOTAL", 32), ("MAX_BLOB_BYTES", 1),
                            ("MAX_BLOB_TOTAL", 1), ("MAX_BLOBS", 0), ("MAX_JSON_NODES", 1)):
            with self.subTest(bound=name), patch.object(RunArchive, name, limit):
                with self.assertRaises(ArtifactError):
                    RunArchive.verify(self.archive)

    def test_json_depth_is_bounded_even_with_valid_hashes(self):
        nested = 0
        for _ in range(70):
            nested = [nested]
        self.rewrite(raw_events=json.dumps({"run_id": "run", "nested": nested}).encode())
        with self.assertRaisesRegex(ArtifactError, "structural ceiling"):
            RunArchive.verify(self.archive)

    def test_source_artifact_ancestor_link_is_denied_and_no_manifest_published(self):
        artifacts = self.state / "artifacts"
        outside = self.root / "outside"
        artifacts.rename(outside)
        artifacts.symlink_to(outside, target_is_directory=True)
        destination = self.root / "refused"
        with self.assertRaises(ArtifactError):
            RunArchive.export("run", self.events, self.store, destination)
        self.assertFalse((destination / "manifest.json").exists())

    def test_export_does_not_overwrite_an_existing_archive(self):
        before = (self.archive / "manifest.json").read_bytes()
        with self.assertRaises(ArtifactError):
            RunArchive.export("run", self.events, self.store, self.archive)
        self.assertEqual((self.archive / "manifest.json").read_bytes(), before)

    def test_file_growth_and_directory_replacement_are_detected(self):
        with self.assertRaises(ArchiveIOError):
            with ArchiveRoot(self.archive) as reader:
                reader.read("events.jsonl", 1 << 20)
                with (self.archive / "events.jsonl").open("ab") as handle:
                    handle.write(b" ")
        with self.assertRaises((ArchiveIOError, OSError)):
            with ArchiveRoot(self.archive) as reader:
                relative = "blobs/sha256/" + self.reference.digest[7:9] + "/" + self.reference.digest[9:]
                reader.read(relative, 1 << 20)
                (self.archive / "blobs").rename(self.root / "replaced")
                (self.archive / "blobs").symlink_to(self.root / "replaced", target_is_directory=True)

    def test_writer_refuses_link_injected_after_root_was_opened(self):
        destination = self.root / "destination"
        outside = self.root / "outside"
        outside.mkdir()
        with ArchiveRoot(destination, create=True) as writer:
            (destination / "blobs").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                writer.write("blobs/sentinel", b"never write this")
        self.assertFalse((outside / "sentinel").exists())

    def test_cli_projects_the_exact_verified_snapshot_only_once(self):
        from camol.cli import command_verify_export

        with patch.object(RunArchive, "verify", return_value=(self.manifest, self.events)) as verify, \
                patch.object(RunArchive, "replay", side_effect=AssertionError("must not reread")), \
                patch("camol.state.project", return_value={}) as project, \
                patch("camol.cli.summary", return_value={}), patch("camol.cli._write_json"):
            self.assertEqual(command_verify_export(argparse.Namespace(archive=str(self.archive))), 0)
        verify.assert_called_once_with(self.archive)
        project.assert_called_once_with(self.events)

    def test_json_node_budget_is_shared_across_events_and_manifest(self):
        # Each record is individually tiny; only the combined budget is exceeded.
        events = b'\n'.join(json.dumps({"run_id": "run", "values": [1] * 8}).encode() for _ in range(5))
        self.rewrite(manifest=dict(self.manifest, event_count=5, artifact_digests=[]), raw_events=events)
        with patch.object(RunArchive, "MAX_JSON_NODES", 40):
            with self.assertRaisesRegex(ArtifactError, "structural ceiling"):
                RunArchive.verify(self.archive)

    def test_export_node_bound_is_checked_before_destination_creation(self):
        destination = self.root / "too-large"
        with patch.object(RunArchive, "MAX_JSON_NODES", 20):
            with self.assertRaisesRegex(ArtifactError, "structural ceiling"):
                RunArchive.export("run", self.events, self.store, destination)
        self.assertFalse(destination.exists())

    def test_empty_stream_still_requires_valid_run_identity(self):
        destination = self.root / "invalid-empty"
        with self.assertRaises(ArtifactError):
            RunArchive.export("", [], self.store, destination)
        self.assertFalse(destination.exists())

    def test_empty_existing_destination_must_be_private_and_is_not_chmodded(self):
        destination = self.root / "public-output"
        destination.mkdir(mode=0o755)
        os.chmod(str(destination), 0o755)
        with self.assertRaises(ArtifactError):
            RunArchive.export("run", self.events, self.store, destination)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(destination.iterdir()), [])
        os.chmod(str(destination), 0o700)
        RunArchive.export("run", self.events, self.store, destination)
        RunArchive.verify(destination)

    def test_caller_mutation_cannot_change_the_encoded_snapshot_inventory(self):
        original_redaction = self.store.redactor.value

        def mutate_caller_after_encoding(value):
            self.events[0]["payload"].clear()
            return original_redaction(value)

        destination = self.root / "snapshot"
        with patch.object(self.store.redactor, "value", side_effect=mutate_caller_after_encoding):
            RunArchive.export("run", self.events, self.store, destination)
        manifest, events = RunArchive.verify(destination)
        self.assertEqual(manifest["artifact_digests"], [self.reference.digest])
        self.assertEqual(events[0]["payload"]["artifact"], self.reference.to_dict())


if __name__ == "__main__":
    unittest.main()
