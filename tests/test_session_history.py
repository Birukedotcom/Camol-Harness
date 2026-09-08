import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.session import MAX_HISTORY_READ_BYTES, SessionError, SessionStore


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.store = SessionStore(self.workspace, self.root / "state")
        self.session = self.store.create()

    def row(self, index, content=None):
        return dict(message_id="message-" + str(index), role="human", content=content or "entry-" + str(index),
                    created_at="2026-09-07T00:00:00+00:00", kind="conversation")

    def write_rows(self, rows):
        self.store.transcript_path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        self.store.transcript_path.chmod(0o600)

    def test_long_archive_recent_history_reads_only_the_bounded_tail(self):
        # Sparse old bytes are deliberately not parsed: history is a recent view,
        # not a claim that old records have been verified or retained correctly.
        with self.store.transcript_path.open("wb") as stream:
            stream.seek(128 << 20)
            stream.write(b"\n")
            for index in range(30):
                stream.write(json.dumps(self.row(index)).encode() + b"\n")
        self.store.transcript_path.chmod(0o600)
        original, read_sizes = os.pread, []
        def measured(descriptor, size, offset):
            read_sizes.append(size)
            return original(descriptor, size, offset)
        with patch("camol.session.os.pread", side_effect=measured):
            rows = self.store.history([], 20)
        self.assertEqual([row["content"] for row in rows], ["entry-" + str(i) for i in range(10, 30)])
        self.assertLessEqual(sum(read_sizes), 65536)
        self.assertEqual(self.store.transcript_path.stat().st_size, (128 << 20) + 1 + sum(len(json.dumps(self.row(i)).encode()) + 1 for i in range(30)))

    def test_unicode_rows_cross_chunk_boundaries_without_partial_json_or_lost_entries(self):
        self.write_rows([self.row(i, str(i) + "🦙" * 6000) for i in range(5)])
        rows = self.store.history([], 3)
        self.assertEqual([row["message_id"] for row in rows], ["message-2", "message-3", "message-4"])
        self.assertEqual(rows[0]["content"], "2" + "🦙" * 6000)

    def test_duplicate_nonfinite_partial_and_nonmessage_records_fail_closed(self):
        for raw in (
            b'{"role":"human","role":"system"}\n', b'{"value":NaN}\n',
            json.dumps(self.row(1)).encode(), b'[]\n', b'{"role":[]}\n', b'\xff\n',
        ):
            with self.subTest(raw=raw):
                self.store.transcript_path.write_bytes(raw)
                self.store.transcript_path.chmod(0o600)
                with self.assertRaises(SessionError):
                    self.store.history([])

    def test_fifo_symlink_and_public_files_are_denied_without_blocking_or_mutation(self):
        path = self.store.transcript_path
        os.mkfifo(path, 0o600)
        began = time.monotonic()
        with self.assertRaises(SessionError): self.store.history([])
        self.assertLess(time.monotonic() - began, 1)
        path.unlink()
        target = self.root / "outside"
        target.write_text("outside remains unchanged")
        path.symlink_to(target)
        with self.assertRaises(SessionError): self.store.history([])
        self.assertEqual(target.read_text(), "outside remains unchanged")
        path.unlink()
        self.write_rows([self.row(1)])
        path.chmod(0o644)
        with self.assertRaises(SessionError): self.store.history([])
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_explicit_byte_ceiling_and_concurrent_growth_do_not_return_partial_success(self):
        path = self.store.transcript_path
        path.write_bytes(b" " * (MAX_HISTORY_READ_BYTES + 1) + b"\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(SessionError, "ceiling"):
            self.store.history([], 1)
        self.write_rows([self.row(1)])
        original = os.pread
        def raced(descriptor, size, offset):
            data = original(descriptor, size, offset)
            with path.open("ab") as stream: stream.write(b"\n")
            return data
        with patch("camol.session.os.pread", side_effect=raced), self.assertRaisesRegex(SessionError, "changed"):
            self.store.history([], 1)

    def test_legacy_fallback_and_counts_do_not_create_an_archive(self):
        self.assertEqual(self.store.history([self.row(1), self.row(2)], 1), [self.row(2)])
        self.assertFalse(self.store.transcript_path.exists())
        for count in (True, 0, -1, 102, "20"):
            with self.subTest(count=count), self.assertRaises(SessionError):
                self.store.history([], count)
