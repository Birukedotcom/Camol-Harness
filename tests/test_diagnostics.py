from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from camol import Harness
from camol.diagnostics import event_metadata, profile_run
from camol.runbook import load_runbook
from tests import test_api as fixture


class DiagnosticTests(unittest.TestCase):
    setUp = fixture.HarnessApiTests.setUp
    tearDown = fixture.HarnessApiTests.tearDown

    def test_profile_has_no_prose_no_zero_denominator_and_surfaces_clock_regression(self):
        with Harness(self.workspace, self.state_dir) as harness:
            now = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
            harness.orchestrator.clock = lambda: now
            plan = load_runbook(fixture.ROOT / "examples/local-n-box-runbook.json")
            plan["run"]["objective"] = "private-prose-must-not-appear"
            initial = harness.prepare(plan)
            now -= timedelta(minutes=1)
            harness.approve(by="owner", digest=initial["plan_digest"])
            events = harness.events()
            report = profile_run(events)
            self.assertIsNone(report["productivity"]["accounted_tokens_per_succeeded_task"])
            self.assertIsNone(report["ledger_elapsed_ms"])
            self.assertTrue(report["clock_issues"])
            self.assertFalse(report["coverage"]["cpu_time_available"])
            self.assertNotIn("private-prose", json.dumps(report))
            metadata = [event_metadata(event) for event in events]
            self.assertNotIn("private-prose", json.dumps(metadata))
            self.assertNotIn("payload", metadata[0])
            self.assertEqual(metadata[-1]["seq"], events[-1]["seq"])
            self.assertEqual(profile_run(events), report)

    def test_actual_run_lifecycle_hotspots_reconcile_with_usage_and_do_not_claim_cpu(self):
        with Harness(self.workspace, self.state_dir) as harness:
            initial = harness.prepare(fixture.ROOT / "examples/local-n-box-runbook.json")
            harness.approve(by="owner", digest=initial["plan_digest"])
            state = harness.run()
            self.assertEqual(state["status"], "completed")
            report = profile_run(harness.events())
            self.assertEqual(report["productivity"]["succeeded_tasks"], len(state["tasks"]))
            self.assertGreater(report["productivity"]["accounted_tokens_per_succeeded_task"], 0)
            self.assertEqual(sum(item["accounted_tokens"] for item in report["task_hotspots"]), report["usage"]["totals"]["accounted_tokens"])
            self.assertTrue(report["lifecycle_spans"])
            self.assertFalse(any(item["right_censored"] for item in report["lifecycle_spans"]))
            self.assertFalse(report["ledger_elapsed_right_censored"])
            self.assertFalse(report["coverage"]["savings_or_causal_claim"])
