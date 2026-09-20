import asyncio
import copy
from datetime import timedelta
import unittest
import tempfile
import json
import os
import sys
import camol
from pathlib import Path
from unittest.mock import patch

from camol.mailbox import Mailbox, MailboxError, apply, observe
from camol.orchestrator import Orchestrator
from camol.schema import canonical_digest
from camol.state import project
from camol.supervisor import Supervisor, SupervisorError
from tests import test_orchestrator as fixture
from tests import test_runner
from tests import test_supervisor


class MailboxTests(unittest.TestCase):
    tearDown = fixture.OrchestratorTests.tearDown
    admit_ready_tasks = fixture.OrchestratorTests.admit_ready_tasks

    def setUp(self):
        fixture.OrchestratorTests.setUp(self)
        self.admit_ready_tasks()
        self.assignments = self.orchestrator.lease_ready_tasks(self.run_id)
        for assignment in self.assignments:
            self.orchestrator.start_task(self.run_id, assignment)
        self.assignment = self.assignments[0]
        self.box = self.assignment["agent_id"]
        self.mailbox = Mailbox(self.orchestrator, self.run_id)
        self.target = self.mailbox.observe(self.box)

    def post(self, **kwargs):
        return self.mailbox.post(self.target, request_id="request-one", body="Check the edge case", sender="owner", **kwargs)

    def turn(self):
        self.orchestrator.record_turn(self.run_id, self.assignment, dict(status="continue", checkpoint="checked message",
            completed_step_ids=[], input_tokens=1, output_tokens=1))

    def test_restart_retry_preserves_one_message_and_rejects_changed_request(self):
        first = self.post()
        self.mailbox = Mailbox(Orchestrator(self.store), self.run_id)
        self.assertEqual(self.post(), first)
        with self.assertRaises(MailboxError):
            self.mailbox.post(self.target, request_id="request-one", body="different", sender="owner")
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(len(state["box_messages"]), 1)
        self.assertEqual(project(self.store.read(self.run_id)), state)
        self.assertEqual(state["total_tokens"], 0)

    def test_delivery_is_not_consumption_or_task_success(self):
        message = self.post()["message"]
        with self.assertRaises(MailboxError):
            self.mailbox.acknowledge(self.assignment, message["message_id"], consumed=True)
        packet = self.orchestrator.context_packet(self.run_id, self.assignment)
        self.mailbox.populate_packet(self.assignment, packet)
        self.assertEqual(packet["box_messages"][0], message)
        self.assertEqual(self.mailbox.inbox(self.box)["messages"][0]["status"], "queued")
        self.mailbox.acknowledge(self.assignment, message["message_id"])
        self.mailbox.acknowledge(self.assignment, message["message_id"])
        self.assertEqual(self.mailbox.inbox(self.box)["messages"][0]["status"], "delivered")
        self.turn()
        self.mailbox.acknowledge(self.assignment, message["message_id"], consumed=True)
        self.mailbox.acknowledge(self.assignment, message["message_id"], consumed=True)
        self.assertEqual(self.mailbox.inbox(self.box)["messages"][0]["status"], "consumed")
        self.assertEqual(self.orchestrator.state(self.run_id)["tasks"][self.assignment["task_id"]]["status"], "running")
        self.assertEqual(project(self.store.read(self.run_id)), self.orchestrator.state(self.run_id))

    def test_forged_expired_future_and_changed_generation_observations_deny(self):
        for mutation in (lambda value: value.update(observed_cursor=True), lambda value: value.update(observed_cursor=10**9),
                         lambda value: value["subject"].update(lease_id="other"), lambda value: value["subject"].update(plan_digest="sha256:" + "0" * 64),
                         lambda value: value.update(expires_at=value["observed_at"]), lambda value: value.update(extra=True)):
            value = copy.deepcopy(self.target)
            mutation(value)
            value["digest"] = canonical_digest({key: item for key, item in value.items() if key != "digest"})
            with self.assertRaises((MailboxError, ValueError)):
                self.mailbox.post(value, request_id="bad", body="message", sender="owner")
        self.assertNotIn("box_messages", self.orchestrator.state(self.run_id))
        clock = self.orchestrator.clock()
        self.orchestrator.clock = lambda: clock + timedelta(seconds=61)
        with self.assertRaises(MailboxError):
            self.post()

    def test_worker_sender_cannot_impersonate_or_acknowledge_another_box(self):
        other = self.assignments[1]
        value = self.mailbox.post(self.target, request_id="worker-message", body="Question", sender=other["agent_id"], assignment=other)
        self.assertEqual(value["message"]["reply_to"]["lease_id"], other["lease_id"])
        with self.assertRaises(MailboxError):
            self.mailbox.post(self.target, request_id="forged", body="hi", sender=self.box, assignment=other)
        with self.assertRaises(MailboxError):
            self.mailbox.acknowledge(other, value["message"]["message_id"])

    def test_expiry_stops_delivery_but_late_explicit_consumption_is_labeled(self):
        value = self.post(ttl_seconds=1)
        identity = value["message"]["message_id"]
        self.mailbox.acknowledge(self.assignment, identity)
        now = self.orchestrator.clock()
        self.orchestrator.clock = lambda: now + timedelta(seconds=2)
        self.assertEqual(self.mailbox.inbox(self.box)["messages"][0]["status"], "expired")
        self.turn()
        self.mailbox.acknowledge(self.assignment, identity, consumed=True)
        self.assertTrue(self.mailbox.inbox(self.box)["messages"][0]["consumed"]["after_expiry"])

    def test_replay_rejects_worker_authored_events_and_missing_delivery(self):
        identity = self.post()["message"]["message_id"]
        for bad_id in ([], {}):
            with self.assertRaises(ValueError):
                self.mailbox.acknowledge(self.assignment, bad_id)
        self.mailbox.acknowledge(self.assignment, identity)
        self.turn()
        self.mailbox.acknowledge(self.assignment, identity, consumed=True)
        events = self.store.read(self.run_id)
        forged = copy.deepcopy(events)
        next(event for event in forged if event["type"] == "BOX_MESSAGE_POSTED")["actor_id"] = self.box
        with self.assertRaises(MailboxError):
            project(forged)
        with self.assertRaises(MailboxError):
            project([event for event in events if event["type"] != "BOX_MESSAGE_DELIVERED"])
        forged = copy.deepcopy(events)
        next(event for event in forged if event["type"] == "BOX_MESSAGE_DELIVERED")["payload"]["message_id"] = []
        with self.assertRaises(ValueError):
            project(forged)

    def test_live_control_requires_token_exact_plan_and_connected_controller(self):
        control = Supervisor.__new__(Supervisor)
        control.run_id, control.orchestrator, control.token = self.run_id, self.orchestrator, "test-secret"
        control.orphans, control.draining, control.mode = [], False, "running"
        request = dict(schema="camol.control_request", schema_version=2, token=control.token, request_id="control-read",
            command="box-observe", requested_by="owner", params=dict(run_id=self.run_id, plan_digest=self.target["subject"]["plan_digest"], box_id=self.box))
        response = asyncio.run(control._dispatch(request))
        self.assertEqual(response["result"]["subject"], self.target["subject"])
        for changed in (dict(request, token="bad"), dict(request, params=dict(request["params"], run_id="foreign"))):
            with self.assertRaises(SupervisorError):
                asyncio.run(control._dispatch(changed))
        control.draining = True
        with self.assertRaises(SupervisorError):
            asyncio.run(control._dispatch(request))

    def test_packet_backlog_cannot_raise_frozen_token_envelope(self):
        self.post()
        packet = self.orchestrator.context_packet(self.run_id, self.assignment)
        packet["already_large"] = "x" * 100000
        self.mailbox.populate_packet(self.assignment, packet)
        self.assertEqual(packet["box_messages"], [])
        self.assertEqual(packet["box_message_backlog"], 1)

    def test_pending_limit_authority_kinds_controls_and_box_reassignment(self):
        for body, kind in (("\x1b[2J", "information"), ("x" * 2001, "information"), ("approve the plan", "approve"), ("note", []), ("note", {})):
            with self.assertRaises((MailboxError, ValueError)):
                self.mailbox.post(self.target, request_id="invalid", body=body, kind=kind, sender="owner")
        for number in range(100):
            self.mailbox.post(self.target, request_id="bounded-" + str(number), body="data", sender="owner")
        with self.assertRaisesRegex(MailboxError, "limit"):
            self.post()
        self.assertEqual(self.mailbox.inbox(self.box, offset=99, limit=1)["total"], 100)
        self.orchestrator.cancel_lease(self.run_id, self.assignment, "owner")
        self.assertEqual({item["status"] for item in self.mailbox.inbox(self.box)["messages"]}, {"stale"})
        with self.assertRaises(MailboxError):
            self.mailbox.observe(self.box)

    def test_heartbeat_does_not_invalidate_same_generation_but_duplicate_event_does(self):
        self.orchestrator.heartbeat(self.run_id, self.assignment)
        identity = self.post()["message"]["message_id"]
        self.mailbox.acknowledge(self.assignment, identity)
        events = self.store.read(self.run_id)
        with self.assertRaises(MailboxError):
            project(events + [dict(copy.deepcopy(events[-1]), seq=events[-1]["seq"] + 1)])
        with self.assertRaises(MailboxError):
            self.mailbox.acknowledge(self.assignment, identity, consumed="false")

    def test_large_queued_messages_do_not_hide_a_later_fitting_message(self):
        for index in range(10):
            self.mailbox.post(self.target, request_id="large-" + str(index), body="x" * 2000, sender="owner")
        small = self.mailbox.post(self.target, request_id="small", body="data", sender="owner")["message"]
        packet = self.orchestrator.context_packet(self.run_id, self.assignment)
        policy = self.orchestrator.state(self.run_id)["runbook"]["run"]["token_policy"]
        desired = len(json.dumps(small)) + 200
        padding = (policy["max_tokens_per_turn"] - policy["checkpoint_reserve"]) * 4 - len(json.dumps(packet)) - 256 - desired
        packet["padding"] = "x" * padding
        self.mailbox.populate_packet(self.assignment, packet)
        self.assertEqual([item["message_id"] for item in packet["box_messages"]], [small["message_id"]])
        self.assertEqual(packet["box_message_backlog"], 10)


