import stat
import tempfile
import unittest
from pathlib import Path

from camol.schema import canonical_digest
from camol.session import MAX_SESSION_BYTES, SessionError, SessionStore, validate_session


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.state_root = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def test_session_is_outside_workspace_private_and_durable(self):
        store = SessionStore(self.workspace, self.state_root)
        session = store.create()
        session = store.append_message(
            session,
            "human",
            "use sk-live-abcdefghijklmnopqrstuvwxyz123456",
        )

        self.assertFalse(store.path.is_relative_to(self.workspace))
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", store.path.read_text())
        self.assertEqual(store.load()["session_id"], session["session_id"])

    def test_plan_mutation_invalidates_digest_and_approval(self):
        store = SessionStore(self.workspace, self.state_root)
        session = store.create()
        plan = {"schema": "camol.plan_proposal", "schema_version": 1, "goal": "ship"}
        digest = canonical_digest(plan)
        approved = store.update(
            session, plan=plan, plan_digest=digest, approved_digest=digest, status="approved"
        )
        changed = dict(approved)
        changed["plan"] = {**plan, "goal": "different"}

        with self.assertRaisesRegex(SessionError, "digest"):
            validate_session(changed)

    def test_symlinked_project_state_is_rejected(self):
        store = SessionStore(self.workspace, self.state_root)
        store.project_dir.parent.mkdir(parents=True)
        target = self.root / "target"
        target.mkdir()
        store.project_dir.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(SessionError, "symlink"):
            store.create()

    def test_unknown_session_field_is_rejected(self):
        store = SessionStore(self.workspace, self.state_root)
        session = store.create()
        session["credential"] = "not allowed"

        with self.assertRaises(Exception):
            validate_session(session)

    def test_save_trims_old_messages_to_its_reload_byte_limit(self):
        store = SessionStore(self.workspace, self.state_root)
        session = store.create()
        session["messages"] = [
            {
                "message_id": "message-{}".format(index),
                "role": "human",
                "content": "{}:".format(index) + "x" * 200_000,
                "created_at": "2026-09-03T00:00:00+00:00",
                "kind": "conversation",
            }
            for index in range(15)
        ]
        session = store.save(session)
        self.assertLessEqual(store.path.stat().st_size, MAX_SESSION_BYTES)
        reloaded = store.load()
        self.assertLess(len(reloaded["messages"]), 15)
        self.assertTrue(reloaded["messages"][-1]["content"].startswith("14:"))


if __name__ == "__main__":
    unittest.main()
