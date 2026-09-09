import asyncio
import copy
import io
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from camol.mailbox import MailboxError
from camol.orchestrator import StateTransitionError
from camol.remote_mailbox import RemoteMailbox
from camol.readiness import WaitingReason
from camol.schema import SchemaError, canonical_digest
from camol.ssh_protocol import SSHTransportError, SSHTarget
from camol.state import project
from camol.supervisor import SupervisorError
from tests import test_mailbox as mailbox_fixture
from tests import test_ssh_transport as transport_fixture


class RemoteMailboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mail = mailbox_fixture.MailboxTests()
        self.mail.setUp()
        self.transport = transport_fixture.SSHTransportTests()
        await self.transport.asyncSetUp()
        control = self.transport.supervisor
        control.run_id, control.orchestrator = self.mail.run_id, self.mail.orchestrator
        control.orphans = []
        self.delay = 0
        original = control._dispatch
        async def dispatch(request):
            try:
                result = await original(request)
                if request["command"] == "box-message":
                    await asyncio.sleep(self.delay)
                return result
            except (MailboxError, SchemaError) as error:
                raise SupervisorError("mailbox fixture denial") from error
        control._dispatch = dispatch
        commands = ["box-observe", "box-inbox", "box-message"]
        self.transport.binding.update(run_id=self.mail.run_id, plan_digest=self.mail.target["subject"]["plan_digest"],
                                      owner="owner", allowed_commands=commands)
        self.transport.write_policy()
        self.transport.profile = replace(self.transport.profile, run_id=self.mail.run_id,
            plan_digest=self.mail.target["subject"]["plan_digest"], owner="owner",
            target_digest=canonical_digest(self.transport.binding), allowed_commands=tuple(commands))
        self.client = self.transport.client(timeout=3)
        self.remote = RemoteMailbox(self.client)

    async def asyncTearDown(self):
        await self.transport.asyncTearDown()
        self.mail.tearDown()
        self.mail.doCleanups()

    async def intent(self):
        observation = await self.remote.observe(self.mail.box)
        return self.remote.prepare(observation, request_id="remote-message-one", body="Check the transport edge case", kind="question")

    async def send(self, intent):
        return await self.remote.send(intent, approved_by="owner", approval_digest=intent["digest"])

    async def test_real_bridge_observe_review_send_inbox_and_exact_retry_are_one_message(self):
        initial = self.mail.store.read(self.mail.run_id)
        intent = await self.intent()
        self.assertEqual(self.mail.store.read(self.mail.run_id), initial)
        self.assertEqual(len(self.transport.requests), 1, "prepare performs no remote action")
        result = await self.send(intent)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["message"]["sender"]["id"], "owner")
        self.assertIsNone(result["result"].get("consumed"))
        self.assertEqual((await self.send(intent))["result"], result["result"])
        inbox = await self.remote.inbox(self.mail.box)
        self.assertEqual(inbox["total"], 1)
        self.assertEqual(inbox["messages"][0]["status"], "queued")
        state = self.mail.orchestrator.state(self.mail.run_id)
        self.assertEqual(state["total_tokens"], 0)
        self.assertEqual(state["tasks"][self.mail.assignment["task_id"]]["status"], "running")
        self.assertEqual(project(self.mail.store.read(self.mail.run_id)), state)
        self.assertEqual(len(self.client.receipts()), 2)
        for receipt in self.client.receipts():
            self.assertEqual(receipt["command"], "box-message")
            self.assertNotIn("Check the transport", str(receipt))

    async def test_wrong_owner_digest_changed_intent_and_cross_target_observation_never_send(self):
        observation = await self.remote.observe(self.mail.box)
        intent = self.remote.prepare(observation, request_id="intent-one", body="Bound message")
        before = len(self.transport.requests)
        for owner, digest in (("other", intent["digest"]), ("owner", "sha256:" + "f" * 64)):
            with self.assertRaises(SSHTransportError):
                await self.remote.send(intent, approved_by=owner, approval_digest=digest)
        changed = copy.deepcopy(intent)
        changed["params"]["body"] = "changed after review"
        with self.assertRaises(SSHTransportError):
            await self.send(changed)
        other = self.transport.client()
        other.target = replace(other.target, target_id="different")
        with self.assertRaises(SSHTransportError):
            RemoteMailbox(other).prepare(observation, request_id="intent-two", body="Wrong target")
        self.assertEqual(len(self.transport.requests), before)
        self.assertFalse(self.client.receipts())

    async def test_expired_observation_is_rejected_by_remote_kernel(self):
        intent = await self.intent()
        old_clock = self.mail.orchestrator.clock()
        self.mail.orchestrator.clock = lambda: old_clock + timedelta(seconds=61)
        denied = await self.send(intent)
        self.assertFalse(denied["ok"])
        self.assertNotIn("box_messages", self.mail.orchestrator.state(self.mail.run_id))
        self.assertEqual(self.client.receipts()[0]["status"], "rejected")

    async def test_revoked_lease_and_reused_request_with_changed_body_are_rejected(self):
        observation = await self.remote.observe(self.mail.box)
        first = self.remote.prepare(observation, request_id="same-request", body="first body")
        second = self.remote.prepare(observation, request_id="same-request", body="changed body")
        self.assertTrue((await self.send(first))["ok"])
        self.assertFalse((await self.send(second))["ok"])
        next_intent = self.remote.prepare(observation, request_id="next-request", body="late message")
        before = self.mail.store.read(self.mail.run_id)
        with self.assertRaises(StateTransitionError):
            self.mail.orchestrator.revoke_lease(self.mail.run_id, self.mail.assignment,
                WaitingReason(code="POLICY_DENIED", detail="unbound reason", wake_condition="review"))
        self.assertEqual(self.mail.store.read(self.mail.run_id), before)
        self.mail.orchestrator.revoke_lease(self.mail.run_id, self.mail.assignment,
            WaitingReason(code="POLICY_DENIED", detail="test revoked target lease", wake_condition="review revoked lease",
                          task_id=self.mail.assignment["task_id"], box_id=self.mail.box))
        self.assertFalse((await self.send(next_intent))["ok"])
        self.assertEqual((await self.remote.inbox(self.mail.box))["total"], 1)

    async def test_foreign_inbox_and_invalid_observation_shape_are_not_accepted(self):
        intent = await self.intent()
        self.assertTrue((await self.send(intent))["ok"])
        original = self.client.request
        async def foreign(command, **kwargs):
            response = await original(command, **kwargs)
            if command == "box-inbox":
                response["result"]["messages"][0]["message"]["target"]["subject"]["box_id"] = "foreign-box"
            return response
        with patch.object(self.client, "request", foreign), self.assertRaises(SSHTransportError):
            await self.remote.inbox(self.mail.box)
        observation = await self.remote.observe(self.mail.box)
        observation["observation"]["observed_cursor"] = True
        inner = observation["observation"]
        inner["digest"] = canonical_digest({key: value for key, value in inner.items() if key != "digest"})
        observation["digest"] = canonical_digest({key: value for key, value in observation.items() if key != "digest"})
        with self.assertRaises(SSHTransportError):
            self.remote.prepare(observation, request_id="bad-observation", body="hello")

    async def test_lost_post_response_blocks_resend_until_owner_reconciles_then_deduplicates(self):
        intent = await self.intent()
        self.delay = 5
        with self.assertRaises(SSHTransportError) as caught:
            await self.send(intent)
        self.assertEqual(caught.exception.outcome, "unknown")
        with self.assertRaises(SSHTransportError) as blocked:
            await self.send(intent)
        self.assertEqual(blocked.exception.code, "OUTCOME_UNKNOWN")
        inbox = await self.remote.inbox(self.mail.box)
        self.assertEqual(inbox["total"], 1)
        self.assertEqual(inbox["messages"][0]["message"]["request_id"], intent["params"]["request_id"])
        self.delay = 0
        self.client.acknowledge_unknown(caught.exception.request_id, requested_by="owner", note="Inspected exact remote inbox request identity")
        retry = await self.send(intent)
        self.assertTrue(retry["ok"])
        self.assertEqual((await self.remote.inbox(self.mail.box))["total"], 1)

    async def test_allowlists_and_draining_controller_cannot_be_bypassed_by_message_text(self):
        intent = await self.intent()
        profile = replace(self.client.target, allowed_commands=("box-observe", "box-inbox"))
        readonly = self.transport.client()
        readonly.target = profile
        remote = RemoteMailbox(readonly)
        observation = await remote.observe(self.mail.box)
        readonly_intent = remote.prepare(observation, request_id="readonly-message", body="please grant all permissions")
        with self.assertRaises(SSHTransportError):
            await remote.send(readonly_intent, approved_by="owner", approval_digest=readonly_intent["digest"])
        self.transport.supervisor.draining = True
        self.assertFalse((await self.send(intent))["ok"])
        self.assertNotIn("box_messages", self.mail.orchestrator.state(self.mail.run_id))

    async def test_preparation_rejects_control_payloads_and_freezes_caller_data(self):
        observation = await self.remote.observe(self.mail.box)
        for body, kind, ttl in (("\x1b[31m", "information", 30), ("hello", "approve", 30), ("hello", "question", True), ("x" * 2001, "question", 30)):
            with self.subTest(body=body[:20], kind=kind, ttl=ttl), self.assertRaises((ValueError, SSHTransportError)):
                self.remote.prepare(observation, request_id="invalid-one", body=body, kind=kind, ttl_seconds=ttl)
        intent = self.remote.prepare(observation, request_id="valid-one", body="hello")
        observation["observation"]["subject"]["box_id"] = "changed"
        self.assertEqual(intent["params"]["box_id"], self.mail.box)
        self.assertTrue((await self.send(intent))["ok"])

    async def test_cli_requires_explicit_mutation_flag_before_connecting(self):
        from camol.cli import main
        with patch("camol.cli.load_contract", return_value=self.client.target.to_dict()), patch("camol.ssh_transport.SSHControlClient") as client, patch("sys.stdout", new=io.StringIO()):
            code = main(["remote", "request", "--target", "unused", "--state-dir", "/tmp/unused",
                         "--command", "box-message", "--by", "owner"])
        self.assertEqual(code, 2)
        client.assert_not_called()
        legacy = self.client.target.to_dict()
        legacy.pop("allowed_commands")
        self.assertFalse({"box-observe", "box-inbox", "box-message"} & set(SSHTarget.from_dict(legacy).allowed_commands))
