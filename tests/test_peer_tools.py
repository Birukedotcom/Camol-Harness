import asyncio
import copy
from datetime import timedelta
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.mailbox import MailboxError
from camol.peer_tools import PeerTools, EVENT, apply
from camol.schema import canonical_digest
from camol.state import project
from tests import test_mailbox, test_runner


class PeerToolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_mailbox.MailboxTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.control = self.fixture.orchestrator
        self.run = self.fixture.run_id
        self.tools = PeerTools(self.control, self.run, self.fixture.assignment, 1)

    def test_observation_is_durable_exact_and_retry_is_not_refresh(self):
        before = self.control.state(self.run)
        first = self.tools.call("list", dict(offset=0, limit=50), request_id="list")
        self.assertEqual(len(first["result"]["boxes"]), 3)
        self.assertEqual(self.tools.call("list", dict(offset=0, limit=50), request_id="list"), first)
        with self.assertRaises(MailboxError):
            self.tools.call("list", dict(offset=1, limit=50), request_id="list")
        now = self.control.state(self.run)
        self.assertEqual(now["total_tokens"], before["total_tokens"])
        self.assertEqual(now["tasks"], before["tasks"])
        self.assertEqual(len(now["peer_tool_reads"]), 1)
        self.assertEqual(project(self.fixture.store.read(self.run)), now)

    def test_peer_send_requires_recorded_read_and_binds_sender_and_exact_target(self):
        other = self.fixture.assignments[1]
        target = self.fixture.mailbox.observe(other["agent_id"])
        with self.assertRaisesRegex(MailboxError, "recorded"):
            self.tools.send(target, request_id="note", body="not yet observed")
        observed = self.tools.call("observe", dict(box_id=other["agent_id"]), request_id="read-peer")["result"]
        record = self.tools.send(observed, request_id="note", body="[red] check the invariant")
        self.assertEqual(record["message"]["sender"]["id"], self.fixture.assignment["agent_id"])
        self.assertEqual(record["message"]["target"], observed)
        self.assertEqual(self.tools.send(observed, request_id="note", body="[red] check the invariant"), record)
        self.assertEqual(len(self.control.state(self.run)["box_messages"]), 1)
        recipient = PeerTools(self.control, self.run, other, 1)
        received = recipient.call("inbox", dict(offset=0, limit=10), request_id="inbox")["result"]
        self.assertEqual(received["messages"][0]["status"], "queued")
        with self.assertRaises(ValueError):
            recipient.call("inbox", dict(box_id=self.fixture.box, offset=0, limit=10), request_id="foreign")

    def test_closed_stale_expired_and_unowned_turns_cannot_act(self):
        self.tools.close()
        with self.assertRaisesRegex(MailboxError, "closed"):
            self.tools.call("list", dict(offset=0, limit=1), request_id="closed")
        with self.assertRaises(MailboxError):
            PeerTools(self.control, self.run, self.fixture.assignment, True)
        owned = PeerTools(self.control, self.run, self.fixture.assignment, 1, active=lambda: False)
        with self.assertRaises(MailboxError):
            owned.call("list", dict(offset=0, limit=1), request_id="unowned")
        old = PeerTools(self.control, self.run, self.fixture.assignment, 1)
        self.fixture.turn()
        with self.assertRaises(MailboxError):
            old.call("list", dict(offset=0, limit=1), request_id="old-turn")
        newer = PeerTools(self.control, self.run, self.fixture.assignment, 2)
        self.control.cancel_lease(self.run, self.fixture.assignment, "owner")
        with self.assertRaises(MailboxError):
            newer.call("list", dict(offset=0, limit=1), request_id="revoked")

    def test_read_retry_never_renews_expired_receipt_or_retargets_changed_peer(self):
        from camol.schema import parse_timestamp
        other = self.fixture.assignments[1]
        value = self.tools.call("observe", dict(box_id=other["agent_id"]), request_id="read")
        at = parse_timestamp(value["result"]["expires_at"], "expiry")
        with patch.object(self.control, "_now", return_value=at.isoformat()):
            self.assertEqual(self.tools.call("observe", dict(box_id=other["agent_id"]), request_id="read"), value)
            with self.assertRaisesRegex(MailboxError, "expired"):
                self.tools.send(value["result"], request_id="late", body="late")
        self.control.cancel_lease(self.run, other, "owner")
        with self.assertRaises(MailboxError):
            self.tools.send(value["result"], request_id="changed", body="late")

    def test_replay_rejects_tampered_actor_result_turn_digest_or_duplicate(self):
        self.tools.call("list", dict(offset=0, limit=1), request_id="read")
        events = self.fixture.store.read(self.run)
        for mutation in ("actor", "result", "turn", "digest", "types"):
            forged = copy.deepcopy(events)
            event = next(event for event in forged if event["type"] == EVENT)
            if mutation == "actor":
                event["actor_id"] = self.fixture.box
            elif mutation == "result":
                event["payload"]["result"]["boxes"][0]["status"] = "succeeded"
            elif mutation == "turn":
                event["payload"]["turn_number"] = 9
            elif mutation == "types":
                event["payload"]["result"]["more"] = int(event["payload"]["result"]["more"])
            else:
                event["payload"]["digest"] = "sha256:" + "0" * 64
            if mutation != "digest":
                event["payload"]["digest"] = canonical_digest({key: item for key, item in event["payload"].items() if key != "digest"})
            with self.assertRaises(ValueError):
                project(forged)
        with self.assertRaises(ValueError):
            apply(self.control.state(self.run), events[-1])

    def test_limits_and_control_commands_cannot_expand_authority(self):
        for op, args in (("approve", {}), ("force-stop", {}), ([], {}), ("list", dict(offset=True, limit=1)),
                         ("list", dict(offset=0, limit=51)), ("inbox", dict(offset=0, limit=11))):
            with self.assertRaises(ValueError):
                self.tools.call(op, args, request_id="invalid")
        for index in range(64):
            self.tools.call("list", dict(offset=0, limit=1), request_id="read-{}".format(index))
        with self.assertRaisesRegex(MailboxError, "allowance"):
            self.tools.call("list", dict(offset=0, limit=1), request_id="overflow")

    def test_runner_closes_tool_object_on_adapter_failure(self):
        from camol.runner import HarnessRunner
        captured = []
        class Adapter:
            supports_peer_tools = True
            async def execute_turn(adapter, *args, **kwargs):
                captured.append(adapter.peer_tools)
                adapter.peer_tools.call("list", dict(offset=0, limit=1), request_id="read")
                raise failure("fixture fails after observing")
        runner = HarnessRunner(self.control, self.fixture.source, state_dir=self.fixture.state_dir)
        self.addCleanup(runner.close)
        for failure in (OSError, asyncio.CancelledError):
            adapter = Adapter()
            with self.assertRaises(failure):
                asyncio.run(runner._tracked_provider_turn(adapter, self.run, {}, self.fixture.assignment, {}, 1))
            self.assertIsNone(adapter.peer_tools)
            self.assertFalse(runner._provider_turns)
            with self.assertRaisesRegex(MailboxError, "closed"):
                captured[-1].call("list", dict(offset=0, limit=1), request_id="late")


