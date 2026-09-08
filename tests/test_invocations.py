import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path

from camol.adapter import AdapterError
from camol.invocations import InvocationJournal


class InvocationJournalTests(unittest.TestCase):
    def test_exactly_one_concurrent_claim_and_immutable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "charge.json"
            kwargs = dict(packet_sha256="a" * 64, profile_digest="sha256:" + "b" * 64,
                          assignment=dict(task_id="task", agent_id="worker", lease_id="lease", fence_digest="sha256:" + "c" * 64),
                          run_id="run", turn_number=1, workspace=temporary)
            def claim(_):
                try:
                    InvocationJournal(path, **kwargs).reserve([])
                    return True
                except AdapterError:
                    return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                self.assertEqual(sum(executor.map(claim, range(8))), 1)
            original = path.read_bytes()
            journal = InvocationJournal(path, **kwargs)
            journal.record_outcome([{"known": "terminal"}])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(AdapterError) as caught:
                journal.reserve([])
            self.assertEqual(caught.exception.observed_evidence, [{"known": "terminal"}])
            for changed in (dict(kwargs, packet_sha256="d" * 64), dict(kwargs, profile_digest="sha256:" + "e" * 64), dict(kwargs, turn_number=2)):
                with self.assertRaisesRegex(AdapterError, "another subject"):
                    InvocationJournal(path, **changed).reserve([])

    def test_symlink_or_partial_intent_never_authorizes_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("partial")
            path = root / "charge.json"
            path.symlink_to(target)
            journal = InvocationJournal(path, packet_sha256="a" * 64, profile_digest="sha256:" + "b" * 64,
                assignment=dict(task_id="task", agent_id="worker", lease_id="lease"), run_id="run", turn_number=1, workspace=temporary)
            with self.assertRaises(AdapterError):
                journal.reserve([])
            self.assertEqual(target.read_text(), "partial")

    def test_fifo_intent_fails_closed_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "charge.json"
            os.mkfifo(path)
            journal = InvocationJournal(path, packet_sha256="a" * 64, profile_digest="sha256:" + "b" * 64,
                assignment=dict(task_id="task", agent_id="worker", lease_id="lease"), run_id="run", turn_number=1, workspace=temporary)
            with self.assertRaises(AdapterError):
                journal.reserve([])
