import asyncio
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

from camol.sandbox import (
    DeveloperTrustedBackend,
    MacOSSandboxBackend,
    SandboxError,
    SandboxPolicy,
    select_backend,
    system_read_paths,
)
from camol.workspace import WorkspaceManager


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def policy(self, trust_tier="developer_sandboxed", network=()):
        return SandboxPolicy(
            policy_id="test-policy",
            workspace=str(self.workspace),
            read_paths=(str(self.workspace),) + system_read_paths(sys.executable),
            write_paths=(str(self.workspace),),
            environment_names=("PATH", "LANG"),
            network_destinations=network,
            credential_refs=(),
            trust_tier=trust_tier,
        )

    def test_policy_is_deterministic_and_rejects_fake_network_precision(self):
        first = self.policy()
        second = SandboxPolicy(
            policy_id="test-policy",
            workspace=str(self.workspace),
            read_paths=tuple(reversed(first.read_paths)),
            write_paths=(str(self.workspace),),
            environment_names=("LANG", "PATH"),
            network_destinations=(),
            credential_refs=(),
            trust_tier="developer_sandboxed",
        )
        self.assertEqual(first.digest(), second.digest())
        with self.assertRaisesRegex(SandboxError, "only denied or explicitly unrestricted"):
            self.policy(network=("api.example.com:443",))

    def test_unsandboxed_backend_cannot_claim_sandboxed_trust(self):
        with self.assertRaisesRegex(SandboxError, "cannot satisfy"):
            asyncio.run(
                DeveloperTrustedBackend().run(
                    [sys.executable, "-c", "pass"],
                    cwd=self.workspace,
                    policy=self.policy(),
                    timeout_seconds=10,
                )
            )

    def test_developer_trusted_backend_is_visibly_labeled_and_filters_environment(self):
        policy = self.policy(trust_tier="developer_trusted")
        result = asyncio.run(
            DeveloperTrustedBackend().run(
                [sys.executable, "-c", "import os; print('PATH' in os.environ, 'CAMOL_SECRET' in os.environ)"],
                cwd=self.workspace,
                policy=policy,
                timeout_seconds=10,
                environment={"CAMOL_SECRET": "not-forwarded"},
            )
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), b"True False")
        self.assertEqual(result.backend, "developer_trusted")

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_destructive_fixture_can_write_only_inside_disposable_worktree(self):
        source = self.root / "source"
        source.mkdir()
        sentinel = source / "must-not-change.txt"
        sentinel.write_text("original", encoding="utf-8")
        script = (
            "from pathlib import Path; "
            "Path('inside.txt').write_text('ok'); "
            "Path(%r).write_text('damaged')" % str(sentinel)
        )
        result = asyncio.run(
            MacOSSandboxBackend().run(
                [sys.executable, "-c", script],
                cwd=self.workspace,
                policy=self.policy(),
                timeout_seconds=10,
            )
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual((self.workspace / "inside.txt").read_text(), "ok")
        self.assertEqual(sentinel.read_text(), "original")

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_workspace_symlink_cannot_escape_write_policy(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        result = asyncio.run(
            MacOSSandboxBackend().run(
                [sys.executable, "-c", "from pathlib import Path; Path('escape/bad').write_text('bad')"],
                cwd=self.workspace,
                policy=self.policy(),
                timeout_seconds=10,
            )
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((outside / "bad").exists())

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_worker_cannot_write_source_or_integration_worktree(self):
        source = self.root / "repository"
        state = self.root / "state"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Camol Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "camol@example.invalid"], check=True)
        source_file = source / "tracked.txt"
        source_file.write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "fixture"], check=True)
        manager = WorkspaceManager(source, state)
        worker = manager.prepare_task("run", "task", "box")
        integration = manager.prepare_integration("run")
        policy = SandboxPolicy(
            policy_id="worker-policy",
            workspace=str(worker.path),
            read_paths=(str(worker.path),) + system_read_paths(sys.executable),
            write_paths=(str(worker.path),),
            environment_names=("PATH", "LANG"),
            network_destinations=(),
            credential_refs=(),
            trust_tier="developer_sandboxed",
        )
        for protected in (source_file, integration.path / "tracked.txt"):
            result = asyncio.run(
                MacOSSandboxBackend().run(
                    [sys.executable, "-c", "from pathlib import Path; Path(%r).write_text('damaged')" % str(protected)],
                    cwd=worker.path,
                    policy=policy,
                    timeout_seconds=10,
                )
            )
            self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(source_file.read_text(), "source\n")
        self.assertEqual((integration.path / "tracked.txt").read_text(), "source\n")


if __name__ == "__main__":
    unittest.main()
