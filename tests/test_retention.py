import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from camol.artifacts import ArtifactStore
from camol.events import new_event
from camol.orchestrator import Orchestrator
from camol.retention import (CLASSES, InventoryLimits, RetentionError, RetentionPolicy,
                             RetentionRule, inspect_retention)
from camol.store import SQLiteEventStore


class RetentionInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.database = self.state / "camol.sqlite3"
        self.document = json.loads((Path(__file__).resolve().parents[1] / "examples/local-n-box-runbook.json").read_text())
        self.run_id = self.document["run"]["id"]
        self.plan = self.create_run(self.run_id)
        self.policy = RetentionPolicy(self.run_id, self.plan, "owner", tuple(RetentionRule(name, None, "hold") for name in CLASSES))
        self.cas = ArtifactStore(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def create_run(self, run_id):
        document = json.loads(json.dumps(self.document))
        document["run"]["id"] = run_id
        store = SQLiteEventStore(self.database)
        try:
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(document)
            orchestrator.approve_plan(run_id, "owner", state["plan_digest"])
            return state["plan_digest"]
        finally:
            store.close()

    def append(self, payload, run_id=None, event_type="EVIDENCE_RECORDED"):
        store = SQLiteEventStore(self.database)
        try:
            event = new_event(run_id or self.run_id, "EVIDENCE_RECORDED", "fixture", payload)
            event["type"] = event_type
            store.append(event)
        finally:
            store.close()

    def artifact(self, text=b"test evidence"):
        ref = self.cas.put_bytes(text, producer={"run_id": self.run_id}, redact=False)
        self.append({"artifact_refs": [ref.to_dict()]})
        return ref

    def inspect(self, **kwargs):
        return inspect_retention(state_dir=self.state, database=self.database, run_id=self.run_id,
                                 policy=kwargs.pop("policy", self.policy), **kwargs)

    def fingerprint(self):
        return {str(path.relative_to(self.state)): (path.stat().st_mode, path.stat().st_ino,
                path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
                for path in [self.state] + sorted(self.state.rglob("*"))}

    def test_policy_roundtrip_strict_and_never_purge_authority(self):
        self.assertEqual(RetentionPolicy.from_dict(self.policy.to_dict()), self.policy)
        self.assertEqual(self.policy.digest(), RetentionPolicy.from_dict(self.policy.to_dict()).digest())
        for field, value in (("extra", True), ("schema_version", True), ("plan_digest", "bogus")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                RetentionPolicy.from_dict(dict(self.policy.to_dict(), **{field: value}))
        data = self.policy.to_dict()
        data["rules"][0]["extra"] = "never accepted"
        with self.assertRaisesRegex(RetentionError, "unknown"):
            RetentionPolicy.from_dict(data)
        for age, disposition in ((1, "hold"), (None, "retain"), (None, "delete")):
            with self.assertRaises(RetentionError):
                RetentionRule("unknown", age, disposition)
        with self.assertRaises(RetentionError):
            RetentionRule("artifacts", True, "hold")
        for value in ([], {}, True, None):
            with self.subTest(disposition=value), self.assertRaises(RetentionError):
                RetentionRule("artifacts", None, value)

    def test_exact_snapshot_and_nonmutation(self):
        ref = self.artifact(b"private fixture response must not appear in output")
        before = self.fingerprint()
        report = self.inspect()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(report.policy_digest, self.policy.digest())
        self.assertEqual(report.stream_cuts[0].last_seq, 3)
        item = report.artifacts[0]
        self.assertEqual(item.digest, ref.digest)
        self.assertEqual(item.ownership, "run_exclusive")
        self.assertEqual(item.reference_owners, (self.run_id,))
        self.assertTrue(item.bytes_verified)
        self.assertEqual(item.action, "hold")
        self.assertEqual(item.content_class, "unknown")
        self.assertFalse(report.to_dict()["deletion_authorized"])
        self.assertFalse(report.to_dict()["filesystem_inventory_complete"])
        self.assertNotIn("private fixture response", json.dumps(report.to_dict()))
        self.assertEqual(report.digest(), self.inspect().digest())

    def test_all_stream_reference_ownership_and_dedup(self):
        ref = self.artifact()
        self.create_run("another-run")
        self.append({"references": [ref.to_dict(), ref.to_dict()]}, "another-run")
        report = self.inspect()
        self.assertEqual(len(report.stream_cuts), 2)
        self.assertEqual(len(report.artifacts), 1)
        self.assertEqual(report.artifacts[0].ownership, "state_shared")
        self.assertEqual(report.artifacts[0].reference_count, 3)
        self.assertEqual(set(report.artifacts[0].reference_owners), {self.run_id, "another-run"})

    def test_unknown_other_stream_prevents_exclusivity(self):
        self.artifact()
        self.create_run("future-run")
        self.append({"opaque": "future reference semantics"}, "future-run", "FUTURE_ARTIFACT_LINK")
        report = self.inspect()
        self.assertFalse(report.reference_scan_complete)
        self.assertEqual(report.artifacts[0].ownership, "unknown")
        self.assertIn("UNKNOWN_EVENT_TYPE", report.holds)

    def test_unknown_artifact_version_is_held(self):
        ref = self.artifact()
        self.append({"artifact_refs": [dict(ref.to_dict(), schema_version=20)]})
        report = self.inspect()
        self.assertFalse(report.reference_scan_complete)
        self.assertIn("UNSUPPORTED_ARTIFACT_REFERENCE", report.holds)

    def test_unrecognized_root_and_control_roots_are_not_opened(self):
        for name in ("provider-preflights", "salvage", "models", "ssh", "watchers", "control", "unrecognized"):
            directory = self.state / name
            directory.mkdir()
            os.mkfifo(directory / "do-not-open")
        report = self.inspect()
        roots = {item.name: item for item in report.protected_roots}
        self.assertEqual(roots["provider-preflights"].purpose, "control_accounting_unknown_effects")
        self.assertEqual(roots["unrecognized"].ownership, "unknown")
        self.assertTrue(all(item.action == "hold" for item in roots.values()))
        self.assertIn("UNKNOWN_ROOT_HELD", report.holds)

    def test_external_paths_in_payloads_are_never_followed(self):
        fifo = self.root / "external-watch-journal"
        os.mkfifo(fifo)
        self.append({"workspace": str(self.root), "watch_source": str(fifo), "model_root": "/nonexistent/protected"})
        report = self.inspect()
        self.assertIn("external_references_not_followed", [item.name for item in report.protected_roots])

    def test_unknown_stream_contract_is_held(self):
        self.artifact()
        self.append({}, "uncreated-stream")
        report = self.inspect()
        self.assertFalse(report.reference_scan_complete)
        self.assertIn("UNKNOWN_STREAM_CONTRACT", report.holds)

    def test_missing_and_corrupt_bodies_are_not_green(self):
        ref = self.artifact()
        path = self.cas.path_for(ref.digest)
        path.unlink()
        report = self.inspect()
        self.assertFalse(report.artifacts[0].bytes_verified)
        self.assertIn("ARTIFACT_BODY_MISSING", report.artifacts[0].holds)
        path.write_bytes(b"damaged")
        report = self.inspect()
        self.assertFalse(report.artifacts[0].bytes_verified)
        self.assertIn("ARTIFACT_BODY_MISMATCH", report.artifacts[0].holds)

    def test_conflicting_sizes_do_not_hide_in_dedup(self):
        ref = self.artifact()
        self.append({"artifact_refs": [dict(ref.to_dict(), stored_bytes=100)]})
        item = self.inspect().artifacts[0]
        self.assertFalse(item.bytes_verified)
        self.assertIn("CONFLICTING_REFERENCE_SIZES", item.holds)

    def test_unsafe_blob_types_rejected_without_blocking(self):
        ref = self.artifact()
        path = self.cas.path_for(ref.digest)
        target = self.root / "outside"
        target.write_bytes(b"evidence")
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, path)
                else:
                    os.mkfifo(path)
                with self.assertRaises(RetentionError):
                    self.inspect()

    def test_symlinked_parent_rejected(self):
        ref = self.artifact()
        directory = self.cas.path_for(ref.digest).parent
        moved = self.root / "moved-prefix"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(RetentionError):
            self.inspect()

    def test_state_alias_and_linked_database_rejected(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(RetentionError):
            inspect_retention(state_dir=alias, database=alias / self.database.name, run_id=self.run_id, policy=self.policy)
        os.link(self.database, self.root / "database-hardlink")
        with self.assertRaises(RetentionError):
            self.inspect()

    def test_missing_root_database_and_outside_database_never_created(self):
        missing = self.root / "missing"
        for state, database in ((missing, missing / "database"), (self.state, self.state / "missing.sqlite3"), (self.state, self.root / "outside.sqlite3")):
            with self.assertRaises(RetentionError):
                inspect_retention(state_dir=state, database=database, run_id=self.run_id, policy=self.policy)
            self.assertFalse(database.exists())
        self.assertFalse(missing.exists())

    def test_live_wal_denied_without_source_changes(self):
        store = SQLiteEventStore(self.database)
        try:
            store.append(new_event(self.run_id, "EVIDENCE_RECORDED", "fixture", {}))
            before = self.fingerprint()
            with self.assertRaisesRegex(RetentionError, "UNSUPPORTED_LIVE_SQLITE"):
                self.inspect()
            self.assertEqual(self.fingerprint(), before)
        finally:
            store.close()

    def test_new_shared_reference_mid_inspection_invalidates_snapshot(self):
        from camol import retention
        ref = self.artifact()
        self.create_run("another-run")
        original = retention._Root.file
        fired = []
        def inject(instance, relative, maximum):
            result = original(instance, relative, maximum)
            if relative.startswith("artifacts/") and not fired:
                fired.append(True)
                self.append({"artifact_refs": [ref.to_dict()]}, "another-run")
            return result
        with patch.object(retention._Root, "file", inject):
            with self.assertRaisesRegex(RetentionError, "snapshot changed"):
                self.inspect()
        self.assertEqual(self.inspect().artifacts[0].ownership, "state_shared")

    def test_exact_plan_owner_and_run_are_required(self):
        for value in (dict(self.policy.to_dict(), owner="other-owner"),
                      dict(self.policy.to_dict(), plan_digest="sha256:" + "0" * 64),
                      dict(self.policy.to_dict(), run_id="another-run")):
            with self.subTest(value=value), self.assertRaises(RetentionError):
                self.inspect(policy=RetentionPolicy.from_dict(value))

    def test_bounded_database_events_streams_payloads_and_blobs(self):
        self.artifact()
        self.create_run("another-run")
        for kwargs in ({"max_database_bytes": 1}, {"max_events": 1}, {"max_streams": 1},
                       {"max_event_bytes": 10}, {"max_total_event_bytes": 10}, {"max_blob_bytes": 1},
                       {"max_total_blob_bytes": 1}, {"max_root_entries": 1}, {"max_json_nodes": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RetentionError):
                self.inspect(limits=InventoryLimits(**kwargs))
        with self.assertRaises(RetentionError):
            InventoryLimits(max_events=True)

    def test_duplicate_json_event_rejected_without_echoing_payload(self):
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE events SET payload_json=? WHERE seq=2", ('{"private":"secret-body","private":2}',))
        connection.commit()
        connection.close()
        with self.assertRaises(RetentionError) as caught:
            self.inspect()
        self.assertNotIn("secret-body", str(caught.exception))

    def test_readonly_database_permissions_unchanged_and_no_sidecars(self):
        self.database.chmod(0o400)
        before = self.fingerprint()
        self.inspect()
        self.assertEqual(self.fingerprint(), before)
        # Some SQLite/macOS builds retain empty sidecars after a writer closes.
        # Inspection must preserve those too, not remove them or create new ones.
        self.assertEqual(sorted(path.name for path in self.state.iterdir()),
                         sorted(name for name in before if name != "." and "/" not in name))

    def test_blob_substitution_after_initial_hash_is_rejected(self):
        from camol import retention
        ref = self.artifact()
        original = retention._Root.file
        fired = []
        def inject(instance, relative, maximum):
            result = original(instance, relative, maximum)
            if relative.startswith("artifacts/") and not fired:
                fired.append(True)
                self.cas.path_for(ref.digest).write_bytes(b"modified text")
            return result
        with patch.object(retention._Root, "file", inject):
            with self.assertRaisesRegex(RetentionError, "artifact snapshot changed"):
                self.inspect()

    def test_object_ceiling_and_future_source_truncation_are_explicit(self):
        self.artifact(b"first")
        self.artifact(b"second")
        with self.assertRaises(RetentionError):
            self.inspect(limits=InventoryLimits(max_objects=1))
        ref = self.cas.put_bytes(b"prefix", producer={"run_id": self.run_id}, source_bytes=100,
                                 source_sha256="sha256:" + "f" * 64, truncated=True, redact=False)
        self.append({"artifact_refs": [ref.to_dict()]})
        item = next(item for item in self.inspect().artifacts if item.digest == ref.digest)
        self.assertTrue(item.bytes_verified)
        self.assertFalse(item.full_source_retained)

    def test_noncontiguous_stream_rejected(self):
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE events SET seq=5 WHERE seq=2")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RetentionError, "noncontiguous"):
            self.inspect()

    def test_source_symlink_swap_before_sqlite_open_never_opens_external_db(self):
        outside = self.root / "outside.sqlite3"
        outside.write_bytes(b"NOT A DATABASE: protected external contents")
        real_connect = sqlite3.connect
        paths = []
        def swap(path, *args, **kwargs):
            paths.append(path)
            temporary = Path(unquote(urlparse(path).path))
            self.assertEqual(temporary.stat().st_mode & 0o777, 0o600)
            self.assertEqual(temporary.parent.stat().st_mode & 0o777, 0o700)
            self.database.rename(self.state / "original.sqlite3")
            self.database.symlink_to(outside)
            self.assertNotIn(str(self.state), path)
            self.assertNotIn(str(outside), path)
            return real_connect(path, *args, **kwargs)
        with patch("camol.retention.sqlite3.connect", swap):
            with self.assertRaises(RetentionError):
                self.inspect()
        self.assertEqual(len(paths), 1)
        self.assertIn("camol-retention-", paths[0])
        self.assertFalse(Path(unquote(urlparse(paths[0]).path)).parent.exists())
        self.assertEqual(outside.read_bytes(), b"NOT A DATABASE: protected external contents")

    def test_metadata_ceiling_before_rows_are_materialized(self):
        for column in ("actor_id", "event_type", "event_id", "occurred_at", "run_id", "causation_id", "correlation_id"):
            connection = sqlite3.connect(self.database)
            connection.execute("UPDATE events SET " + column + "=? WHERE seq=2", ("m" * 257,))
            connection.commit()
            connection.close()
            with self.subTest(column=column), self.assertRaisesRegex(RetentionError, "metadata"):
                self.inspect()
            connection = sqlite3.connect(self.database)
            connection.execute("UPDATE events SET " + column + "=? WHERE seq=2", (self.run_id if column == "run_id" else "fixture",))
            connection.commit()
            connection.close()


if __name__ == "__main__":
    unittest.main()
