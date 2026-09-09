import copy
import json
import subprocess
from pathlib import Path
import unittest

from camol.usage import UsageError, usage_report
from tests import test_usage as fixture
from tests import test_api as api_fixture
from camol import Harness
from camol.artifacts import RunArchive
from camol.state import project


class ActivityUsageTests(unittest.TestCase):
    def command(self, **changes):
        item = fixture.evidence(fixture.receipt())
        item.update(kind="command", epistemic_status="EXECUTED", data=dict(invocation_id="call-1",
            started_at="2026-09-07T10:00:00Z", finished_at="2026-09-07T10:00:02Z"))
        item.update(changes)
        return item

    def report(self, *items):
        return usage_report([fixture.event("EVIDENCE_RECORDED", item) for item in items])

    def test_same_local_invocation_id_in_two_boxes_counts_both(self):
        first = self.command()
        second = self.command(task_id="task-2", agent_id="box-2", lease_id="lease-2")
        report = self.report(first, second, copy.deepcopy(first))
        self.assertEqual(report["commands"], 2)
        self.assertEqual(report["command_duration_ms"], 4000)

    def test_conflicting_command_receipt_is_not_silently_first_wins(self):
        first = self.command()
        changed = copy.deepcopy(first)
        changed["data"]["finished_at"] = "2026-09-07T10:00:09Z"
        with self.assertRaises(UsageError):
            self.report(first, changed)

    def test_clock_regression_and_missing_times_are_unknown_not_zero_measurements(self):
        backwards = self.command()
        backwards["data"]["finished_at"] = "2026-09-07T09:59:59Z"
        missing = self.command()
        missing["data"] = dict(invocation_id="call-2")
        report = self.report(backwards, missing)
        self.assertEqual(report["commands"], 2)
        self.assertEqual(report["command_timing"]["known_commands"], 0)
        self.assertEqual(report["command_timing"]["unknown_commands"], 2)
        self.assertEqual(report["command_timing"]["clock_regressions"], 1)
        self.assertTrue(report["coverage"]["unknown_command_timing"])

    def test_tool_ids_are_scoped_and_missing_ids_never_collapse_unrelated_requests(self):
        first = self.command()
        first.update(kind="tool_call", data=dict(invocation_id="call-1", tool_use_id="tool-1", phase="request", tool="Bash"))
        second = copy.deepcopy(first)
        second.update(task_id="task-2", agent_id="box-2", lease_id="lease-2")
        report = self.report(first, second, copy.deepcopy(first))
        self.assertEqual(report["tools"]["Bash"]["calls"], 2)
        unknown = copy.deepcopy(first)
        unknown["data"] = dict(phase="request", tool="Read")
        report = self.report(unknown, copy.deepcopy(unknown))
        self.assertEqual(report["tools"]["Read"]["calls"], 2)
        self.assertEqual(report["activity_identity"]["unbound_tool_requests"], 2)

    def test_lease_and_producer_are_part_of_the_activity_identity(self):
        first = self.command()
        second = self.command(lease_id="lease-2")
        third = self.command(producer="verifier")
        report = self.report(first, second, third)
        self.assertEqual(report["commands"], 3)
        self.assertEqual(report["evaluator_duration_ms"], 2000)

    def test_missing_invocation_uses_evidence_identity_or_explicitly_unbound_rows(self):
        item = self.command(evidence_id="command-1")
        item["data"].pop("invocation_id")
        unbound = copy.deepcopy(item)
        unbound.pop("evidence_id")
        report = self.report(item, copy.deepcopy(item), unbound, copy.deepcopy(unbound))
        self.assertEqual(report["commands"], 3)
        self.assertEqual(report["activity_identity"]["unbound_commands"], 2)
        self.assertTrue(report["coverage"]["activity_identity_incomplete"])

    def test_malformed_timing_and_private_command_bodies_never_leak_into_report(self):
        item = self.command()
        marker = "private-command-body-must-not-appear"
        item["data"].update(started_at=marker, argv=["echo", marker], stderr=marker)
        report = self.report(item)
        self.assertEqual(report["command_timing"]["invalid_timestamps"], 1)
        self.assertEqual(report["command_timing"]["known_commands"], 0)
        self.assertNotIn(marker, json.dumps(report))
        for value in ("2026-09-07T10:00:00", "2026-09-07T10:00:00.1234567Z", {},
                      "0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
            with self.subTest(value=value):
                item["data"]["started_at"] = value
                self.assertEqual(self.report(item)["command_timing"]["invalid_timestamps"], 1)

    def test_conflicting_tool_requests_and_invalid_identifiers_fail_closed(self):
        item = self.command()
        item.update(kind="tool_call", data=dict(invocation_id="call-1", tool_use_id="tool-1", phase="request", tool="Bash"))
        changed = copy.deepcopy(item)
        changed["data"]["tool"] = "Read"
        with self.assertRaises(UsageError):
            self.report(item, changed)
        for changes in (dict(invocation_id=[]), dict(tool=False), dict(tool_use_id=True)):
            changed = copy.deepcopy(item)
            changed["data"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(changed)

    def test_activity_claims_never_become_bills_and_debugger_timing_stays_separate(self):
        item = self.command(producer="adapter")
        item["data"] = dict(invocation_id="call-1", input_tokens=9999, cost_usd_micros=9999)
        unverified = copy.deepcopy(item)
        unverified.update(producer="worker", epistemic_status="UNVERIFIED")
        debug = fixture.event("DEBUG_EXECUTION_FINISHED", dict(case_id="case", execution_id="exec",
            receipt={"results": [{"duration_ms": 15}, {"duration_ms": 30}]}))
        debug["actor_id"] = "debug-executor"
        report = usage_report([fixture.event("EVIDENCE_RECORDED", item), fixture.event("EVIDENCE_RECORDED", unverified), debug, copy.deepcopy(debug)])
        self.assertEqual(report["totals"]["accounted_tokens"], 0)
        self.assertEqual(report["totals"]["accounted_cost_usd_micros"], 0)
        self.assertEqual(report["commands"], 3)
        self.assertEqual(report["command_duration_ms"], 45)
        self.assertEqual(report["command_timing"]["known_commands"], 2)
        self.assertEqual(report["command_timing"]["unknown_commands"], 1)
        self.assertEqual(report["activity_provenance"]["command_producers"], {"adapter": 1})
        self.assertEqual(report["activity_provenance"]["debugger_receipt_commands"], 2)
        self.assertTrue(report["coverage"]["activity_is_reported_evidence_not_provider_billing"])


class ActivityRunTests(unittest.TestCase):
    setUp = api_fixture.HarnessApiTests.setUp
    tearDown = api_fixture.HarnessApiTests.tearDown

    def test_real_n_box_worker_claims_stay_unverified_and_report_replays(self):
        agent = self.workspace / "examples/fake_agent.py"
        original = agent.read_text()
        marker = '"purpose": command["purpose"],'
        self.assertEqual(original.count(marker), 1)
        agent.write_text(original.replace(marker, '"invocation_id": "worker-command-" + str(len(evidence)), ' + marker))
        for arguments in (("add", "examples/fake_agent.py"), ("commit", "-q", "-m", "scoped command fixture")):
            subprocess.run(["git", "-C", str(self.workspace), *arguments], check=True)
        with Harness(self.workspace, self.state_dir) as harness:
            initial = harness.prepare(api_fixture.ROOT / "examples/local-n-box-runbook.json")
            harness.approve(by="owner", digest=initial["plan_digest"])
            state = harness.run()
            self.assertEqual(state["status"], "completed", state.get("terminal"))
            events = harness.events()
            commands = [event["payload"] for event in events if event["type"] == "EVIDENCE_RECORDED"
                        and event["payload"]["kind"] == "command" and event["payload"]["producer"] == "worker"]
            self.assertGreaterEqual(len({item["agent_id"] for item in commands}), 2)
            self.assertLess(len({item["data"]["invocation_id"] for item in commands}), len(commands))
            report = usage_report(events)
            self.assertTrue(all(item["epistemic_status"] == "UNVERIFIED" for item in commands))
            self.assertNotIn("worker", report["activity_provenance"]["command_producers"])
            executed = [event["payload"] for event in events if event["type"] == "EVIDENCE_RECORDED"
                        and event["payload"]["kind"] == "command" and event["payload"]["epistemic_status"] == "EXECUTED"]
            self.assertEqual(report["commands"], len(executed))
            self.assertGreater(report["command_timing"]["known_commands"], 0)
            self.assertEqual(project(events), state)
            archive = Path(self.temp.name) / "activity-export"
            harness.export(archive)
            self.assertEqual(RunArchive.replay(archive), state)
