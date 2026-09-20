import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from camol.workspace import SalvageReceipt, WorkspaceError, WorkspaceManager


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Camol Test")
        git(self.source, "config", "user.email", "camol@example.invalid")
        (self.source / "tracked.txt").write_text("source\n", encoding="utf-8")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-q", "-m", "fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def test_task_and_integration_worktrees_are_distinct_and_idempotent(self):
        manager = WorkspaceManager(self.source, self.state)
        task = manager.prepare_task("run-1", "task-1", "box-1", created_at="2026-09-03T00:00:00Z")
        same = manager.prepare_task("run-1", "task-1", "box-1")
        integration = manager.prepare_integration("run-1", created_at="2026-09-03T00:00:00Z")

        self.assertEqual(task.receipt, same.receipt)
        self.assertNotEqual(task.path, integration.path)
        self.assertNotEqual(task.branch, integration.branch)
        self.assertEqual(task.receipt.filesystem_policy, "isolated_worktree_write")
        self.assertEqual(task.receipt.cleanup_owner, "camol")
        self.assertEqual((self.source / "tracked.txt").read_text(), "source\n")

    def test_dirty_source_nested_state_symlink_escape_and_branch_collision_are_rejected(self):
        (self.source / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "dirty"):
            WorkspaceManager(self.source, self.state).prepare_task("run", "task", "box")
        (self.source / "dirty.txt").unlink()

        with self.assertRaisesRegex(WorkspaceError, "outside"):
            WorkspaceManager(self.source, self.source / ".camol")

        outside = self.root / "outside"
        outside.mkdir()
        symlink = outside / "state-link"
        symlink.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, "outside"):
            WorkspaceManager(self.source, symlink)

        manager = WorkspaceManager(self.source, self.state)
        git(self.source, "branch", manager._branch_name("run", "task", "box"))
        with self.assertRaisesRegex(WorkspaceError, "branch already exists"):
            manager.prepare_task("run", "task", "box")

    def test_same_run_task_box_in_distinct_state_directories_have_distinct_refs(self):
        first = WorkspaceManager(self.source, self.state).prepare_task("run", "task", "box")
        other_state = self.root / "other-state"
        second = WorkspaceManager(self.source, other_state).prepare_task("run", "task", "box")
        self.assertNotEqual(first.branch, second.branch)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(WorkspaceManager(self.source, self.state).prepare_task("run", "task", "box"), first)
        self.assertEqual(WorkspaceManager(self.source, other_state).prepare_task("run", "task", "box"), second)

    def test_base_revision_is_exact_and_source_is_never_worker_cwd(self):
        base = git(self.source, "rev-parse", "HEAD")
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box", base_revision=base)
        self.assertEqual(handle.receipt.base_revision, base)
        self.assertEqual(git(handle.path, "rev-parse", "HEAD"), base)
        self.assertNotEqual(handle.path, self.source.resolve())

        with self.assertRaisesRegex(WorkspaceError, "safe identifier"):
            manager.prepare_task("run", "../escape", "box")

    def test_salvage_captures_diff_and_untracked_content_before_cleanup(self):
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box")
        (handle.path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (handle.path / "new.txt").write_text("untracked\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "salvage"):
            manager.cleanup(
                handle,
                type("Fake", (), {"to_dict": lambda self: {}, "workspace_digest": handle.receipt.digest()})(),
            )
        salvage = manager.salvage(handle, created_at="2026-09-03T00:00:00Z")
        self.assertGreater(salvage.patch_bytes, 0)
        self.assertEqual([item["path"] for item in salvage.untracked], ["new.txt"])
        self.assertEqual(SalvageReceipt.from_dict(salvage.to_dict()), salvage)
        corrupt = salvage.to_dict()
        corrupt["untracked"][0]["path"] = "../escape"
        with self.assertRaisesRegex(WorkspaceError, "workspace-relative"):
            SalvageReceipt.from_dict(corrupt)
        manager.cleanup(handle, salvage)
        self.assertFalse(handle.path.exists())
        self.assertEqual((self.source / "tracked.txt").read_text(), "source\n")

    def test_cleanup_rejects_changes_made_after_salvage(self):
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box")
        salvage = manager.salvage(handle, created_at="2026-09-03T00:00:00Z")
        (handle.path / "late.txt").write_text("not yet salvaged", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "changed after salvage"):
            manager.cleanup(handle, salvage)
        self.assertTrue(handle.path.exists())

    def test_worker_commit_cannot_hide_changes_from_candidate_salvage(self):
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box")
        (handle.path / "tracked.txt").write_text("committed by worker\n", encoding="utf-8")
        git(handle.path, "add", "tracked.txt")
        git(handle.path, "commit", "-q", "-m", "worker commit")
        salvage = manager.salvage(handle, created_at="2026-09-03T00:00:00Z")
        self.assertGreater(salvage.patch_bytes, 0)
        verifier = manager.prepare_verifier(
            "run", "task", "candidate-proof", base_revision=handle.receipt.base_revision,
        )
        manager.materialize_candidate(salvage, verifier)
        self.assertEqual((verifier.path / "tracked.txt").read_text(encoding="utf-8"), "committed by worker\n")

    def test_integration_generation_role_is_persisted_and_reloaded(self):
        manager = WorkspaceManager(self.source, self.state)
        base = git(self.source, "rev-parse", "HEAD")
        generation = manager.prepare_integration_generation(
            "run", "task", "candidate-proof", base_revision=base,
        )
        again = manager.prepare_integration_generation(
            "run", "task", "candidate-proof", base_revision=base,
        )
        record = json.loads(
            (self.state / "records" / "workspaces" / (generation.receipt.workspace_id + ".json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(generation.integration)
        self.assertTrue(again.integration)
        self.assertTrue(record["integration"])

    def test_integration_commit_inherits_base_identity_without_global_or_local_config(self):
        git(self.source, "config", "--unset", "user.name")
        git(self.source, "config", "--unset", "user.email")
        manager = WorkspaceManager(self.source, self.state)
        base = git(self.source, "rev-parse", "HEAD")
        generation = manager.prepare_integration_generation(
            "run", "task", "candidate-proof", base_revision=base,
        )
        (generation.path / "tracked.txt").write_text("integrated\n", encoding="utf-8")
        revision = manager.commit_workspace(generation, "integration proof")
        identity = git(generation.path, "show", "-s", "--format=%an%n%ae", revision).splitlines()
        self.assertEqual(identity, ["Camol Test", "camol@example.invalid"])

    def test_cleanup_never_destroys_adopted_workspace(self):
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box")
        adopted = replace(handle, receipt=replace(handle.receipt, cleanup_owner="adopted"))
        salvage = manager.salvage(handle)
        with self.assertRaisesRegex(WorkspaceError, "never destroyed"):
            manager.cleanup(adopted, salvage)
        self.assertTrue(handle.path.exists())

    def test_untracked_symlink_is_not_followed_during_salvage(self):
        manager = WorkspaceManager(self.source, self.state)
        handle = manager.prepare_task("run", "task", "box")
        secret = self.root / "secret.txt"
        secret.write_text("do not copy", encoding="utf-8")
        (handle.path / "escape.txt").symlink_to(secret)
        with self.assertRaisesRegex(WorkspaceError, "unsafe untracked"):
            manager.salvage(handle)


if __name__ == "__main__":
    unittest.main()
