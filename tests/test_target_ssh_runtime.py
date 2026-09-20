"""Real local bridge processes; no external SSH host, account or model call."""

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import camol
from camol.schema import canonical_digest
from camol.ssh_protocol import SSHTarget, DEFAULT_READ_COMMANDS
from camol.ssh_transport import SSHControlClient
from camol.state import apply_event, project
from camol.target_runtime import local_profile
from camol.target_ssh_runtime import COMMAND, MEANING
from camol.targets import TargetError
from camol.supervisor import Supervisor, SupervisorError, send_control, send_control_v2
from tests import test_ssh_transport as transport_fixture
from tests import test_targets as target_fixture


class SSHRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.remote = transport_fixture.SSHTransportTests()
        await self.remote.asyncSetUp()
        self.addAsyncCleanup(self.remote.asyncTearDown)
        self.runtime_root = Path(camol.__file__).resolve().parent.parent
        # Installed-wheel checks must not redirect children back into source.
        with patch.object(transport_fixture, "ROOT", self.runtime_root):
            self.remote.install_fake()
        self.local = target_fixture.TargetTests()
        self.local.setUp()
        self.addCleanup(self.local.doCleanups)
        remote = self.remote
        remote.supervisor.workspace = remote.root
        remote.supervisor.paths = remote.paths
        remote.supervisor._target_profile_task = None
        remote.supervisor._shutdown = asyncio.Event()
        remote.binding["allowed_commands"].append(COMMAND)
        remote.write_policy()
        value = remote.profile.to_dict()
        value.update(allowed_commands=remote.binding["allowed_commands"], target_digest=canonical_digest(remote.binding))
        remote.profile = SSHTarget.from_dict(value)
        self.profile = remote.profile.to_dict()
        self.proposal = self.local.proposal(dict(self.local.target,
            transport=dict(kind="ssh", profile_digest=remote.profile.digest())))
        self.local.adopt(self.proposal)

    def arguments(self, **changes):
        args = dict(by="test-owner", adoption_digest=self.proposal["digest"], request_id="ssh-observe-1",
            ttl_seconds=60, ssh_profile=self.profile, state_dir=self.remote.root / "observer-audit", allow_network=True)
        args.update(changes)
        return args

    def client(self, target, **kwargs):
        return SSHControlClient(target, ssh_binary=self.remote.fake_ssh, **kwargs)

    async def observe(self, **changes):
        with patch("camol.target_ssh_runtime.SSHControlClient", side_effect=self.client):
            return await self.local.registry.observe_ssh(self.proposal["descriptor"]["generation"], **self.arguments(**changes))

    async def test_real_bridge_measurement_replay_inspection_and_historical_retry(self):
        self.assertNotIn(COMMAND, DEFAULT_READ_COMMANDS)
        report = await self.observe()
        self.assertEqual(report["profile"], local_profile())
        self.assertEqual(report["meaning"], MEANING)
        target = self.local.registry.inspect()["targets"][0]
        self.assertEqual(target["ssh_runtime_observation"], report)
        self.assertEqual(target["readiness"], "unproven")
        self.assertFalse(target["execution_authority"])
        self.assertFalse(target["deletion_authority"])
        self.assertEqual(project(self.local.fixture.store.read(self.local.run)), self.local.orch.state(self.local.run))
        self.local.retire(self.proposal)
        with patch("camol.target_ssh_runtime.SSHControlClient", side_effect=AssertionError("historical retry must not reconnect")):
            self.assertEqual(await self.local.registry.observe_ssh(self.proposal["descriptor"]["generation"],
                **self.arguments(allow_network=False)), report)
            with self.assertRaises(TargetError):
                await self.local.registry.observe_ssh(self.proposal["descriptor"]["generation"], **self.arguments(ttl_seconds=61))
        self.assertEqual(len(self.remote.requests), 1)

    async def test_reject_owner_profile_permission_network_and_bounds_before_connect(self):
        other = copy.deepcopy(self.profile)
        other["host"] = "other.example"
        for changes in (dict(by="strategist"), dict(ssh_profile=other), dict(allow_network=False),
                        dict(allow_network=1), dict(ttl_seconds=True), dict(ttl_seconds=301),
                        dict(request_id="x" * 129), dict(adoption_digest="sha256:" + "b" * 64), dict(ssh_profile={})):
            with self.subTest(changes=changes), patch("camol.target_ssh_runtime.SSHControlClient", side_effect=AssertionError("no connection")):
                with self.assertRaises(ValueError):
                    await self.local.registry.observe_ssh(self.proposal["descriptor"]["generation"], **self.arguments(**changes))
        self.assertFalse(self.remote.requests)

    async def test_remote_policy_rejection_and_changed_supervisor_runtime_publish_nothing(self):
        self.remote.binding["allowed_commands"].remove(COMMAND)
        self.remote.write_policy()
        with self.assertRaises(TargetError):
            await self.observe()
        self.remote.binding["allowed_commands"].append(COMMAND)
        self.remote.write_policy()
        changed = local_profile()
        changed["software"]["package_sha256"] = "sha256:" + "c" * 64
        changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
        with patch("camol.target_runtime.local_profile", return_value=changed), self.assertRaises(TargetError):
            await self.observe()
        self.assertNotIn("target_ssh_runtime_observations", self.local.orch.state(self.local.run))

    async def test_retirement_during_network_wait_rejects_late_report(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.remote.supervisor._measure_target_profile
        async def delayed():
            entered.set()
            await release.wait()
            return await original()
        self.remote.supervisor._measure_target_profile = delayed
        driver = asyncio.create_task(self.observe())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.local.retire(self.proposal)
            cursor = self.local.orch.state(self.local.run)["last_seq"]
            release.set()
            with self.assertRaises(TargetError):
                await asyncio.wait_for(driver, 5)
            self.assertEqual(self.local.orch.state(self.local.run)["last_seq"], cursor)
        finally:
            release.set()
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)

    async def test_cancel_and_busy_keep_kernel_unchanged_and_no_late_publication(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.remote.supervisor._measure_target_profile
        async def delayed():
            entered.set()
            await release.wait()
            return await original()
        self.remote.supervisor._measure_target_profile = delayed
        driver = asyncio.create_task(self.observe())
        cursor = self.local.orch.state(self.local.run)["last_seq"]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with self.assertRaises(TargetError):
                await self.observe(request_id="ssh-observe-2")
            driver.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(driver, 5)
            release.set()
            await asyncio.sleep(.05)
            self.assertFalse(self.local.registry._ssh_runtime_busy)
            self.assertEqual(self.local.orch.state(self.local.run)["last_seq"], cursor)
        finally:
            release.set()
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)

    async def test_replay_rejects_forged_scope_response_and_authority(self):
        before = self.local.orch.state(self.local.run)
        await self.observe()
        event = self.local.fixture.store.read(self.local.run)[-1]
        for key, value in (("meaning", "ready"), ("response_digest", "sha256:" + "d" * 64),
                           ("expires_at", "2099-01-01T00:00:00Z"), ("schema_version", True),
                           ("elapsed_ms", True), ("run_id", "other"), ("extra", 1)):
            changed = copy.deepcopy(event)
            changed["payload"][key] = value
            changed["payload"]["digest"] = canonical_digest({k: v for k, v in changed["payload"].items() if k != "digest"})
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_event(before, changed)
        with self.assertRaises(TargetError):
            apply_event(before, dict(event, actor_id="strategist"))

    async def test_append_failure_and_lost_response_retry_do_not_fabricate_freshness(self):
        before = self.local.orch.state(self.local.run)
        with patch.object(self.local.fixture.store, "append", side_effect=OSError("fixture unavailable")):
            with self.assertRaises(OSError):
                await self.observe()
        self.assertEqual(self.local.orch.state(self.local.run), before)
        append = self.local.fixture.store.append
        def appended_then_lost(*args, **kwargs):
            append(*args, **kwargs)
            raise OSError("fixture response lost after commit")
        with patch.object(self.local.fixture.store, "append", side_effect=appended_then_lost):
            with self.assertRaises(OSError):
                await self.observe()
        report = self.local.orch.state(self.local.run)["target_ssh_runtime_observations"]["ssh-observe-1"]
        self.local.fixture.clock.advance(3600)
        with patch("camol.target_ssh_runtime.SSHControlClient", side_effect=AssertionError("committed retry cannot reconnect")):
            self.assertEqual(await self.local.registry.observe_ssh(self.proposal["descriptor"]["generation"],
                **self.arguments(allow_network=False)), report)
        self.assertEqual(len(self.remote.requests), 2)

    async def test_live_child_cli_observation_keeps_controls_responsive(self):
        fixture = self.local.fixture
        supervisor = Supervisor(transport_fixture.ROOT / "examples/three-agent-runbook.json", fixture.source,
            fixture.state_dir, database=fixture.state_dir / "events.sqlite3")
        supervisor.draining = True  # This test authorizes observation, not task execution.
        serving = asyncio.create_task(supervisor.serve())
        profile_file = self.remote.root / "reviewed-ssh-profile.json"
        profile_file.write_text(json.dumps(self.profile))
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.remote.supervisor._measure_target_profile
        async def delayed():
            entered.set()
            await release.wait()
            return await original()
        self.remote.supervisor._measure_target_profile = delayed
        child = None
        try:
            for _ in range(500):
                if supervisor.paths.socket.exists():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(supervisor.paths.socket.exists())
            state = supervisor.orchestrator.state(supervisor.run_id)
            args = [sys.executable, "-m", "camol", "target", "observe-ssh", "--live",
                "--state-dir", str(fixture.state_dir), "--db", str(supervisor.paths.database),
                "--run-id", state["run_id"], "--plan-digest", state["plan_digest"],
                "--workspace", str(fixture.source), "--by", "test-owner", "--generation", "generation-1",
                "--adoption-digest", self.proposal["digest"], "--request-id", "live-1", "--ssh-profile", str(profile_file)]
            with patch("camol.target_ssh_runtime.SSHControlClient", side_effect=self.client):
                child = await asyncio.create_subprocess_exec(*args, "--allow-network", cwd=str(self.runtime_root),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(entered.wait(), 5)
                self.assertTrue((await asyncio.wait_for(send_control(fixture.state_dir, "ping"), 1))["ok"])
                params = dict(generation="generation-1", adoption_digest=self.proposal["digest"],
                    request_id="busy-2", ttl_seconds=300, approved_by="test-owner", ssh_profile=self.profile, allow_network=True)
                with self.assertRaises(SupervisorError):
                    await send_control_v2(fixture.state_dir, "target-observe-ssh", requested_by="test-owner", params=params,
                        expected_run_id=state["run_id"], expected_plan_digest=state["plan_digest"])
                release.set()
                out, err = await asyncio.wait_for(child.communicate(), 10)
                self.assertEqual(child.returncode, 0, err.decode()[:500])
                report = json.loads(out)
                self.assertEqual(report["profile"], local_profile())
                self.assertEqual(supervisor.orchestrator.state(supervisor.run_id)["total_tokens"], 0)
                # A separate real CLI request returns the historical receipt with no network opt-in.
                child = await asyncio.create_subprocess_exec(*args, cwd=str(self.runtime_root),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(child.communicate(), 10)
                self.assertEqual(child.returncode, 0, err.decode()[:500])
                self.assertEqual(json.loads(out), report)
            self.assertEqual(len(self.remote.requests), 1)
            params["ssh_profile"] = {}
            with self.assertRaises(SupervisorError):
                await send_control_v2(fixture.state_dir, "target-observe-ssh", requested_by="test-owner", params=params,
                    expected_run_id=state["run_id"], expected_plan_digest=state["plan_digest"])
            self.assertTrue((await send_control(fixture.state_dir, "ping"))["ok"])
        finally:
            release.set()
            if child is not None and child.returncode is None:
                child.kill()
                await child.wait()
            if not serving.done():
                await send_control(fixture.state_dir, "stop", requested_by="test-owner")
            await asyncio.wait_for(serving, 5)
