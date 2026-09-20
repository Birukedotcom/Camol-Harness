import asyncio
import contextlib
import copy
import io
import json
import sys
import threading
import unittest
from unittest.mock import patch

from camol.artifacts import ArtifactStore, RunArchive
from camol.cli import main
from camol.schema import canonical_digest
from camol.state import apply_event, project
from camol.store import ConcurrentAppendError
from camol.target_runtime import local_profile, validate_profile
from camol.targets import TargetError
from camol.supervisor import Supervisor, SupervisorError, send_control, send_control_v2
from camol.ssh_protocol import SSHTransportError
from tests import test_targets as target_fixture
from tests import test_supervisor as supervisor_fixture


class TargetRuntimeTests(unittest.TestCase):
    setUp = target_fixture.TargetTests.setUp
    proposal = target_fixture.TargetTests.proposal
    adopt = target_fixture.TargetTests.adopt
    retire = target_fixture.TargetTests.retire

    def local(self):
        profile = local_profile()
        proposal = self.proposal(dict(self.target, transport=dict(kind="local", profile_digest=profile["digest"])))
        self.adopt(proposal)
        return proposal, profile

    def observe(self, proposal, **changes):
        arguments = dict(by="test-owner", adoption_digest=proposal["digest"], request_id="observe-1", ttl_seconds=60)
        arguments.update(changes)
        return self.registry.observe_local(proposal["descriptor"]["generation"], **arguments)

    def test_real_local_report_is_bound_replayed_and_never_capacity_or_readiness(self):
        proposal, profile = self.local()
        before = self.orch.state(self.run)
        report = self.observe(proposal)
        self.assertEqual(report["profile"], profile)
        self.assertEqual(report["generation"], proposal["descriptor"]["generation"])
        self.assertIsInstance(report["elapsed_ms"], int)
        after = self.orch.state(self.run)
        for field in ("tasks", "admissions", "reservations", "agents", "total_tokens", "evidence"):
            self.assertEqual(before[field], after[field])
        record = self.registry.inspect()["targets"][0]
        self.assertEqual(record["runtime_observation"], report)
        self.assertEqual(record["readiness"], "unproven")
        self.assertFalse(record["execution_authority"])
        events = self.fixture.store.read(self.run)
        self.assertEqual(project(events), after)
        archive = self.fixture.state_dir.parent / "runtime-export"
        RunArchive.export(self.run, events, ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), after)

    def test_retry_never_refreshes_expiry_or_reobserves_after_retirement(self):
        proposal, _ = self.local()
        report = self.observe(proposal)
        self.fixture.clock.advance(61)
        self.retire(proposal)
        before = self.orch.state(self.run)
        with patch("camol.target_runtime.local_profile", side_effect=AssertionError("must not reobserve")):
            self.assertEqual(self.observe(proposal), report)
            with self.assertRaises(TargetError):
                self.observe(proposal, ttl_seconds=120)
            with self.assertRaises(TargetError):
                self.observe(proposal, request_id="observe-2")
        self.assertEqual(before, self.orch.state(self.run))

    def test_remote_wrong_owner_digest_and_bounds_fail_before_measuring(self):
        proposal = self.proposal()
        self.adopt(proposal)
        with patch("camol.target_runtime.local_profile", side_effect=AssertionError("must not measure")):
            for changes in ({}, dict(by="strategist"), dict(ttl_seconds=True), dict(ttl_seconds=301),
                            dict(request_id="a" * 129), dict(adoption_digest="sha256:" + "b" * 64)):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.observe(proposal, **changes)
        self.assertNotIn("target_runtime_observations", self.orch.state(self.run))

    def test_profile_change_or_concurrent_retirement_denies_report(self):
        proposal, profile = self.local()
        changed = copy.deepcopy(profile)
        changed["hostname"] += "-changed"
        changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
        with patch("camol.target_runtime.local_profile", return_value=changed):
            with self.assertRaises(TargetError):
                self.observe(proposal)
        def retire_during_measurement():
            self.retire(proposal)
            return profile
        with patch("camol.target_runtime.local_profile", side_effect=retire_during_measurement):
            with self.assertRaises(TargetError):
                self.observe(proposal)
        self.assertNotIn("target_runtime_observations", self.orch.state(self.run))

    def test_clock_regression_unknown_cpu_and_history_bound(self):
        proposal, profile = self.local()
        with patch("camol.target_runtime.MAX_OBSERVATIONS", 0), patch("camol.target_runtime.local_profile", side_effect=AssertionError("no capacity")):
            with self.assertRaises(TargetError):
                self.observe(proposal)
        with patch.object(self.orch, "_now", side_effect=["2026-09-03T12:01:00Z", "2026-09-03T12:00:00Z"]), patch("camol.target_runtime.local_profile", return_value=profile):
            with self.assertRaises(TargetError):
                self.observe(proposal)
        with patch("camol.target_runtime.os.cpu_count", return_value=None):
            self.assertIsNone(self.observe(proposal)["cpu_count"])

    def test_replay_rejects_worker_authorship_changed_identity_and_claimed_capacity(self):
        proposal, _ = self.local()
        before = self.orch.state(self.run)
        self.observe(proposal)
        event = self.fixture.store.read(self.run)[-1]
        for key, value in (("cpu_count", True), ("meaning", "ready"), ("expires_at", "2099-01-01T00:00:00Z"),
                           ("run_id", "other"), ("schema_version", True), ("extra", 5)):
            changed = copy.deepcopy(event)
            changed["payload"][key] = value
            changed["payload"]["digest"] = canonical_digest({k: v for k, v in changed["payload"].items() if k != "digest"})
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_event(before, changed)
        with self.assertRaises(TargetError):
            apply_event(before, dict(event, actor_id="strategist"))

    def test_standalone_cli_and_unsafe_identity_are_explicit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["target", "local-profile"]), 0)
        validate_profile(json.loads(output.getvalue()))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["target", "local-profile", "--run-id", "ignored"]), 2)
        with patch("camol.ssh_bridge.bridge_identity", side_effect=SSHTransportError("IDENTITY_DENIED", "fixture")):
            with self.assertRaises(TargetError):
                local_profile()

    def test_lost_append_response_and_secret_requests_do_not_remeasure(self):
        proposal, _ = self.local()
        token = "runtime-request-private-token-1234567890"
        with patch.dict("os.environ", {"CAMOL_RUNTIME_TEST_TOKEN": token}), \
                patch("camol.target_runtime.local_profile", side_effect=AssertionError("deny before measurement")):
            with self.assertRaises(TargetError):
                self.observe(proposal, request_id=token)
        before = self.orch.state(self.run)
        with patch.object(self.fixture.store, "append", side_effect=OSError("fixture append failure")):
            with self.assertRaises(OSError):
                self.observe(proposal)
        self.assertEqual(before, self.orch.state(self.run))
        append = self.fixture.store.append
        def lost(*args, **kwargs):
            append(*args, **kwargs)
            raise OSError("fixture lost response")
        with patch.object(self.fixture.store, "append", side_effect=lost):
            with self.assertRaises(OSError):
                self.observe(proposal)
        with patch("camol.target_runtime.local_profile", side_effect=AssertionError("must not remeasure")):
            receipt = self.observe(proposal)
        self.assertEqual(receipt, self.orch.state(self.run)["target_runtime_observations"]["observe-1"])
        with patch("subprocess.Popen", side_effect=AssertionError("no commands during measurement")):
            self.observe(proposal, request_id="observe-2")

    def test_unrelated_run_progress_is_rebased_without_losing_final_compare_and_swap(self):
        proposal, profile = self.local()
        other = dict(self.target, generation="generation-2", target_id="target-2",
                     provider=dict(self.target["provider"], resource_id="other-instance"))
        def progress():
            self.adopt(self.proposal(other))
            return profile
        with patch("camol.target_runtime.local_profile", side_effect=progress):
            self.assertEqual(self.observe(proposal)["profile"], profile)
        with patch.object(self.fixture.store, "append", side_effect=ConcurrentAppendError("fixture changed after final validation")):
            with self.assertRaises(ConcurrentAppendError):
                self.observe(proposal, request_id="observe-2")
        self.assertNotIn("observe-2", self.orch.state(self.run)["target_runtime_observations"])


class TargetRuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = supervisor_fixture.SupervisorTests.asyncSetUp
    asyncTearDown = supervisor_fixture.SupervisorTests.asyncTearDown
    _wait_for = supervisor_fixture.SupervisorTests._wait_for

    async def test_blocked_measurement_keeps_control_responsive_and_cancellation_cannot_publish(self):
        supervisor = Supervisor(supervisor_fixture.ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        entered, release = threading.Event(), threading.Event()
        profile = local_profile()
        def slow_profile():
            entered.set()
            if not release.wait(5):
                raise TargetError("fixture release deadline")
            return profile
        serving = asyncio.create_task(supervisor.serve())
        measuring = None
        try:
            await self._wait_for(supervisor.paths.socket)
            with patch("camol.target_runtime.local_profile", side_effect=slow_profile):
                measuring = asyncio.create_task(supervisor._measure_target_profile())
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertTrue((await asyncio.wait_for(send_control(self.state, "ping"), .5))["ok"])
                with self.assertRaises(TargetError):
                    await supervisor._measure_target_profile()
                measuring.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await measuring
                self.assertFalse(supervisor._target_profile_task.done())
                release.set()
                await asyncio.wait_for(supervisor._target_profile_task, 2)
                self.assertNotIn("target_runtime_observations", supervisor.orchestrator.state(supervisor.run_id))
                with self.assertRaises(TargetError):
                    supervisor._target_measurement_scope("different-run", "sha256:" + "f" * 64)
        finally:
            release.set()
            if measuring is not None and not measuring.done():
                measuring.cancel()
                await asyncio.gather(measuring, return_exceptions=True)
            if not serving.done():
                await send_control(self.state, "stop")
            await asyncio.wait_for(serving, 5)

    async def test_live_profile_and_observe_cli_measure_supervisor_not_client(self):
        supervisor = Supervisor(supervisor_fixture.ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        serving = asyncio.create_task(supervisor.serve())
        try:
            await self._wait_for(supervisor.paths.socket)
            await send_control(self.state, "drain")
            await send_control(self.state, "approve", requested_by="human-owner")
            state = supervisor.orchestrator.state(supervisor.run_id)
            async def control(command, params):
                return await send_control_v2(self.state, command, requested_by="human-owner", params=params,
                    expected_run_id=state["run_id"], expected_plan_digest=state["plan_digest"])
            async def cli(action, *extra):
                arguments = [sys.executable, "-m", "camol", "target", action, "--live", "--state-dir", str(self.state),
                    "--run-id", state["run_id"], "--plan-digest", state["plan_digest"]]
                child = await asyncio.create_subprocess_exec(*arguments, *extra, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(child.communicate(), 10)
                    self.assertEqual(child.returncode, 0, err.decode()[:300])
                    return json.loads(out)
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
            profile = await cli("local-profile")
            self.assertEqual(profile, local_profile())
            target = dict(schema="camol.execution_target", schema_version=1, target_id="host-1", generation="generation-1",
                control_plane_id="control-1", ownership="adopted", label="local fixture",
                provider=dict(kind="local", account="owner", project="fixture", location="local", resource_id="host-1", resource_name="host"),
                transport=dict(kind="local", profile_digest=profile["digest"]))
            proposal = (await control("target-propose", dict(descriptor=target, expires_at="2099-01-01T00:00:00Z", approved_by="human-owner")))["result"]
            await control("target-adopt", dict(proposal=proposal, approval_digest=proposal["digest"], approved_by="human-owner"))
            report = await cli("observe-local", "--workspace", str(self.source), "--by", "human-owner", "--generation", "generation-1",
                "--adoption-digest", proposal["digest"], "--request-id", "runtime-1", "--ttl-seconds", "60")
            self.assertEqual(report["profile"], profile)
            snapshot = (await control("target-inspect", dict(offset=0, limit=50)))["result"]
            self.assertEqual(snapshot["targets"][0]["runtime_observation"], report)
            self.assertEqual(supervisor.orchestrator.state(state["run_id"])["total_tokens"], 0)
            with patch("camol.ssh_bridge.bridge_identity", side_effect=SSHTransportError("IDENTITY_DENIED", "fixture")):
                with self.assertRaises(SupervisorError):
                    await control("target-local-profile", {})
        finally:
            if not serving.done():
                await send_control(self.state, "stop", requested_by="human-owner")
            await asyncio.wait_for(serving, 5)
