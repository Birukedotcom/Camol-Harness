import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.events import new_event
from camol.store import SQLiteEventStore


class SQLiteEventStoreSecurityTests(unittest.TestCase):
    def test_database_and_sidecars_are_owner_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o755)
            database = root / "events.sqlite3"
            database.touch(mode=0o644)
            os.chmod(database, 0o644)
            store = SQLiteEventStore(database)
            try:
                for path in (
                    database,
                    Path(str(database) + "-wal"),
                    Path(str(database) + "-shm"),
                ):
                    self.assertTrue(path.exists(), path)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
            finally:
                store.close()

    def test_database_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.txt"
            target.write_text("do not replace", encoding="utf-8")
            database = root / "events.sqlite3"
            database.symlink_to(target)

            with self.assertRaises(OSError):
                SQLiteEventStore(database)

            self.assertEqual(target.read_text(encoding="utf-8"), "do not replace")

    def test_permission_failure_rolls_back_before_append_is_committed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteEventStore(Path(temporary) / "events.sqlite3")
            event = new_event("run-security", "RUN_STARTED", "orchestrator", {})
            try:
                with patch.object(
                    store,
                    "_harden_database_files",
                    side_effect=OSError("permission hardening failed"),
                ):
                    with self.assertRaisesRegex(OSError, "permission hardening failed"):
                        store.append(event)
                self.assertEqual(store.read("run-security"), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
