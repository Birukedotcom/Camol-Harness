import asyncio
import copy
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from camol.remote_monitor import RemoteMonitor, display_text, render_snapshot
from camol.schema import canonical_digest
from camol.ssh_protocol import SSHTarget, SSHTransportError


def profile():
    digest = "sha256:" + "a" * 64
    return SSHTarget(name="fixture", host="fixture.example", port=22, login="fixture",
        known_hosts="/tmp/not-used-known", known_hosts_sha256=digest, identity_file="/tmp/not-used-key",
        target_id="target", target_digest=digest, run_id="run", plan_digest=digest, owner="owner",
        bridge_identity=dict(camol_version="test", python_executable="/tmp/not-used-python",
                             python_sha256=digest, package_sha256=digest, control_version=3),
        allowed_commands=("status", "box", "stop"))


class FakeClient:
    def __init__(self, count=60):
        self.target = profile()
        self.calls, self.failure, self.wait, self.cancelled = [], None, None, False
        self.status = dict(schema="camol.supervisor_status", schema_version=1, mode="running",
            run=dict(run_id="run", plan_digest=self.target.plan_digest, status="running", total_tokens=321,
                agents={"box-{:03d}".format(i): dict(status="idle", role="builder", task_id="task {}".format(i)) for i in range(count)}))

    async def request(self, command, params=None):
        self.calls.append((command, copy.deepcopy(params)))
        assert command in {"status", "box"}, command
        if self.wait is not None:
            try:
                await self.wait.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.failure:
            raise self.failure
        if command == "status":
            return dict(ok=True, result=copy.deepcopy(self.status))
        result = dict(schema="camol.control_box", schema_version=1, run_id="run",
                      plan_digest=self.target.plan_digest, box_id=params["box_id"],
                      events=[dict(text="literal [red] details \u001b]0;remote-title\u0007")])
        return dict(ok=True, result=dict(result, snapshot_digest=canonical_digest(result)))


class RemoteMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_and_exact_box_are_bound_and_read_only_even_with_mutation_profile(self):
        client = FakeClient()
        monitor = RemoteMonitor(client)
        overview = await monitor.refresh()
        detail = await monitor.refresh("box-010")
        self.assertEqual(len(overview["rows"]), 60)
        self.assertIn("Reported model usage: 321 tokens", render_snapshot(overview))
        self.assertEqual(detail["detail"]["box_id"], "box-010")
        self.assertTrue(detail["read_only"])
        self.assertEqual(client.calls, [("status", None), ("status", None), ("box", dict(box_id="box-010", after_seq=0, limit=100, tail=True))])
        self.assertNotIn("\u001b", render_snapshot(detail))
        self.assertIn("literal [red]", render_snapshot(detail))
        self.assertIn("display truncated", display_text("x" * 100, 10))

    async def test_wrong_run_missing_worker_bad_digest_and_target_change_fail_closed(self):
        client = FakeClient()
        monitor = RemoteMonitor(client)
        client.status["run"]["run_id"] = "foreign"
        with self.assertRaises(SSHTransportError):
            await monitor.refresh()
        client.status["run"]["run_id"] = "run"
        with self.assertRaises(SSHTransportError):
            await monitor.refresh("prefix-box")
        self.assertTrue(all(command == "status" for command, _ in client.calls))
        original = client.request
        async def forged(command, params=None):
            response = await original(command, params)
            if command == "box":
                response["result"]["box_id"] = "other-box"
            return response
        with patch.object(client, "request", forged), self.assertRaises(SSHTransportError):
            await monitor.refresh("box-001")
        client.target = replace(client.target, name="changed")
        with self.assertRaises(SSHTransportError):
            await monitor.refresh()

    async def test_invalid_status_and_inventory_do_not_produce_a_healthy_snapshot(self):
        for change in ({"schema_version": True}, {"schema": "other"}, {"run": None}):
            client = FakeClient()
            client.status.update(change)
            with self.subTest(change=change), self.assertRaises(SSHTransportError):
                await RemoteMonitor(client).refresh()
        client = FakeClient()
        client.status["run"]["agents"]["box-000"]["status"] = []
        with self.assertRaises(SSHTransportError):
            await RemoteMonitor(client).refresh()
        client.target = replace(client.target, allowed_commands=("status",))
        with self.assertRaises(SSHTransportError):
            RemoteMonitor(client)

    async def test_snapshot_tampering_scope_race_and_oversize_are_rejected(self):
        for defect in ("digest", "scope", "oversize"):
            client = FakeClient()
            original = client.request
            async def faulty(command, params=None):
                value = await original(command, params)
                if defect == "digest" and command == "box":
                    value["result"]["events"].append(dict(text="changed after snapshot"))
                if defect == "scope":
                    client.target = replace(client.target, host="different.example")
                if defect == "oversize":
                    value["extra"] = "x" * (8 * 1024 * 1024)
                return value
            with patch.object(client, "request", faulty), self.subTest(defect=defect), self.assertRaises(SSHTransportError):
                await RemoteMonitor(client).refresh("box-001")

    async def test_requests_are_serialized_and_cancellation_reaches_the_transport(self):
        client = FakeClient()
        client.wait = asyncio.Event()
        monitor = RemoteMonitor(client)
        first = asyncio.create_task(monitor.refresh())
        await asyncio.sleep(0)
        second = asyncio.create_task(monitor.refresh("box-001"))
        await asyncio.sleep(0)
        self.assertEqual(len(client.calls), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(client.cancelled)
        client.wait.set()
        self.assertEqual((await second)["selected_box"], "box-001")

    async def test_actual_local_ssh_bridge_and_supervisor_report_boxes_without_mutation(self):
        from tests import test_ssh_transport as transport_fixture
        from camol.orchestrator import Orchestrator
        from camol.store import SQLiteEventStore
        from camol.runbook import load_runbook
        from camol.runner import summary
        fixture = transport_fixture.SSHTransportTests()
        await fixture.asyncSetUp()
        store = SQLiteEventStore(fixture.state / "monitor.sqlite3")
        try:
            plan = load_runbook(Path(__file__).resolve().parents[1] / "examples/three-agent-runbook.json")
            plan["run"]["id"] = "remote-run"
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(plan)
            fixture.supervisor.orchestrator = orchestrator
            fixture.supervisor.runbook = plan
            fixture.supervisor.store = store
            fixture.supervisor.paths = fixture.paths
            fixture.supervisor.status = lambda: dict(schema="camol.supervisor_status", schema_version=1,
                                                     run=summary(orchestrator.state("remote-run")), mode="stopped")
            fixture.binding.update(plan_digest=state["plan_digest"], allowed_commands=["status", "box"])
            fixture.write_policy()
            fixture.profile = replace(fixture.profile, plan_digest=state["plan_digest"],
                                      target_digest=canonical_digest(fixture.binding), allowed_commands=("status", "box"))
            monitor = RemoteMonitor(fixture.client())
            before = store.read("remote-run")
            result = await monitor.refresh("builder")
            self.assertEqual(result["detail"]["box_id"], "builder")
            self.assertEqual(result["detail"]["observation"]["basis"], "durable_ledger_snapshot")
            self.assertEqual(store.read("remote-run"), before)
            self.assertEqual([r["command"] for r in fixture.requests], ["status", "box"])
            self.assertNotIn(fixture.token, render_snapshot(result))
        finally:
            store.close()
            await fixture.asyncTearDown()


class RemoteMonitorCLITests(unittest.TestCase):
    def test_construct_after_a_closed_loop_and_reuse_across_sequential_loops(self):
        asyncio.run(asyncio.sleep(0))
        monitor = RemoteMonitor(FakeClient())
        self.assertEqual(asyncio.run(monitor.refresh())["run_id"], "run")
        self.assertEqual(asyncio.run(monitor.refresh("box-001"))["selected_box"], "box-001")

    def test_cli_opens_monitor_and_rejects_mutation_options_before_client_creation(self):
        from camol.cli import main
        target = profile().to_dict()
        with patch("camol.cli.load_contract", return_value=target), patch("camol.ssh_transport.SSHControlClient") as client, patch("camol.remote_tui.RemoteMonitorApp") as app:
            client.return_value = FakeClient()
            self.assertEqual(main(["remote", "monitor", "--target", "unused.json", "--state-dir", "/tmp/unused-monitor", "--interval", "10"]), 0)
            app.assert_called_once()
            app.return_value.run.assert_called_once()
            client.reset_mock()
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(main(["remote", "monitor", "--target", "unused.json", "--state-dir", "/tmp/unused-monitor", "--allow-mutation"]), 2)
            client.assert_not_called()