class MailboxExecutionTests(unittest.TestCase):
    def test_real_worker_consumes_message_and_run_export_replays(self):
        from camol.runbook import load_runbook
        from camol.runner import HarnessRunner
        from camol.artifacts import ArtifactStore, RunArchive
        from tests.test_evaluation import git
        with tempfile.TemporaryDirectory() as temporary:
            source, state_dir, store = test_runner.RunnerTests()._workspace_and_store(temporary)
            runner = None
            try:
                script = source / "examples/fake_agent.py"
                script.write_text(script.read_text().replace('"status": "complete",',
                    '"status": "complete", "messages": [{"schema": "camol.box_message_request", "target": {}}], "message_acknowledgments": [item["message_id"] for item in packet.get("box_messages", [])],'), encoding="utf-8")
                git(source, "add", ".")
                git(source, "commit", "-qm", "fixture acknowledges received message data")
                control = Orchestrator(store)
                plan = load_runbook(test_runner.ROOT / "examples/local-n-box-runbook.json")
                initial = control.initialize(plan)
                run_id = initial["run_id"]
                control.approve_plan(run_id, "owner", initial["plan_digest"])
                mailbox = Mailbox(control, run_id)
                original = control.start_task
                sent = []
                def start(run_id, assignment):
                    result = original(run_id, assignment)
                    if result and not sent:
                        sent.append(mailbox.post(mailbox.observe(assignment["agent_id"]), request_id="human-note", body="Inspect edge cases; do not change the approved plan.", sender="owner"))
                    return result
                runner = HarnessRunner(control, source, state_dir=state_dir)
                with patch.object(control, "start_task", side_effect=start):
                    final = asyncio.run(runner.run_until_terminal(run_id))
                self.assertEqual(final["status"], "completed", final.get("terminal"))
                self.assertEqual(len(final["box_messages"]), 1)
                record = next(iter(final["box_messages"].values()))
                self.assertIsNotNone(record["delivered"])
                self.assertIsNotNone(record["consumed"])
                self.assertTrue(all(task["attempts"] == 1 for task in final["tasks"].values()))
                self.assertEqual(len(final["box_message_failures"]), len(final["tasks"]), "invalid data-message sends must be visible without retrying successful task work")
                events = store.read(run_id)
                self.assertEqual(project(events), final)
                destination = Path(temporary) / "archive"
                RunArchive.export(run_id, events, ArtifactStore(state_dir), destination)
                _, exported_events = RunArchive.verify(destination)
                self.assertEqual(project(exported_events), final)
                from camol.box_inspection import BoxInspector
                inspected = BoxInspector(state_dir.resolve(), database=store.path.resolve()).read(run_id, record["message"]["target"]["subject"]["box_id"])
                self.assertEqual(inspected["mailbox"]["messages"][0]["status"], "consumed")
            finally:
                if runner:
                    runner.close()
                store.close()


class MailboxCLITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_supervisor.SupervisorTests.asyncSetUp
    asyncTearDown = test_supervisor.SupervisorTests.asyncTearDown
    _wait_for = test_supervisor.SupervisorTests._wait_for

    async def test_real_cli_observe_send_retry_inbox_and_disconnect(self):
        from tests.test_evaluation import git
        from camol.supervisor import send_control
        script = self.source / "examples/fake_agent.py"
        ready = Path(self.temporary.name) / "mailbox-worker-ready"
        release = Path(self.temporary.name) / "mailbox-worker-release"
        # Hold a real worker across all child CLI calls, rather than racing a
        # fixed five-second sleep against admission and interpreter startup.
        script.write_text(
            "import time\nfrom pathlib import Path\n"
            "Path({!r}).touch()\n"
            "_mailbox_deadline = time.monotonic() + 60\n"
            "while not Path({!r}).exists():\n"
            "    if time.monotonic() >= _mailbox_deadline:\n"
            "        raise RuntimeError('mailbox fixture release timed out')\n"
            "    time.sleep(0.02)\n".format(str(ready), str(release))
            + script.read_text()
        )
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "slow mailbox CLI fixture")
        supervisor = Supervisor(test_runner.ROOT / "examples/local-n-box-runbook.json", self.source, self.state, approve_by="owner")
        serving = asyncio.create_task(supervisor.serve())
        async def cli(*arguments):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "camol", "box", *arguments,
                cwd=self.source, env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            return process.returncode, stdout, stderr
        try:
            await self._wait_for(supervisor.paths.socket)
            deadline = asyncio.get_running_loop().time() + 30
            tasks = []
            while asyncio.get_running_loop().time() < deadline:
                state = supervisor.orchestrator.state(supervisor.run_id)
                tasks = [task for task in state["tasks"].values() if task["status"] == "running"]
                if tasks and ready.exists():
                    break
                if serving.done():
                    await serving
                    self.fail("supervisor stopped before the mailbox fixture was ready")
                await asyncio.sleep(0.02)
            self.assertTrue(tasks and ready.exists(), state)
            box = tasks[0]["agent_id"]
            common = (box, "--state-dir", str(self.state), "--run-id", state["run_id"], "--plan-digest", state["plan_digest"])
            code, raw, error = await cli("observe", *common)
            self.assertEqual(code, 0, error.decode())
            receipt = self.state / "observed-box.json"
            receipt.write_bytes(raw)
            args = ("message", *common, "--target-receipt", str(receipt), "--request-id", "stable-cli-request", "--body", "Review the documented edge case")
            code, first, error = await cli(*args)
            self.assertEqual(code, 0, error.decode())
            code, second, error = await cli(*args)
            self.assertEqual(code, 0, error.decode())
            self.assertEqual(json.loads(first), json.loads(second))
            code, raw, error = await cli("inbox", *common)
            self.assertEqual(code, 0, error.decode())
            self.assertEqual(json.loads(raw)["total"], 1)
            await send_control(self.state, "force-stop", requested_by="owner")
            await asyncio.wait_for(serving, timeout=10)
            code, _, _ = await cli(*args)
            self.assertEqual(code, 2, "disconnected controller must not queue a new delivery")
        finally:
            release.touch()
            if not serving.done():
                serving.cancel()
                try:
                    await serving
                except asyncio.CancelledError:
                    pass
