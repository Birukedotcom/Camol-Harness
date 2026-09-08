import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol import Harness
from camol.artifacts import RunArchive
from camol.orchestrator import StateTransitionError
from camol.store import ReadOnlyEventStore, ReadOnlyStoreError
from camol.supervisor import SupervisorError


ROOT = Path(__file__).resolve().parents[1]


class HarnessApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "source"
        self.state_dir = root / "state"
        (self.workspace / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", self.workspace / "examples/fake_agent.py")
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("add", "."), ("commit", "-q", "-m", "fixture")):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_embedded_n_box_build_approval_export_and_reopen(self):
        with Harness(self.workspace, self.state_dir) as harness:
            state = harness.prepare(ROOT / "examples/local-n-box-runbook.json")
            with self.assertRaisesRegex(StateTransitionError, "approve the exact"):
                harness.run()
            self.assertFalse((self.state_dir / "worktrees").exists())
            with self.assertRaises(StateTransitionError):
                harness.approve(by="owner", digest="sha256:" + "0" * 64)
            harness.approve(by="owner", digest=state["plan_digest"])
            final = harness.run()
            self.assertEqual(final["status"], "completed", final.get("terminal"))
            events = harness.events()
            self.assertTrue(all(task["status"] == "succeeded" for task in final["tasks"].values()))
            self.assertGreater(harness.usage()["totals"]["accounted_tokens"], 0)
            archive = Path(self.temp.name) / "export"
            harness.export(archive)
            self.assertEqual(RunArchive.replay(archive)["status"], "completed")
            with self.assertRaises(SupervisorError):
                with Harness(self.workspace, self.state_dir):
                    self.fail("a second leader acquired the same state")
        with Harness(self.workspace, self.state_dir) as reopened:
            self.assertEqual(reopened.run()["status"], "completed")
            self.assertEqual(reopened.events(), events)
            self.assertEqual(reopened.events(after_seq=events[-1]["seq"]), [])
        source = subprocess.run(["git", "-C", str(self.workspace), "status", "--porcelain"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(source.stdout, "")

    def test_sync_entry_rejects_nested_event_loop_without_launch(self):
        async def scenario():
            with Harness(self.workspace, self.state_dir) as harness:
                harness.prepare(ROOT / "examples/local-n-box-runbook.json")
                with self.assertRaisesRegex(StateTransitionError, "run_async"):
                    harness.run()
        asyncio.run(scenario())

    def test_event_pages_are_bounded_without_starting_workers(self):
        with Harness(self.workspace, self.state_dir) as harness:
            state = harness.prepare(ROOT / "examples/local-n-box-runbook.json")
            harness.approve(by="owner", digest=state["plan_digest"])
            expected = harness.events()
            pages, cursor = [], 0
            while True:
                page = harness.events(after_seq=cursor, limit=2)
                if not page:
                    break
                self.assertLessEqual(len(page), 2)
                pages.extend(page)
                cursor = page[-1]["seq"]
            self.assertEqual(pages, expected)
            self.assertFalse((self.state_dir / "worktrees").exists())
            for invalid in (True, 0, -1, 10001, 1.5, "2"):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    harness.events(limit=invalid)

    def test_shared_external_database_cannot_bypass_state_directory_ownership(self):
        database = self.state_dir / "owned.sqlite3"
        with Harness(self.workspace, self.state_dir, database=database):
            with self.assertRaisesRegex(StateTransitionError, "leader-locked"):
                Harness(self.workspace, Path(self.temp.name) / "other-state", database=database)
            with self.assertRaises(SupervisorError):
                with Harness(self.workspace, self.state_dir, database=database):
                    self.fail("same database acquired a second execution owner")

    def test_read_only_inspection_never_creates_or_appends_database(self):
        missing = Path(self.temp.name) / "missing" / "run.db"
        with self.assertRaises(OSError):
            ReadOnlyEventStore(missing)
        self.assertFalse(missing.parent.exists())
        with Harness(self.workspace, self.state_dir) as harness:
            state = harness.prepare(ROOT / "examples/local-n-box-runbook.json")
            reader = ReadOnlyEventStore(harness.paths.database)
            try:
                self.assertEqual(reader.latest_run_id(), state["run_id"])
                with self.assertRaises(ReadOnlyStoreError):
                    reader.append_many([])
            finally:
                reader.close()

    def test_failed_open_cannot_keep_writable_store_after_releasing_leader_lock(self):
        harness = Harness(self.workspace, self.state_dir)
        with patch("camol.api.HarnessRunner", side_effect=RuntimeError("fixture startup failure")):
            with self.assertRaises(RuntimeError):
                harness.__enter__()
        self.assertIsNone(harness.store)
        self.assertIsNone(harness.orchestrator)
        with self.assertRaises(StateTransitionError):
            harness.prepare(ROOT / "examples/local-n-box-runbook.json")
        with self.assertRaises(StateTransitionError):
            harness.events()
        with Harness(self.workspace, self.state_dir) as recovered:
            self.assertIsNotNone(recovered.store)


if __name__ == "__main__":
    unittest.main()
