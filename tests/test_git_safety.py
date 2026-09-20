"""Repository config must never make host-side Git plumbing execute source."""

import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.doctor import DoctorOptions, run_doctor
from camol.git_safety import GitSafetyError, safe_git_argv
from camol.probes import GIT_SAFETY_ARGS, run_command, sanitized_environment
from camol.git_view import prepare_view
from camol.repository_graph import crawl_repository
from camol.workspace import WorkspaceError, WorkspaceManager
from tests.test_probes import make_repo


class GitCallbackSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = make_repo(self.root)
        self.state = self.root / "state"
        self.state.mkdir()
        self.sentinel = self.root / "executed"
        self.tracked = self.repo / "tracked.txt"
        self.tracked.write_text("original\n")
        (self.repo / ".gitattributes").write_text("tracked.txt filter=trap diff=trap\n")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "callback fixture")
        self.trap = self.root / "trap.py"
        self.trap.write_text("import pathlib,sys\npathlib.Path({!r}).write_text('executed')\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n".format(str(self.sentinel)))
        self.command = shlex.join([sys.executable, str(self.trap)])

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def configure_traps(self):
        for key in ("filter.trap.clean", "filter.trap.smudge", "filter.trap.process", "diff.trap.command", "diff.trap.textconv"):
            self.git("config", key, self.command)
        self.git("config", "filter.trap.required", "true")

    def mutate_same_size_and_mtime(self):
        before = self.tracked.stat()
        self.tracked.write_text("mutation\n")
        os.utime(self.tracked, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.tracked.stat().st_size)
        self.assertEqual(before.st_mtime_ns, self.tracked.stat().st_mtime_ns)

    def test_doctor_and_graph_cannot_execute_clean_process_or_diff_callbacks(self):
        self.configure_traps()
        self.mutate_same_size_and_mtime()
        report = run_doctor(DoctorOptions(runbook=self.repo / "examples/three-agent-runbook.json", workspace=self.repo,
            state_dir=self.state, json_output=True, target_id="local:test"), stdout=io.StringIO())
        self.assertEqual(report.exit_code, 2)
        graph = crawl_repository(self.repo)
        self.assertTrue(graph.snapshot_id)
        self.assertFalse(self.sentinel.exists())
        with self.assertRaisesRegex(WorkspaceError, "dirty"):
            WorkspaceManager(self.repo, self.state).prepare_task("r", "t", "b")
        self.assertFalse(self.sentinel.exists())

    def test_checkout_salvage_and_integration_disable_every_configured_driver(self):
        self.configure_traps()
        manager = WorkspaceManager(self.repo, self.state)
        task = manager.prepare_task("r", "t", "b")
        self.assertEqual((task.path / "tracked.txt").read_text(), "original\n")
        (task.path / "tracked.txt").write_text("mutation\n")
        salvage = manager.salvage(task)
        self.assertGreater(salvage.patch_bytes, 0)
        integration = manager.prepare_integration_generation("r", "t", "candidate", base_revision=task.receipt.base_revision)
        manager.materialize_candidate(salvage, integration)
        manager.commit_workspace(integration, "approved integration")
        self.assertEqual((integration.path / "tracked.txt").read_text(), "mutation\n")
        self.assertFalse(self.sentinel.exists())

    def test_worktree_conditional_include_is_inspected_before_materialization(self):
        included = self.root / "worktree-config"
        subprocess.run(["git", "config", "--file", str(included), "filter.trap.smudge", self.command], check=True)
        self.git("config", "includeIf.onbranch:camol/**.path", str(included))
        task = WorkspaceManager(self.repo, self.state).prepare_task("r", "t", "b")
        self.assertEqual((task.path / "tracked.txt").read_text(), "original\n")
        self.assertFalse(self.sentinel.exists())

    def test_included_malformed_driver_key_fails_closed_without_execution(self):
        included = self.root / "hostile-config"
        subprocess.run(["git", "config", "--file", str(included), "filter.bad name.clean", self.command], check=True)
        self.git("config", "include.path", str(included))
        with self.assertRaisesRegex(GitSafetyError, "unsupported driver"):
            safe_git_argv(shutil.which("git"), ["-C", str(self.repo), "status", "--porcelain"], env=sanitized_environment())
        self.assertFalse(self.sentinel.exists())

    def test_index_visibility_opt_outs_cannot_hide_same_size_mutation(self):
        self.git("config", "core.trustctime", "false")
        self.git("config", "core.checkStat", "minimal")
        for flag, undo in (("--assume-unchanged", "--no-assume-unchanged"), ("--skip-worktree", "--no-skip-worktree")):
            self.git("update-index", flag, "tracked.txt")
            self.mutate_same_size_and_mtime()
            with self.assertRaisesRegex(WorkspaceError, "hide source changes"):
                WorkspaceManager(self.repo, self.state).assert_source_ready()
            self.git("update-index", undo, "tracked.txt")
        self.assertFalse(self.sentinel.exists())

    def test_replacement_refs_cannot_substitute_approved_commit_bytes(self):
        approved = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.tracked.write_text("substituted\n")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "replacement")
        replacement = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("replace", approved, replacement)
        # Ordinary Git demonstrates the attack while trusted plumbing ignores it.
        self.assertEqual(self.git("show", approved + ":tracked.txt").stdout, b"substituted\n")
        arguments = ["-C", str(self.repo), "show", approved + ":tracked.txt"]
        direct = subprocess.check_output(safe_git_argv(shutil.which("git"), arguments, env=sanitized_environment()))
        self.assertEqual(direct, b"original\n")
        # Doctor feeds static safety arguments back through the centralized parser.
        observed = run_command([shutil.which("git"), *GIT_SAFETY_ARGS, *arguments], None, 20)
        self.assertEqual(observed.exit_code, 0)
        self.assertEqual(observed.stdout, "original\n")
        task = WorkspaceManager(self.repo, self.state).prepare_task("r", "t", "b", base_revision=approved)
        self.assertEqual(task.receipt.base_revision, approved)
        self.assertEqual((task.path / "tracked.txt").read_text(), "original\n")
        _, manifest = prepare_view(self.repo, self.state, task)
        self.assertEqual(manifest["base_revision"], approved)


if __name__ == "__main__":
    unittest.main()
