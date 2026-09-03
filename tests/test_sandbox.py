import asyncio
import hashlib
import json
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

    def test_invocation_record_is_completed_and_cancellation_kills_process_group(self):
        policy = self.policy(trust_tier="developer_trusted")
        completed_record = self.workspace / "completed.invocation.json"
        result = asyncio.run(
            DeveloperTrustedBackend().run(
                [sys.executable, "-c", "print('done')"],
                cwd=self.workspace,
                policy=policy,
                timeout_seconds=10,
                invocation_record=completed_record,
            )
        )
        completed = json.loads(completed_record.read_text(encoding="utf-8"))
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["pid"], result.process_id)
        self.assertTrue(completed["process_started"])

        cancelled_record = self.workspace / "cancelled.invocation.json"

        async def cancel_running_process():
            task = asyncio.create_task(
                DeveloperTrustedBackend().run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=self.workspace,
                    policy=policy,
                    timeout_seconds=60,
                    invocation_record=cancelled_record,
                )
            )
            for _ in range(100):
                if cancelled_record.exists():
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(cancelled_record.exists())
            active = json.loads(cancelled_record.read_text(encoding="utf-8"))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(active["pid"], 0)

        asyncio.run(cancel_running_process())
        cancelled = json.loads(cancelled_record.read_text(encoding="utf-8"))
        self.assertEqual(cancelled["state"], "terminated")

    def test_process_streams_are_drained_but_retention_is_bounded(self):
        policy = self.policy(trust_tier="developer_trusted")
        size = (1 << 20) + 8192
        result = asyncio.run(
            DeveloperTrustedBackend().run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * {}); sys.stderr.buffer.write(b'y' * {})".format(size, size),
                ],
                cwd=self.workspace,
                policy=policy,
                timeout_seconds=20,
            )
        )
        self.assertEqual(len(result.stdout), 1 << 20)
        self.assertEqual(len(result.stderr), 1 << 20)
        self.assertEqual(result.stdout_bytes, size)
        self.assertEqual(result.stderr_bytes, size)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertEqual(result.stdout_sha256, "sha256:" + hashlib.sha256(b"x" * size).hexdigest())
        self.assertEqual(result.stderr_sha256, "sha256:" + hashlib.sha256(b"y" * size).hexdigest())

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
