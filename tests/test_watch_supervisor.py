import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from camol.schema import canonical_digest
from camol.supervisor import Supervisor, SupervisorError, send_control, send_control_v2
from camol.watchers import WatchSpec
from camol.watch_runtime import JournalSource, journal_fixture_digest, journal_header
from tests import test_supervisor as supervisor_fixture
ROOT = supervisor_fixture.ROOT


class WatchSupervisorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = supervisor_fixture.SupervisorTests.asyncSetUp
    asyncTearDown = supervisor_fixture.SupervisorTests.asyncTearDown
    _wait_for = supervisor_fixture.SupervisorTests._wait_for


async def _test_live_observer_resumes_from_durable_cursor_without_a_client(self):
    async def no_build(runner, run_id, **kwargs):
        return runner.orchestrator.state(run_id)

    now = datetime.now(timezone.utc)
    spec = WatchSpec("calls", "local-calls", "journal:local-calls", ("started", "ended"), ("job",), "ended",
                     (("job", "one"),), "calls-v1", "jsonl-v1", canonical_digest("placeholder"), timeout_seconds=2)
    spec = replace(spec, fixture_digest=journal_fixture_digest(spec))
    journal = self.source.parent / "calls.jsonl"
    journal.write_text(json.dumps(journal_header(spec)) + "\n")
    journal.chmod(0o600)
    schedule = dict(schema="camol.watch_schedule", schema_version=1, watcher_id="calls",
                    binding=JournalSource().binding(spec.source_id, journal), interval_seconds=1,
                    max_polls=10, expires_at=(now + timedelta(minutes=5)).isoformat())

    async def watch_state():
        return (await send_control_v2(self.state, "watch-inspect", requested_by="owner", params={}))["result"]["calls"]

    async def wait_poll(predicate):
        for _ in range(200):
            result = await watch_state()
            if predicate(result):
                return result
            await asyncio.sleep(0.02)
        self.fail("watcher failed to reach the expected durable state")

    with patch("camol.supervisor.HarnessRunner.run_until_terminal", no_build):
        first = Supervisor(ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        serving = asyncio.create_task(first.serve())
        await self._wait_for(first.paths.socket)
        try:
            await send_control(self.state, "approve", requested_by="owner")
            with self.assertRaisesRegex(SupervisorError, "exact watcher specification"):
                await send_control_v2(self.state, "watch-create", requested_by="owner",
                           params=dict(spec=spec.to_dict(), approved_by="owner", approval_digest=canonical_digest("wrong")))
            created = await send_control_v2(self.state, "watch-create", requested_by="owner",
                           params=dict(spec=spec.to_dict(), approved_by="owner", approval_digest=canonical_digest(spec.to_dict())))
            self.assertTrue(created["ok"], created)
            configured = await send_control_v2(self.state, "watch-schedule", requested_by="owner",
                           params=dict(schedule=schedule, approved_by="owner", approval_digest=canonical_digest(schedule)))
            self.assertTrue(configured["ok"], configured)
            before = await wait_poll(lambda item: item["scheduled_polls"] >= 1 and not item["active_poll"])
            self.assertIsNotNone(before["cursor"])
        finally:
            await send_control(self.state, "stop", requested_by="owner")
            await asyncio.wait_for(serving, timeout=5)
        terminal = dict(event_id="call-one", revision=1, event_class="ended", correlation={"job": "one"},
                        occurred_at=datetime.now(timezone.utc).isoformat(), content_digest=canonical_digest("call"))
        with journal.open("a") as handle:
            handle.write(json.dumps(terminal) + "\n")
        second = Supervisor(ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        serving = asyncio.create_task(second.serve())
        await self._wait_for(second.paths.socket)
        try:
            completed = await wait_poll(lambda item: item["status"] == "completed" and not item["active_poll"])
            self.assertGreater(completed["scheduled_polls"], before["scheduled_polls"])
            self.assertEqual(len(completed["observations"]), 1)
            self.assertNotEqual(completed["cursor"], before["cursor"])
            self.assertIsNone((await send_control(self.state, "status"))["result"]["watch_error"])
        finally:
            await send_control(self.state, "stop", requested_by="owner")
            await asyncio.wait_for(serving, timeout=5)


WatchSupervisorTests.test_live_observer_resumes_from_durable_cursor_without_a_client = _test_live_observer_resumes_from_durable_cursor_without_a_client
