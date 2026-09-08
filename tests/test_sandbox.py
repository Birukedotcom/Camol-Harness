import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dataclasses import replace

from camol.sandbox import (
    DeveloperTrustedBackend,
    MacOSSandboxBackend,
    SandboxError,
    SandboxPolicy,
    system_read_paths,
)
from camol.workspace import WorkspaceManager
from camol.admission import AdmissionController


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

    def test_macos_profile_allows_only_metadata_on_runtime_ancestors(self):
        profile = MacOSSandboxBackend.profile(self.policy())
        self.assertIn("file-read-metadata", profile)
        self.assertNotIn('(allow file-read* (subpath "{}"))'.format(self.root), profile)

    def test_v1_policy_identity_is_unchanged_and_v2_roundtrips_exclusions(self):
        old = self.policy()
        serialized = old.to_dict()
        self.assertEqual(serialized["schema_version"], 1)
        self.assertNotIn("readonly_paths", serialized)
        self.assertEqual(SandboxPolicy.from_dict(serialized).digest(), old.digest())
        oracle = self.workspace / "oracle.txt"
        oracle.write_text("strict")
        policy = replace(old, readonly_paths=(str(oracle),))
        self.assertEqual(policy.to_dict()["schema_version"], 2)
        self.assertEqual(SandboxPolicy.from_dict(policy.to_dict()), policy)
        with self.assertRaisesRegex(SandboxError, "cannot enforce"):
            replace(policy, trust_tier="developer_trusted")
        forged = old.to_dict()
        forged["readonly_paths"] = [str(oracle)]
        with self.assertRaises(ValueError):
            SandboxPolicy.from_dict(forged)

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_readonly_oracle_denies_transient_writes_and_parent_replacement(self):
        oracles = self.workspace / "oracles"
        oracles.mkdir()
        oracle = oracles / "expected.txt"
        oracle.write_text("good")
        policy = replace(self.policy(), readonly_paths=(str(oracle),))
        attack = """from pathlib import Path
blocked = 0
oracle = Path('oracles/expected.txt')
for operation in (lambda: oracle.write_text('bad'), lambda: oracle.unlink(),
                  lambda: Path('oracles').rename('renamed'), lambda: oracle.rename('moved.txt')):
    try:
        operation()
    except PermissionError:
        blocked += 1
Path('unrelated.txt').write_text('allowed')
assert blocked == 4, blocked
assert oracle.read_text() == 'good'
"""
        result = asyncio.run(MacOSSandboxBackend().run([sys.executable, "-c", attack], cwd=self.workspace, policy=policy, timeout_seconds=10))
        self.assertEqual(result.exit_code, 0, result.stderr.decode())
        self.assertEqual(oracle.read_text(), "good")
        self.assertEqual((self.workspace / "unrelated.txt").read_text(), "allowed")

    def test_runtime_read_roots_follow_multihop_venv_without_home_or_root_access(self):
        installation = self.root / "managed-python"
        (installation / "bin").mkdir(parents=True)
        (installation / "lib" / "python3.12").mkdir(parents=True)
        binary = installation / "bin" / "python3.12"
        binary.write_bytes(b"fixture")
        shim = self.root / "shim-bin"
        shim.mkdir()
        (shim / "python3.12").symlink_to(binary)
        virtual = self.root / "venv"
        (virtual / "bin").mkdir(parents=True)
        (virtual / "lib").mkdir()
        (virtual / "pyvenv.cfg").write_text("home = fixture")
        (virtual / "bin" / "python").symlink_to(shim / "python3.12")
        paths = system_read_paths(str(virtual / "bin" / "python"))
        for expected in (shim, installation / "bin", installation / "lib", virtual / "lib", virtual / "pyvenv.cfg"):
            self.assertIn(str(expected), paths)
        self.assertNotIn("/", paths)
        self.assertNotIn(str(Path.home()), paths)
        self.assertNotIn(str(self.root), paths)

    def test_xcode_selector_adds_only_trusted_selected_runtime_and_link_metadata(self):
        from camol import sandbox
        applications = self.root / "Applications"
        developer = applications / "Xcode_26.app" / "Contents" / "Developer"
        developer.mkdir(parents=True)
        selected = applications / "Xcode.app"
        selected.symlink_to(developer.parents[1], target_is_directory=True)
        selector = self.root / "private" / "var" / "select" / "developer_dir"
        selector.parent.mkdir(parents=True)
        selector.symlink_to(selected / "Contents" / "Developer", target_is_directory=True)
        real_lstat = Path.lstat

        def root_owned(path):
            value = list(real_lstat(path))
            value[4] = 0  # deterministic CI-like root ownership; no OS mutations
            return os.stat_result(value)

        with patch.object(sandbox.sys, "platform", "darwin"), patch.object(sandbox, "_MACOS_APPLICATIONS", applications), patch.object(
                sandbox, "_MACOS_DEVELOPER_SELECTOR", selector), patch.object(Path, "lstat", root_owned):
            with patch.dict(os.environ, {"DEVELOPER_DIR": str(self.root / "untrusted-home-override")}):
                paths = system_read_paths(sys.executable)
            self.assertIn(str(developer), paths)
            self.assertIn(str(selector.parent), paths)
            self.assertNotIn(str(applications), paths)
            self.assertNotIn(str(self.root / "private" / "var"), paths)
            self.assertNotIn(str(self.root), paths)
            profile = MacOSSandboxBackend.profile(replace(self.policy(), read_paths=paths + (str(self.workspace),)))
            self.assertIn('(allow file-read-metadata (subpath "{}"))'.format(selector.parent), profile)
            self.assertNotIn('(allow file-read* (subpath "{}"))'.format(selector.parent), profile)
            self.assertIn('(allow file-read* (subpath "{}"))'.format(developer), profile)
            self.assertIn('(allow file-read-metadata (literal "{}"))'.format(selected), profile)
            self.assertNotIn('(allow file-read* (subpath "{}"))'.format(selected), profile)
            developer.chmod(0o777)
            with self.assertRaisesRegex(SandboxError, "root-owned"):
                system_read_paths(sys.executable)
            developer.chmod(0o755)
            selector.unlink()
            selector.symlink_to(self.root / "user-models")
            with self.assertRaisesRegex(SandboxError, "outside supported"):
                system_read_paths(sys.executable)

    def test_xcode_selector_user_owned_and_cyclic_targets_fail_closed(self):
        from camol import sandbox
        applications = self.root / "Applications"
        applications.mkdir()
        first, second = applications / "One.app", applications / "Two.app"
        first.symlink_to(second)
        second.symlink_to(first)
        selector = self.root / "selector" / "developer_dir"
        selector.parent.mkdir()
        selector.symlink_to(first / "Contents" / "Developer")
        real_lstat = Path.lstat

        def fake_owner(path, uid):
            value = list(real_lstat(path))
            value[4] = uid
            return os.stat_result(value)

        with patch.object(sandbox.sys, "platform", "darwin"), patch.object(sandbox, "_MACOS_APPLICATIONS", applications), patch.object(
                sandbox, "_MACOS_DEVELOPER_SELECTOR", selector):
            with patch.object(Path, "lstat", lambda path: fake_owner(path, 501)):
                with self.assertRaisesRegex(SandboxError, "root-owned"):
                    system_read_paths(sys.executable)
            with patch.object(Path, "lstat", lambda path: fake_owner(path, 0)):
                with self.assertRaisesRegex(SandboxError, "cyclic"):
                    system_read_paths(sys.executable)

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_xcode_style_selector_metadata_and_selected_content_are_enforced(self):
        from camol import sandbox
        applications = self.root / "Applications"
        developer = applications / "Xcode_26.app" / "Contents" / "Developer"
        developer.mkdir(parents=True)
        (developer / "tool").write_text("selected toolchain")
        alias = applications / "Xcode.app"
        alias.symlink_to(developer.parents[1])
        selector = self.root / "private" / "var" / "select" / "developer_dir"
        selector.parent.mkdir(parents=True)
        selector.symlink_to(alias / "Contents" / "Developer")
        (selector.parent / "unrelated-secret").write_text("selector sibling must stay hidden")
        other = applications / "Other.app"
        other.mkdir()
        (other / "secret").write_text("unselected applications must stay hidden")
        real_lstat = Path.lstat

        def root_owned(path):
            value = list(real_lstat(path))
            value[4] = 0  # fixture ownership only; no privileged filesystem edits
            return os.stat_result(value)

        script = """import os,pathlib
selector=pathlib.Path(%r)
selected=pathlib.Path(os.readlink(selector))
assert (selected/'tool').read_text() == 'selected toolchain'
assert os.readlink(%r)
denied=0
for file in (selector.parent/'unrelated-secret', pathlib.Path(%r)):
    try: file.read_text()
    except PermissionError: denied += 1
assert denied == 2, denied
""" % (str(selector), str(alias), str(other / "secret"))
        with patch.object(sandbox, "_MACOS_APPLICATIONS", applications), patch.object(sandbox, "_MACOS_DEVELOPER_SELECTOR", selector), patch.object(
                Path, "lstat", root_owned):
            result = asyncio.run(MacOSSandboxBackend().run([sys.executable, "-c", script], cwd=self.workspace,
                                                          policy=self.policy(), timeout_seconds=10))
        self.assertEqual(result.exit_code, 0, result.stderr.decode())

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

    def test_fast_exit_is_recorded_when_os_fingerprint_misses_the_process(self):
        policy = self.policy(trust_tier="developer_trusted")
        record = self.workspace / "fast.invocation.json"
        with patch(
            "camol.sandbox.process_start_fingerprint",
            side_effect=SandboxError("cannot establish process-start identity"),
        ):
            result = asyncio.run(
                DeveloperTrustedBackend().run(
                    [sys.executable, "-c", "pass"],
                    cwd=self.workspace,
                    policy=policy,
                    timeout_seconds=10,
                    invocation_record=record,
                )
            )
        persisted = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(persisted["state"], "completed")
        self.assertEqual(persisted["process_started"], "exited-before-os-observation")

    def test_stdin_backpressure_is_bounded_and_reaps_the_child(self):
        record = self.workspace / "blocked-input.invocation.json"

        async def scenario():
            with self.assertRaisesRegex(SandboxError, "timed out"):
                await asyncio.wait_for(
                    DeveloperTrustedBackend().run(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=self.workspace, policy=self.policy(trust_tier="developer_trusted"),
                        timeout_seconds=0.2, stdin_bytes=b"x" * (2 << 20),
                        invocation_record=record,
                    ), timeout=3,
                )
            payload = json.loads(record.read_text())
            self.assertEqual(payload["state"], "timed_out")
            with self.assertRaises(ProcessLookupError):
                os.kill(payload["pid"], 0)

        asyncio.run(scenario())

    def test_cancellation_during_stdin_backpressure_reaps_the_child(self):
        record = self.workspace / "cancel-input.invocation.json"

        async def scenario():
            pending = asyncio.create_task(DeveloperTrustedBackend().run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=self.workspace, policy=self.policy(trust_tier="developer_trusted"),
                timeout_seconds=30, stdin_bytes=b"x" * (2 << 20), invocation_record=record,
            ))
            for _ in range(100):
                if record.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(record.exists())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            payload = json.loads(record.read_text())
            self.assertEqual(payload["state"], "terminated")
            with self.assertRaises(ProcessLookupError):
                os.kill(payload["pid"], 0)

        asyncio.run(scenario())

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
    def test_worker_output_grant_cannot_overwrite_or_unlink_control_evidence(self):
        controller = AdmissionController.__new__(AdmissionController)
        controller.state_dir = self.root / "state"
        controller.runbook = {"run": {"id": "run"}}
        agent = dict(id="worker", adapter=dict(kind="process", argv=[sys.executable]), trust_tier="developer_sandboxed")
        policy = controller._sandbox_policy(SimpleNamespace(path=self.workspace), {"id": "task"}, agent)
        packets = controller.state_dir / "packets" / "run" / "task"
        protected = [packets / name for name in ("turn-001.packet.json", "turn-001.provider-result.json", "turn-001.charge-pending.json", "turn-001.invocation.json")]
        for path in protected:
            path.write_text("trusted")
        output = packets / "worker-output" / "turn-001.result.json"
        invocation = packets / "kernel-record.json"
        script = """from pathlib import Path
import time
for _ in range(200):
    if Path(%r).exists():
        break
    time.sleep(0.005)
blocked = 0
for name in %r:
    path = Path(name)
    for operation in (lambda: path.write_text('forged'), lambda: path.unlink()):
        try:
            operation()
        except PermissionError:
            blocked += 1
Path(%r).write_text('untrusted-result')
Path('inside.txt').write_text('allowed')
assert blocked == 10, blocked
""" % (str(invocation), [str(path) for path in protected + [invocation]], str(output))
        backend = MacOSSandboxBackend()
        backend.invocation_root = packets
        result = asyncio.run(backend.run([sys.executable, "-c", script], cwd=self.workspace,
            policy=policy, timeout_seconds=10, invocation_record=invocation))
        self.assertEqual(result.exit_code, 0, result.stderr.decode())
        self.assertEqual(output.read_text(), "untrusted-result")
        self.assertEqual((self.workspace / "inside.txt").read_text(), "allowed")
        self.assertTrue(all(path.read_text() == "trusted" for path in protected))
        self.assertEqual(json.loads(invocation.read_text())["state"], "completed")
        self.assertNotIn(str(packets), policy.write_paths)

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
