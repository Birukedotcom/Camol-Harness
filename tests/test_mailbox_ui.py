import asyncio
import copy
import getpass
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from camol.app import InteractiveController
from camol.mailbox import Mailbox
from camol.mailbox_outbox import Outbox, OutboxError
from camol.schema import canonical_digest
from camol.supervisor import SupervisorError
from tests import test_mailbox


class MailboxUITests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_mailbox.MailboxTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        fixture = self.fixture
        self.controller = InteractiveController(fixture.source, state_root=fixture.source.parent / "ui-state")
        self.addCleanup(self.controller.close_client)
        self.plan = dict(schema="camol.product_plan", schema_version=2, proposal=None, run_id=fixture.run_id,
                         runbook=fixture.runbook, execution_status="ready", execution_limitation="fixture",
                         source=dict(workspace=str(fixture.source), revision="a" * 40))
        self.controller.session = self.controller.store.update(self.controller.session, plan=self.plan,
            plan_digest=canonical_digest(self.plan), run_id=fixture.run_id, state_dir=str(fixture.state_dir), selected_box=fixture.box)
        self.calls = []
        self.controller._control = Mock(side_effect=self.control)
        self.controller._pane_state = Mock(side_effect=lambda: (fixture.orchestrator.state(fixture.run_id), "ledger_snapshot"))
        self.controller.converse_fn = Mock(side_effect=AssertionError("message UI cannot call a model"))
        self.controller.spawn_fn = Mock(side_effect=AssertionError("message UI cannot spawn"))
        self.controller.connections.refresh = Mock(side_effect=AssertionError("message UI cannot probe accounts"))
        self.outbox = Outbox(self.controller.store.project_dir)

    def control(self, command, params=None):
        self.calls.append((command, copy.deepcopy(params)))
        mailbox = self.fixture.mailbox
        if command == "box-observe":
            return mailbox.observe(params["box_id"])
        if command == "box-message":
            return mailbox.post(params["target"], request_id=params["request_id"], body=params["body"], sender=getpass.getuser(),
                                kind=params["kind"], correlation_id=params["correlation_id"], ttl_seconds=params["ttl_seconds"])
        if command == "box-inbox":
            return mailbox.inbox(params["box_id"], offset=params.get("offset", 0), limit=params.get("limit", 100))
        raise SupervisorError("fixture control command unavailable")

    def test_send_inbox_and_outbox_preserve_plan_authority(self):
        before = self.controller.store.load()
        result = self.controller.handle('/message {} "[red] check edge cases"'.format(self.fixture.box))
        self.assertIn("accepted", result.messages[0])
        saved = self.outbox.list()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["status"], "accepted_not_consumed")
        inbox = self.controller.handle("/inbox")
        self.assertEqual(inbox.box_view, "inbox")
        self.assertEqual(inbox.box_id, self.fixture.box)
        self.assertIn("[red] check edge cases", inbox.messages[0])
        self.assertIn("accepted_not_consumed", self.controller.handle("/outbox").messages[0])
        for key in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir", "status"):
            self.assertEqual(before[key], self.controller.store.load()[key])
        self.controller.converse_fn.assert_not_called()
        self.controller.spawn_fn.assert_not_called()
        self.controller.connections.refresh.assert_not_called()

    def test_lost_response_restart_retry_reuses_exact_intent_without_new_observation(self):
        def lost(command, params=None):
            value = self.control(command, params)
            if command == "box-message":
                raise OSError("response lost after commit")
            return value
        self.controller._control.side_effect = lost
        result = self.controller.handle("/message {} hello".format(self.fixture.box))
        self.assertIn("unconfirmed", result.messages[0])
        record = self.outbox.list()[0]
        self.assertEqual(record["status"], "unknown_or_not_sent")
        original = record["intent"]["params"]
        second = InteractiveController(self.fixture.source, state_root=self.fixture.source.parent / "ui-state")
        self.addCleanup(second.close_client)
        second._control = Mock(side_effect=self.control)
        reply = second.handle("/message retry " + original["request_id"])
        self.assertIn("accepted", reply.messages[0])
        self.assertEqual(second._control.call_count, 1)
        self.assertEqual(second._control.call_args.args, ("box-message", original))
        self.assertEqual(len(self.fixture.orchestrator.state(self.fixture.run_id)["box_messages"]), 1)

    def test_retry_after_plan_change_does_not_send_or_retarget(self):
        self.controller.handle("/message {} note".format(self.fixture.box))
        identity = self.outbox.list()[0]["intent"]["params"]["request_id"]
        plan = copy.deepcopy(self.plan)
        plan["execution_limitation"] = "new plan"
        self.controller.session = self.controller.store.update(self.controller.session, plan=plan, plan_digest=canonical_digest(plan))
        self.controller._control.reset_mock()
        self.assertIn("will not be retargeted", self.controller.handle("/message retry " + identity).messages[0])
        self.controller._control.assert_not_called()

    def test_reply_preserves_original_worker_lease_and_correlation(self):
        other = self.fixture.assignments[1]
        message = self.fixture.mailbox.post(self.fixture.target, request_id="worker-question", body="Question", sender=other["agent_id"], assignment=other)["message"]
        response = self.controller.handle("/reply {} answer".format(message["message_id"]))
        self.assertIn("accepted", response.messages[0])
        sent = self.outbox.list()[0]["intent"]["params"]
        self.assertEqual(sent["target"]["subject"], message["reply_to"])
        self.assertEqual(sent["correlation_id"], message["correlation_id"])
        self.fixture.orchestrator.cancel_lease(self.fixture.run_id, other, "owner")
        self.assertIn("denied:", self.controller.handle("/reply {} late answer".format(message["message_id"])).messages[0])
        self.assertEqual(len(self.outbox.list()), 1)

    def test_cancel_after_observation_does_not_publish_or_send(self):
        def cancelled(command, params=None):
            value = self.control(command, params)
            self.controller._cancel_event.set()
            return value
        self.controller._control.side_effect = cancelled
        self.assertIn("cancelled", self.controller.handle("/message {} note".format(self.fixture.box)).messages[0])
        self.assertEqual(self.outbox.list(), [])
        self.assertEqual([name for name, _ in self.calls], ["box-observe"])

    def test_corrupt_linked_and_foreign_outbox_records_deny_retry(self):
        self.controller.handle("/message {} note".format(self.fixture.box))
        record = self.outbox.list()[0]
        identity = record["intent"]["params"]["request_id"]
        path = self.outbox.path / (identity + ".json")
        original = path.read_bytes()
        path.write_text("not json")
        self.assertIn("denied:", self.controller.handle("/message retry " + identity).messages[0])
        path.write_bytes(original)
        os.chmod(path, 0o644)
        with self.assertRaises(OutboxError):
            self.outbox.get(identity)
        os.chmod(path, 0o600)
        copy_path = self.outbox.path / "linked-record"
        copy_path.write_bytes(original)
        path.unlink()
        path.symlink_to(copy_path)
        self.assertIn("denied:", self.controller.handle("/message retry " + identity).messages[0])

    def test_bad_live_inbox_never_falls_back_to_cached_data_or_crashes(self):
        self.controller._control.side_effect = lambda *args: dict(schema="camol.box_inbox", schema_version=1, run_id="foreign")
        result = self.controller.inspect_box(self.fixture.box, "inbox")
        self.assertIn("Invalid live response", result)
        self.assertNotIn("RETAINED", result)

    def test_outbox_identifiers_cannot_traverse_paths_even_with_valid_intent(self):
        self.controller.handle("/message {} note".format(self.fixture.box))
        record = self.outbox.list()[0]["intent"]
        for identity in ("a/../../escaped", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                self.outbox.get(identity)
            params = dict(record["params"], request_id=identity)
            with self.assertRaises(ValueError):
                self.outbox.put(record["scope"], record["sender"], params)
        self.assertEqual(len(self.outbox.list()), 1)

    def test_saved_acceptance_identity_and_unhashable_intent_are_rejected(self):
        self.controller.handle("/message {} note".format(self.fixture.box))
        record = self.outbox.list()[0]
        identity = record["intent"]["params"]["request_id"]
        receipt_path = self.outbox.path / (identity + ".accepted.json")
        receipt = json.loads(receipt_path.read_text())
        receipt["message_id"] = "foreign-message"
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(OutboxError, "another message"):
            self.outbox.status(identity)
        intent = record["intent"]
        intent["params"]["kind"] = {}
        intent["digest"] = canonical_digest({key: value for key, value in intent.items() if key != "digest"})
        (self.outbox.path / (identity + ".json")).write_text(json.dumps(intent))
        self.controller._control.reset_mock()
        self.assertIn("denied:", self.controller.handle("/message retry " + identity).messages[0])
        self.controller._control.assert_not_called()

    def test_outbox_capacity_reserves_acceptance_for_unknown_sends(self):
        from unittest.mock import patch
        self.controller.handle("/message {} note".format(self.fixture.box))
        record = self.outbox.list()[0]
        intent = record["intent"]
        params = copy.deepcopy(intent["params"])
        params["request_id"] = "new-send"
        pending = [self.outbox.path / ("pending-{}.json".format(index)) for index in range(999)]
        with patch.object(Path, "iterdir", return_value=iter(pending)):
            self.outbox.put(intent["scope"], intent["sender"], params)
        params["request_id"] = "over-capacity"
        with patch.object(Path, "iterdir", return_value=iter(pending + [self.outbox.path / "new-send.json"])):
            with self.assertRaisesRegex(OutboxError, "retention limit"):
                self.outbox.put(intent["scope"], intent["sender"], params)
        self.assertFalse((self.outbox.path / "over-capacity.json").exists())
        self.assertEqual(self.outbox.put(intent["scope"], intent["sender"], intent["params"]), intent)

    def test_retained_inbox_survives_disconnect_without_a_delivery_claim(self):
        from camol.store import SQLiteEventStore
        self.controller.handle("/message {} retained note".format(self.fixture.box))
        copied = SQLiteEventStore(self.fixture.state_dir / "camol.sqlite3")
        try:
            self.fixture.store.connection.backup(copied.connection)
        finally:
            copied.close()
        self.controller._control.side_effect = SupervisorError("disconnected")
        result = self.controller.handle("/inbox")
        self.assertEqual(result.box_view, "inbox")
        self.assertIn("RETAINED", result.messages[0])
        self.assertIn("retained note", result.messages[0])
        self.assertIn("not live delivery proof", result.messages[0])

    def test_wrong_acceptance_identity_remains_unknown_and_cancel_after_save_is_unsent(self):
        def wrong(command, params=None):
            value = self.control(command, params)
            if command == "box-message":
                value["message"]["message_id"] = "foreign-message"
            return value
        self.controller._control.side_effect = wrong
        self.assertIn("unconfirmed", self.controller.handle("/message {} note".format(self.fixture.box)).messages[0])
        self.assertEqual(self.outbox.list()[0]["status"], "unknown_or_not_sent")
        from unittest.mock import patch
        original = Outbox.put
        def cancel(outbox, *args, **kwargs):
            intent = original(outbox, *args, **kwargs)
            self.controller._cancel_event.set()
            return intent
        self.controller._control.side_effect = self.control
        self.controller._control.reset_mock()
        with patch.object(Outbox, "put", cancel):
            result = self.controller.handle("/message {} second note".format(self.fixture.box))
        self.assertIn("not dispatched", result.messages[0])
        self.assertEqual([call.args[0] for call in self.controller._control.call_args_list], ["box-observe"])
        self.assertEqual(len(self.outbox.list()), 2)