class PeerToolExecutionTests(unittest.TestCase):
    def test_embedded_adapter_observes_during_real_build_and_export_replays(self):
        from camol.artifacts import ArtifactStore, RunArchive
        from camol.orchestrator import Orchestrator
        from camol.runbook import load_runbook
        from camol.runner import HarnessRunner
        from camol import runner as runner_module
        with tempfile.TemporaryDirectory() as temporary:
            source, state_dir, store = test_runner.RunnerTests()._workspace_and_store(temporary)
            runner = None
            captured = []
            try:
                control = Orchestrator(store)
                state = control.initialize(load_runbook(test_runner.ROOT / "examples/local-n-box-runbook.json"))
                control.approve_plan(state["run_id"], "owner", state["plan_digest"])
                original_factory = runner_module.create_agent_adapter
                def factory(*args, **kwargs):
                    adapter = original_factory(*args, **kwargs)
                    original = adapter.execute_turn
                    adapter.supports_peer_tools = True
                    async def execute(*args, **kwargs):
                        tools = adapter.peer_tools
                        captured.append(tools)
                        result = tools.call("list", dict(offset=0, limit=50), request_id="list")
                        self.assertEqual(result["result"]["run_id"], state["run_id"])
                        return await original(*args, **kwargs)
                    adapter.execute_turn = execute
                    return adapter
                runner = HarnessRunner(control, source, state_dir=state_dir, adapter_factory=factory)
                final = asyncio.run(runner.run_until_terminal(state["run_id"]))
                self.assertEqual(final["status"], "completed", final.get("terminal"))
                self.assertTrue(captured)
                self.assertTrue(all(tools.closed for tools in captured))
                self.assertEqual(len(final["peer_tool_reads"]), len(final["tasks"]))
                self.assertTrue(all(task["attempts"] == 1 for task in final["tasks"].values()))
                events = store.read(state["run_id"])
                self.assertEqual(project(events), final)
                destination = Path(temporary) / "archive"
                RunArchive.export(state["run_id"], events, ArtifactStore(state_dir), destination)
                _, exported = RunArchive.verify(destination)
                self.assertEqual(project(exported), final)
                from camol.box_inspection import BoxInspector
                report = BoxInspector(state_dir.resolve(), database=store.path.resolve()).read(
                    state["run_id"], captured[0].caller["box_id"], previews=False)
                self.assertTrue(any(event["type"] == EVENT for event in report["events"]))
            finally:
                if runner:
                    runner.close()
                store.close()
