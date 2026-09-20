import copy
import unittest

from camol.usage import UsageError, UsageRecord, accounted_tokens, provider_cost_used, usage_report


def receipt(**overrides):
    values = dict(invocation_id="invoke-1", run_id="run-1", task_id="task-1", agent_id="box-1", lease_id="lease-1",
                  turn_number=1, provider="anthropic", model="model-1", phase="worker", provenance="provider_observed",
                  outcome="success", input_tokens=100, output_tokens=20, cache_read_tokens=30, cache_creation_tokens=10,
                  cost_usd_micros=30000, reserved_tokens=1000, reserved_cost_usd_micros=100000,
                  started_at="2026-09-07T10:00:00+00:00", finished_at="2026-09-07T10:00:02+00:00")
    values.update(overrides)
    return UsageRecord(**values)


def evidence(record):
    return dict(run_id=record.run_id, task_id=record.task_id, agent_id=record.agent_id, lease_id=record.lease_id,
                kind="model_usage", producer="adapter", epistemic_status="OBSERVED", data=record.to_dict())


def event(kind, payload):
    return dict(run_id="run-1", type=kind, payload=payload)


class UsageTests(unittest.TestCase):
    def legacy(self, *, invocation=None, evidence_id="legacy-1", **changes):
        item = evidence(receipt())
        item["data"] = dict(requested_model="model-1", resolved_model="model-1", all_reported_models=["model-1"],
                            input_tokens=100, output_tokens=20, cost_usd_micros=30000, provider_session_persisted=False)
        item["data"].update(changes)
        if invocation is not None:
            item["data"]["invocation_id"] = invocation
        item["evidence_id"] = evidence_id
        return item

    def command(self, invocation="invoke-1"):
        item = evidence(receipt())
        item.update(kind="command", epistemic_status="EXECUTED",
                    data=dict(invocation_id=invocation, started_at="2026-09-07T10:00:00+00:00", finished_at="2026-09-07T10:00:02+00:00"))
        return item

    def test_successful_turn_and_duplicate_receipts_are_not_double_counted(self):
        record = receipt()
        item = evidence(record)
        turn = dict(task_id="task-1", agent_id="box-1", lease_id="lease-1", input_tokens=100, output_tokens=20, usage_invocation_id="invoke-1")
        events = [event("EVIDENCE_RECORDED", item), event("AGENT_TURN_RECORDED", turn), event("EVIDENCE_RECORDED", item)]
        report = usage_report(events)
        self.assertEqual(report["totals"]["accounted_tokens"], 120)
        self.assertEqual(report["totals"]["invocations"], 1)
        state = dict(evidence={"a": item, "b": item}, total_tokens=120, accounted_usage_invocations=["invoke-1"])
        self.assertEqual(accounted_tokens(state), 120)
        self.assertEqual(provider_cost_used(state), 30000)

    def test_unknown_failure_charges_reservation_without_claiming_actual_spend(self):
        record = receipt(provenance="unknown", outcome="error", input_tokens=None, output_tokens=None,
                         cost_usd_micros=None, cache_read_tokens=None, cache_creation_tokens=None)
        state = dict(total_tokens=10, evidence={"a": evidence(record)})
        self.assertEqual(accounted_tokens(state), 1010)
        self.assertEqual(provider_cost_used(state), 100000)
        report = usage_report([event("EVIDENCE_RECORDED", evidence(record))])
        self.assertEqual(report["totals"]["observed_cost_usd_micros"], 0)
        self.assertEqual(report["totals"]["reserved_unknown_cost_usd_micros"], 100000)
        self.assertTrue(report["coverage"]["unknown_usage"])

    def test_conflicting_invocation_and_cross_subject_receipts_fail_closed(self):
        original = evidence(receipt())
        modified = evidence(receipt(cost_usd_micros=1))
        with self.assertRaisesRegex(UsageError, "conflicting"):
            usage_report([event("EVIDENCE_RECORDED", original), event("EVIDENCE_RECORDED", modified)])
        modified = copy.deepcopy(original)
        modified["task_id"] = "foreign-task"
        with self.assertRaisesRegex(UsageError, "match"):
            usage_report([event("EVIDENCE_RECORDED", modified)])

    def test_worker_claim_is_not_a_provider_bill(self):
        item = evidence(receipt())
        item.update(producer="worker", epistemic_status="UNVERIFIED")
        state = dict(evidence={"forged": item})
        self.assertEqual(provider_cost_used(state), 0)
        self.assertEqual(usage_report([event("EVIDENCE_RECORDED", item)])["totals"]["invocations"], 0)

    def test_process_estimates_are_reported_separately(self):
        turn = dict(task_id="task-1", agent_id="box-1", lease_id="lease-1", input_tokens=100, output_tokens=20)
        report = usage_report([event("AGENT_TURN_RECORDED", turn)])
        self.assertEqual(report["totals"]["worker_reported_tokens"], 120)
        self.assertEqual(report["totals"]["provider_observed_tokens"], 0)

    def test_tool_request_and_result_count_as_one_call(self):
        request = dict(kind="tool_call", epistemic_status="EXECUTED", data=dict(invocation_id="a", phase="request", tool_use_id="tool-1", tool="Bash"))
        result = dict(kind="tool_call", epistemic_status="EXECUTED", data=dict(invocation_id="a", phase="result", tool_use_id="tool-1"))
        report = usage_report([event("EVIDENCE_RECORDED", request), event("EVIDENCE_RECORDED", result), event("EVIDENCE_RECORDED", request)])
        self.assertEqual(report["tools"]["Bash"]["calls"], 1)
        self.assertIsNone(report["tools"]["Bash"]["duration_ms"])

    def test_record_rejects_invalid_numbers_times_and_unknown_fields(self):
        for changes in ({"input_tokens": True}, {"cost_usd_micros": -1}, {"reserved_tokens": float("nan")},
                        {"finished_at": "2026-09-07T09:00:00+00:00"}, {"cache_read_tokens": 200}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt(**changes)
        with self.assertRaises(UsageError):
            UsageRecord.from_dict(dict(receipt().to_dict(), extra=True))

    def test_debugger_execution_is_counted_once_and_is_not_model_spend(self):
        execution = dict(run_id="run-1", type="DEBUG_EXECUTION_FINISHED", actor_id="debug-executor",
                         payload={"case_id": "case-a", "execution_id": "execution-a", "receipt": {"results": [{"duration_ms": 15}, {"duration_ms": 30}]}})
        report = usage_report([execution, copy.deepcopy(execution)])
        self.assertEqual(report["debugger_duration_ms"], 45)
        self.assertEqual(report["debugger_commands"], 2)
        self.assertEqual(report["commands"], 2)
        self.assertEqual(report["totals"]["observed_cost_usd_micros"], 0)

    def test_legacy_observed_bill_matches_budget_and_turn_is_not_counted_twice(self):
        command, bill = self.command(), self.legacy()
        turn = dict(task_id="task-1", agent_id="box-1", lease_id="lease-1", input_tokens=100, output_tokens=20)
        events = [event("EVIDENCE_RECORDED", command), event("EVIDENCE_RECORDED", bill), event("AGENT_TURN_RECORDED", turn)]
        report = usage_report(events)
        self.assertEqual(report["totals"]["accounted_cost_usd_micros"], 30000)
        self.assertEqual(report["totals"]["accounted_cost_usd_micros"], provider_cost_used(dict(evidence={"command": command, "bill": bill})))
        self.assertEqual(report["totals"]["accounted_tokens"], 120)
        self.assertEqual(report["totals"]["worker_reported_tokens"], 0)
        self.assertEqual(report["totals"]["duration_ms"], 2000)
        self.assertEqual(report["coverage"]["legacy_provider_receipts"], 1)
        self.assertFalse(report["coverage"]["unknown_usage"])
        self.assertFalse(report["coverage"]["legacy_identity_incomplete"])
        self.assertEqual(report["by_task"]["task-1"]["observed_cost_usd_micros"], 30000)

    def test_legacy_missing_measurements_and_timing_remain_explicitly_unknown(self):
        item = self.legacy(input_tokens=None, output_tokens=None, cost_usd_micros=None)
        report = usage_report([event("EVIDENCE_RECORDED", item)])
        self.assertTrue(report["coverage"]["unknown_usage"])
        self.assertTrue(report["coverage"]["legacy_identity_incomplete"])
        self.assertFalse(report["coverage"]["duration_coverage_complete"])
        self.assertEqual(report["totals"]["unknown_token_invocations"], 1)
        self.assertEqual(report["totals"]["unknown_cost_invocations"], 1)
        # No historical reservation exists: never manufacture a frozen ceiling.
        self.assertEqual(report["totals"]["reserved_unknown_cost_usd_micros"], 0)
        self.assertTrue(report["by_task"]["task-1"]["unknown_cost_invocations"])

    def test_legacy_duplicate_command_identity_dedups_but_distinct_equal_calls_do_not(self):
        first, same, second = self.legacy(), self.legacy(evidence_id="legacy-replay"), self.legacy(evidence_id="legacy-2")
        events = [event("EVIDENCE_RECORDED", self.command()), event("EVIDENCE_RECORDED", first),
                  event("EVIDENCE_RECORDED", same), event("EVIDENCE_RECORDED", self.command("invoke-2")),
                  event("EVIDENCE_RECORDED", second)]
        report = usage_report(events)
        self.assertEqual(report["totals"]["invocations"], 2)
        self.assertEqual(report["totals"]["observed_cost_usd_micros"], 60000)
        state = dict(evidence={str(index): item["payload"] for index, item in enumerate(events)})
        self.assertEqual(provider_cost_used(state), 60000)
        # Replaying the same evidence ID after another command cannot silently
        # rebind its bill to the newly observed invocation.
        events.append(event("EVIDENCE_RECORDED", copy.deepcopy(first)))
        self.assertEqual(usage_report(events)["totals"]["observed_cost_usd_micros"], 60000)
        first["data"]["cost_usd_micros"] = 1
        with self.assertRaisesRegex(UsageError, "conflicting legacy"):
            usage_report(events)

    def test_mixed_legacy_modern_bill_is_one_charge_and_conflicting_forms_fail(self):
        old, modern = self.legacy(), evidence(receipt())
        turn = dict(task_id="task-1", agent_id="box-1", lease_id="lease-1", input_tokens=100, output_tokens=20, usage_invocation_id="invoke-1")
        events = [event("EVIDENCE_RECORDED", self.command()), event("EVIDENCE_RECORDED", old),
                  event("EVIDENCE_RECORDED", modern), event("AGENT_TURN_RECORDED", turn)]
        for with_modern_link in (True, False):
            if not with_modern_link:
                turn.pop("usage_invocation_id")
            report = usage_report(events)
            self.assertEqual(report["totals"]["invocations"], 1)
            self.assertEqual(report["totals"]["accounted_tokens"], 120)
            self.assertEqual(report["totals"]["accounted_cost_usd_micros"], 30000)
            self.assertEqual(provider_cost_used(dict(evidence={str(index): item["payload"] for index, item in enumerate(events[:-1])})), 30000)
        old["data"]["cost_usd_micros"] += 1
        with self.assertRaisesRegex(UsageError, "disagree"):
            usage_report(events)

    def test_ambiguous_mixed_bill_and_two_turns_per_old_invocation_fail_closed(self):
        with self.assertRaisesRegex(UsageError, "disambiguated"):
            usage_report([event("EVIDENCE_RECORDED", self.legacy()), event("EVIDENCE_RECORDED", evidence(receipt()))])
        bill = self.legacy(invocation="old-invoke")
        turn = dict(task_id="task-1", agent_id="box-1", lease_id="lease-1", input_tokens=100, output_tokens=20)
        events = [event("EVIDENCE_RECORDED", bill), event("AGENT_TURN_RECORDED", turn),
                  event("EVIDENCE_RECORDED", copy.deepcopy(bill)), event("AGENT_TURN_RECORDED", turn)]
        with self.assertRaisesRegex(UsageError, "two legacy"):
            usage_report(events)

    def test_unverified_legacy_claim_and_invalid_measurements_do_not_become_bills(self):
        bill = self.legacy()
        bill.update(epistemic_status="UNVERIFIED", producer="worker")
        self.assertEqual(usage_report([event("EVIDENCE_RECORDED", bill)])["totals"]["invocations"], 0)
        self.assertEqual(provider_cost_used(dict(evidence={"worker": bill})), 0)
        for amount in (-1, True, float("nan"), "30000"):
            bill = self.legacy(cost_usd_micros=amount)
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                usage_report([event("EVIDENCE_RECORDED", bill)])

    def test_diagnostic_profile_replays_legacy_bill_with_honest_cost_and_coverage(self):
        from camol.diagnostics import profile_run
        from tests.test_orchestrator import OrchestratorTests
        fixture = OrchestratorTests()
        fixture.setUp()
        try:
            fixture.admit_ready_tasks()
            assignment = fixture.orchestrator.lease_ready_tasks(fixture.run_id)[0]
            self.assertTrue(fixture.orchestrator.start_task(fixture.run_id, assignment))
            fixture.orchestrator.record_observed_evidence(fixture.run_id, assignment, kind="model_usage",
                data=dict(cost_usd_micros=125000), epistemic_status="OBSERVED", producer="adapter")
            profile = profile_run(fixture.store.read(fixture.run_id))
            state = fixture.orchestrator.state(fixture.run_id)
            self.assertEqual(profile["usage"]["totals"]["accounted_cost_usd_micros"], provider_cost_used(state))
            self.assertEqual(profile["usage"]["totals"]["accounted_cost_usd_micros"], 125000)
            hotspot = next(item for item in profile["task_hotspots"] if item["task_id"] == assignment["task_id"])
            self.assertEqual(hotspot["accounted_cost_usd_micros"], 125000)
            self.assertTrue(hotspot["unknown_usage"])
            self.assertTrue(profile["usage"]["coverage"]["unknown_usage"])
            self.assertFalse(profile["usage"]["coverage"]["duration_coverage_complete"])
        finally:
            fixture.tearDown()
