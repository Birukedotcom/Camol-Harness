import asyncio
import contextlib
import copy
import json
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

from camol.schema import canonical_digest
from camol.state import project
from camol.supervisor import Supervisor, SupervisorError, send_control, send_control_v2
from camol.worker_delivery import DeliveryError
from camol.worker_gateway import WorkerGateway, policy
from camol.worker_tls import WorkerTLSClient, WorkerTLSServer, WorkerTransportError
from tests import test_worker_tls as tls_fixture


class WorkerGatewayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        tls_fixture.WorkerTLSTests.setUpClass.__func__(cls)

    async def asyncSetUp(self):
        await tls_fixture.WorkerTLSTests.asyncSetUp(self)
        await self.server.close()
        self.service = self.fixture.service
        self.gateway = WorkerGateway(self.fixture.orch, self.fixture.fixture.run_id, self.fixture.fixture.state_dir)
        self.addAsyncCleanup(self.gateway.close)
        self.configuration = dict(schema="camol.worker_gateway", schema_version=1, configuration_id="gateway-1",
            run_id=self.fixture.fixture.run_id, plan_digest=self.value["stream"]["fence"]["plan_digest"],
            scopes=[self.value["scope"]], address="127.0.0.1", port=self.profile["port"], allow_non_loopback=False,
            certificate_file=str(self.cert), private_key_file=str(self.key), certificate_digest=self.profile["certificate_digest"],
            max_connections=2, timeout_seconds=2, poll_seconds=1, streams_per_tick=1,
            expires_at=(self.fixture.fixture.clock() + timedelta(seconds=25)).isoformat())

    def configure(self, configuration=None):
        value = configuration or self.configuration
        return self.gateway.configure(value, by="test-owner", approval_digest=canonical_digest(value))

    async def test_default_off_approved_tls_pump_restart_and_no_empty_poll_events(self):
        await self.gateway.tick()
        self.assertIsNone(self.gateway.server)
        self.assertIsNone(self.gateway._streams)
        configured = self.configure()
        await self.gateway.tick()
        self.assertTrue(self.gateway.status()["listener"]["listening"])
        receipt = await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        self.assertEqual(receipt["cursor"], 1)
        self.gateway.next_poll = 0
        await self.gateway.tick()
        captures = self.service.records(self.value["scope"])
        self.assertEqual(captures["imported_cursor"], 1)
        state = self.fixture.orch.state(self.fixture.fixture.run_id)
        captured_request = next(iter(state["worker_imports"]["requests"].values()))
        self.assertEqual(captured_request["gateway_policy_digest"], configured["policy_digest"])
        before = state["last_seq"]
        for _ in range(3):
            self.gateway.next_poll = 0
            await self.gateway.tick()
        self.assertEqual(self.fixture.orch.state(self.fixture.fixture.run_id)["last_seq"], before)
        await self.gateway.close()
        restarted = WorkerGateway(self.fixture.orch, self.fixture.fixture.run_id, self.fixture.fixture.state_dir)
        self.addAsyncCleanup(restarted.close)
        await restarted.tick()
        self.assertTrue(restarted.status()["listener"]["listening"])
        self.assertEqual(self.service.records(self.value["scope"])["imported_cursor"], 1)
        events = self.fixture.fixture.store.read(self.fixture.fixture.run_id)
        self.assertEqual(project(events), self.fixture.orch.state(self.fixture.fixture.run_id))
        self.assertEqual(self.fixture.orch.state(self.fixture.fixture.run_id)["total_tokens"], 0)

    async def test_owner_scope_digest_and_unknown_fields_precede_certificate_read(self):
        for change in ({"scopes": ["sha256:" + "f" * 64]}, {"run_id": "foreign-run"},
                       {"expires_at": self.fixture.orch._now()}, {"schema_version": True},
                       {"address": "0.0.0.0"}, {"streams_per_tick": 1000}, {"surprise": True}):
            value = dict(self.configuration, **change)
            with patch("camol.worker_gateway.server_context") as load:
                with self.assertRaises(ValueError):
                    self.configure(value)
                load.assert_not_called()
        with self.assertRaises(DeliveryError):
            self.gateway.configure(self.configuration, by="strategist", approval_digest=canonical_digest(self.configuration))
        with self.assertRaises(DeliveryError):
            self.gateway.configure(self.configuration, by="test-owner", approval_digest="sha256:" + "f" * 64)
        self.assertNotIn("worker_gateway", self.fixture.orch.state(self.fixture.fixture.run_id))

    async def test_stop_immediately_denies_inflight_stream_and_cannot_revive_configuration(self):
        configured = self.configure()
        await self.gateway.tick()
        stopped = self.gateway.stop(by="test-owner", configuration_id="gateway-1", policy_digest=configured["policy_digest"])
        self.assertFalse(stopped["enabled"])
        with self.assertRaises(WorkerTransportError):
            await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        with self.assertRaises(DeliveryError):
            self.configure()
        await self.gateway.tick()
        self.assertIsNone(self.gateway.server)
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)

    async def test_missing_operational_key_never_opens_a_restored_listener(self):
        self.configure()
        key = self.service._directory(self.value["scope"]) / "key"
        key.rename(self.fixture.fixture.state_dir / "unavailable-key")
        with patch("camol.worker_gateway.WorkerTLSServer") as listener:
            await self.gateway.tick()
        listener.assert_not_called()
        self.assertIsNone(self.gateway.server)
        self.assertEqual(self.gateway.error, "NO_CURRENT_ENROLLED_STREAM")

    async def test_policy_expiring_during_certificate_setup_never_binds(self):
        self.configuration["expires_at"] = (self.fixture.fixture.clock() + timedelta(seconds=5)).isoformat()
        self.configure()
        created = []
        def delayed(*args, **kwargs):
            server = WorkerTLSServer(*args, **kwargs)
            created.append(server)
            self.fixture.fixture.clock.advance(6)
            return server
        with patch("camol.worker_gateway.WorkerTLSServer", side_effect=delayed):
            await self.gateway.tick()
        self.assertEqual(len(created), 1)
        self.assertIsNone(created[0].listener)
        self.assertIsNone(self.gateway.server)

    async def test_dead_listener_is_recreated_under_same_fresh_policy(self):
        self.configure()
        await self.gateway.tick()
        first = self.gateway.server
        await first.close()
        self.gateway.next_poll = 0
        await self.gateway.tick()
        self.assertIsNot(self.gateway.server, first)
        self.assertTrue(self.gateway.status()["listener"]["listening"])
        self.assertEqual((await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True))["cursor"], 1)

    async def test_expiry_inside_kernel_lock_denies_receipt_and_capture(self):
        self.configuration["expires_at"] = (self.fixture.fixture.clock() + timedelta(seconds=5)).isoformat()
        self.configure()
        await self.gateway.tick()
        original = self.gateway.streams._lease_lock
        @contextlib.contextmanager
        def expire_while_locked():
            with original():
                self.fixture.fixture.clock.advance(6)
                yield
        with patch.object(self.gateway.streams, "_lease_lock", expire_while_locked):
            with self.assertRaises(WorkerTransportError):
                await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        await self.gateway.tick()
        self.assertIsNone(self.gateway.server)
        self.assertFalse(self.gateway.status()["policy_currently_eligible"])

    async def test_capture_policy_expiry_during_spool_read_cannot_become_owner_manual_import(self):
        self.configuration["expires_at"] = (self.fixture.fixture.clock() + timedelta(seconds=5)).isoformat()
        self.configure()
        await self.gateway.tick()
        await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        original = self.gateway.streams._material
        def advance(value):
            result = original(value)
            self.fixture.fixture.clock.advance(3)
            return result
        self.gateway.next_poll = 0
        with patch.object(self.gateway.streams, "_material", side_effect=advance):
            await self.gateway.tick()
        self.assertNotIn("worker_imports", self.fixture.orch.state(self.fixture.fixture.run_id))
        self.assertIn(self.value["scope"], self.gateway.capture_errors)

    async def test_replay_rejects_worker_gateway_forgery_and_removed_capture_authority(self):
        self.configure()
        await self.gateway.tick()
        await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        self.gateway.next_poll = 0
        await self.gateway.tick()
        events = self.fixture.fixture.store.read(self.fixture.fixture.run_id)
        changed = copy.deepcopy(events)
        next(row for row in changed if row["type"] == "WORKER_GATEWAY_CONFIGURED")["actor_id"] = "strategist"
        with self.assertRaises(ValueError):
            project(changed)
        changed = copy.deepcopy(events)
        captured = next(row for row in changed if row["type"] == "WORKER_STREAM_IMPORTED")
        captured["payload"]["gateway_policy_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            project(changed)

    async def test_supervisor_bound_control_background_capture_and_shutdown(self):
        async def no_build(runner, run_id, **kwargs):
            return runner.orchestrator.state(run_id)
        source = self.fixture.fixture.source
        root = self.fixture.fixture.state_dir
        supervisor = Supervisor(tls_fixture.enrollment_fixture.fixture_module.ROOT / "examples/three-agent-runbook.json",
            source, root, database=self.fixture.fixture.store.path)
        fixed_now = self.fixture.orch._now()
        async def control(command, params=None, expected_digest=None):
            return await send_control_v2(root, command, requested_by="test-owner", params=params or {},
                expected_run_id=self.fixture.fixture.run_id, expected_plan_digest=expected_digest or self.configuration["plan_digest"])
        with patch("camol.supervisor.HarnessRunner.run_until_terminal", no_build), \
                patch("camol.supervisor.Orchestrator._now", return_value=fixed_now):
            serving = asyncio.create_task(supervisor.serve())
            try:
                for _ in range(200):
                    if supervisor.paths.socket.exists():
                        break
                    if serving.done():
                        await serving
                    await asyncio.sleep(.01)
                self.assertIsNone((await control("worker-gateway-status"))["result"]["listener"])
                self.assertIsNone(supervisor._worker_task, "an unconfigured gateway has no background poll task")
                with self.assertRaises(SupervisorError):
                    await control("worker-stream-inspect", dict(scope=self.value["scope"], expected_database="/wrong/database"))
                reader, writer = await asyncio.open_unix_connection(str(supervisor.paths.socket))
                try:
                    raw = json.dumps(dict(schema="camol.control_request", schema_version=1, token=supervisor.token,
                        command="status", requested_by="test-owner")).replace('"command": "status"', '"command": "status", "command": "status"')
                    writer.write((raw + "\n").encode())
                    await writer.drain()
                    denied = json.loads(await asyncio.wait_for(reader.readline(), 2))
                    self.assertFalse(denied["ok"])
                    self.assertIn("duplicate", denied["error"])
                finally:
                    writer.close()
                    await writer.wait_closed()
                with self.assertRaises(SupervisorError):
                    await control("worker-gateway-configure", dict(policy=self.configuration,
                        approval_digest=canonical_digest(self.configuration), approved_by="test-owner"), expected_digest="sha256:" + "f" * 64)
                await control("worker-gateway-configure", dict(policy=self.configuration,
                    approval_digest=canonical_digest(self.configuration), approved_by="test-owner"))
                for _ in range(200):
                    status = (await control("worker-gateway-status"))["result"]
                    if status["listener"] and status["listener"]["listening"]:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(status["listener"]["listening"])
                await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
                for _ in range(200):
                    records = (await control("worker-stream-records", dict(scope=self.value["scope"], after=0, limit=100)))["result"]
                    if records["imported_cursor"] == 1:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(records["imported_cursor"], 1)
                child = await asyncio.create_subprocess_exec(sys.executable, "-m", "camol", "worker-gateway", "status",
                    "--state-dir", str(root), "--run-id", self.fixture.fixture.run_id,
                    "--plan-digest", self.configuration["plan_digest"], "--by", "test-owner",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(child.communicate(), 10)
                self.assertEqual(child.returncode, 0, err.decode()[:300])
                self.assertTrue(json.loads(out)["listener"]["listening"])
                child = await asyncio.create_subprocess_exec(sys.executable, "-m", "camol", "worker-enrollment", "records",
                    "--live", "--state-dir", str(root), "--db", str(self.fixture.fixture.store.path),
                    "--run-id", self.fixture.fixture.run_id, "--plan-digest", self.configuration["plan_digest"],
                    "--scope", self.value["scope"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(child.communicate(), 10)
                self.assertEqual(child.returncode, 0, err.decode()[:300])
                self.assertEqual(json.loads(out)["imported_cursor"], 1)
            finally:
                if not serving.done():
                    await send_control(root, "stop", requested_by="test-owner")
                await asyncio.wait_for(serving, 5)
        self.assertIsNone(supervisor.worker_gateway.server)
        self.assertFalse(supervisor.paths.socket.exists())
