import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from camol.diagnostics import event_metadata, profile_run
from camol.peer_telemetry import START, FINISH, apply, attempt, profile
from camol.schema import canonical_digest
from camol.state import project
from tests import test_peer_tools


class PeerTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_peer_tools.PeerToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tools, self.control, self.run = self.fixture.tools, self.fixture.control, self.fixture.run

    def records(self):
        return self.control.state(self.run)["peer_tool_calls"]

    def test_retries_failures_and_elapsed_are_content_free_and_replayable(self):
        result = self.tools.call("list", dict(offset=0, limit=2), request_id="list")
        self.assertEqual(self.tools.call("list", dict(offset=0, limit=2), request_id="list"), result)
        with self.assertRaises(ValueError):
            self.tools.call("forbidden-private-prose", dict(secret="private-argument"), request_id="bad")
        records = list(self.records().values())
        self.assertEqual([item["result"]["outcome"] for item in records], ["success", "success", "error"])
        self.assertEqual([item["result"]["reused"] for item in records], [False, True, False])
        self.assertTrue(all(item["result"]["elapsed_ns"] >= 0 for item in records))
        self.assertNotIn("private-prose", json.dumps(records))
        self.assertNotIn("private-argument", json.dumps(records))
        events = self.fixture.fixture.store.read(self.run)
        self.assertEqual(project(events), self.control.state(self.run))
        report = profile_run(events)
        by_op = {row["operation"]: row for row in report["peer_tools"]["operations"]}
        self.assertEqual(by_op["list"]["attempts"], 2)
        self.assertEqual(by_op["list"]["reused"], 1)
        self.assertEqual(by_op["unsupported"]["error"], 1)
        self.assertNotIn("private-argument", json.dumps(report))
        metadata = [event_metadata(event) for event in events]
        self.assertTrue(any("peer_call" in value for value in metadata))
        self.assertNotIn("private-prose", json.dumps(metadata))
        import camol
        package_root = Path(camol.__file__).resolve().parents[1]
        cli = subprocess.run([sys.executable, "-m", "camol", "profile", "--db",
            str(self.fixture.fixture.store.path), "--run-id", self.run], cwd=str(package_root),
            env=dict(os.environ, PYTHONPATH=str(package_root)), capture_output=True, text=True, timeout=15)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["peer_tools"], report["peer_tools"])

    def test_lost_finish_remains_unknown_even_when_message_was_committed(self):
        from camol import peer_telemetry
        other = self.fixture.fixture.assignments[1]
        target = self.tools.call("observe", dict(box_id=other["agent_id"]), request_id="target")["result"]
        original = peer_telemetry.emit
        def lost(control, run, kind, value):
            if kind == FINISH:
                raise OSError("fixture lost finish publication")
            return original(control, run, kind, value)
        with patch.object(peer_telemetry, "emit", side_effect=lost):
            with self.assertRaises(OSError):
                self.tools.send(target, request_id="note", body="private-message-prose")
        state = self.control.state(self.run)
        self.assertEqual(len(state["box_messages"]), 1)
        self.assertIsNone(list(self.records().values())[-1]["result"])
        row = next(row for row in profile(self.control.state(self.run))["operations"] if row["operation"] == "send")
        self.assertEqual(row["unknown"], 1)
        self.assertIsNone(row["observed_elapsed_ns"])
        self.tools.send(target, request_id="note", body="private-message-prose")
        state = self.control.state(self.run)
        self.assertEqual(len(state["box_messages"]), 1)
        send = next(row for row in profile(state)["operations"] if row["operation"] == "send")
        self.assertEqual((send["unknown"], send["success"], send["reused"]), (1, 1, 1))
        self.assertNotIn("private-message-prose", json.dumps(state["peer_tool_calls"]))

    def test_interruption_is_logged_and_revocation_can_finish_measurement_only(self):
        import asyncio
        def interrupted():
            self.control.cancel_lease(self.run, self.fixture.fixture.assignment, "owner")
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            attempt(self.tools, "list", dict(offset=0, limit=1), "cancel", interrupted)
        record = list(self.records().values())[-1]
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertIsNone(record["result"]["reference"])
        before = len(self.records())
        with self.assertRaises(ValueError):
            self.tools.call("list", dict(offset=0, limit=1), request_id="revoked")
        self.assertEqual(len(self.records()), before)

    def test_failure_log_loss_preserves_original_error_and_open_intent(self):
        from camol import peer_telemetry
        original = peer_telemetry.emit
        def lost(control, run, kind, value):
            if kind == FINISH:
                raise OSError("audit unavailable")
            return original(control, run, kind, value)
        with patch.object(peer_telemetry, "emit", side_effect=lost):
            with self.assertRaisesRegex(ValueError, "only"):
                self.tools.call("approve", {}, request_id="denied")
        self.assertIsNone(list(self.records().values())[-1]["result"])

    def test_replay_cannot_forge_success_elapsed_actor_or_retry(self):
        self.tools.call("list", dict(offset=0, limit=1), request_id="list")
        events = self.fixture.fixture.store.read(self.run)
        for change in ("actor", "elapsed", "digest", "reused", "outcome", "reference"):
            forged = copy.deepcopy(events)
            finish = next(event for event in forged if event["type"] == FINISH)
            if change == "actor":
                finish["actor_id"] = "worker"
            elif change == "elapsed":
                finish["payload"]["elapsed_ns"] = True
            elif change == "digest":
                finish["payload"]["result_digest"] = "sha256:" + "0" * 64
            elif change == "reused":
                finish["payload"]["reused"] = True
            elif change == "outcome":
                finish["payload"]["outcome"] = []
            else:
                finish["payload"]["reference"] = dict(kind="read", key="foreign")
            with self.assertRaises(ValueError):
                project(forged)
        with self.assertRaises(ValueError):
            project([event for event in events if event["type"] != START])
        with self.assertRaises(ValueError):
            apply(self.control.state(self.run), events[-1])

    def test_call_limit_survives_reconstruction_and_start_failure_cannot_execute(self):
        from camol.peer_tools import PeerTools
        for index in range(128):
            self.tools.call("list", dict(offset=0, limit=1), request_id="same-read")
        replacement = PeerTools(self.control, self.run, self.fixture.fixture.assignment, 1)
        with self.assertRaisesRegex(ValueError, "allowance"):
            replacement.call("list", dict(offset=0, limit=1), request_id="same-read")
        self.assertEqual(len(self.records()), 128)
        self.assertEqual(len(self.control.state(self.run)["peer_tool_reads"]), 1)
        other = PeerTools(self.control, self.run, self.fixture.fixture.assignments[1], 1)
        with patch("camol.peer_telemetry.emit", side_effect=OSError("cannot persist start")), patch.object(other, "_call") as execute:
            with self.assertRaises(OSError):
                other.call("list", dict(offset=0, limit=1), request_id="no-start")
            execute.assert_not_called()

    def test_typed_retry_and_invalid_correlation_are_logged_denials_not_effects(self):
        self.tools.call("list", dict(offset=0, limit=1), request_id="typed")
        with self.assertRaisesRegex(ValueError, "different content"):
            self.tools.call("list", dict(offset=False, limit=1), request_id="typed")
        self.assertEqual(list(self.records().values())[-1]["result"]["outcome"], "error")
        target = self.tools.call("observe", dict(box_id=self.fixture.fixture.box), request_id="observe")["result"]
        for correlation in ([], False, ""):
            with self.assertRaises(ValueError):
                self.tools.send(target, request_id="bad-correlation", body="note", correlation_id=correlation)
        self.assertFalse(self.control.state(self.run).get("box_messages"))
