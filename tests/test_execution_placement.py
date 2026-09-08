import asyncio
import io
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from camol.admission import AdmissionBundle
from camol.doctor import DoctorOptions, run_doctor
from camol.execution_placement import ExecutionPlacementError, local_attributes, require_local_placement
from camol.schema import SchemaError
from camol.state import project
from tests import test_capacity_runtime as runtime_fixture
from tests import test_capacity as capacity_fixture
from tests import test_evaluation as evaluation_fixture


class ExecutionPlacementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.CapacityRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)

    def harness(self, placement):
        original = capacity_fixture.v6_plan
        def plan(identifier):
            value = original(identifier)
            value["tasks"][0]["resource_requirements"]["placement"] = dict(placement)
            return value
        with patch.object(capacity_fixture, "v6_plan", plan):
            runner, orchestrator, store, broker = self.fixture.harness(publish=False)
        now = datetime.now(timezone.utc)
        for kind in ("target", "runtime"):
            supply = capacity_fixture.supply(kind, kind, now=now)
            if kind == "target":
                supply["attributes"].update(placement)
            broker.publish(supply)
        return runner, orchestrator, store, broker

    def test_remote_capacity_label_cannot_authorize_local_execution(self):
        runner, orchestrator, store, broker = self.harness({"locality": "remote"})
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 0)
        self.assertEqual(final["total_tokens"], 0)
        self.assertFalse(any(event["type"] == "TASK_LEASED" for event in store.read("capacity-run")))
        self.assertFalse(broker.reservations())

    def test_matching_local_observations_refine_release_and_replay(self):
        placement = local_attributes("developer_trusted")
        placement.pop("region")
        runner, orchestrator, store, broker = self.harness(placement)
        before = evaluation_fixture.git(self.fixture.source, "status", "--porcelain")
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["status"], "awaiting_acceptance", final["tasks"])
        self.assertEqual(final["tasks"]["change"]["attempts"], 2)
        self.assertTrue(all(item["status"] == "released" for item in broker.reservations()))
        self.assertEqual(project(store.read("capacity-run")), final)
        self.assertEqual(evaluation_fixture.git(self.fixture.source, "status", "--porcelain"), before)
        bundle = AdmissionBundle.from_dict(next(iter(final["admissions"].values())))
        require_local_placement(final["tasks"]["change"], bundle)
        requirement = next(p for p in bundle.probe_policy.required_probes if p.probe_id == "execution.placement")
        result = next(p for p in bundle.receipt.probes if p.probe_id == "execution.placement")
        self.assertEqual(requirement.definition_digest, result.definition_digest)
        self.assertEqual(result.target_id, bundle.binding.target_id)
        self.assertIn("locality=local", result.summary)

    def test_unknown_region_cannot_be_proved_by_supply_labels(self):
        runner, _, store, broker = self.harness({"region": "owner-declared-region"})
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 0)
        self.assertFalse(broker.reservations())
        self.assertIn("region=unproven", json.dumps(store.read("capacity-run")))

    def test_legacy_admission_without_placement_probe_is_inspectable_but_cannot_launch(self):
        runner, _, store, broker = self.harness({"locality": "local"})
        runner._ensure_evaluator("capacity-run")
        runner.orchestrator.start("capacity-run")
        # Produce the pre-fix admission shape, keeping the actual frozen task.
        # The runtime must refuse it even though old readiness allows a lease.
        with patch("camol.admission.required_placement", return_value={}):
            # Persist the historical receipt explicitly: async preparation runs
            # in a child process and cannot inherit a parent-only mock.
            runner._ensure_admissions("capacity-run")
        admission = next(iter(runner.orchestrator.state("capacity-run")["admissions"].values()))
        self.assertNotIn("execution.placement", [
            probe.probe_id for probe in AdmissionBundle.from_dict(admission).probe_policy.required_probes
        ])
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 0)
        self.assertEqual(final["tasks"]["change"]["turn_count"], 0)
        self.assertEqual(final["total_tokens"], 0)
        events = store.read("capacity-run")
        self.assertTrue(any(event["type"] == "TASK_LEASED" for event in events))
        self.assertIn("fresh admission is required", json.dumps(events))
        self.assertEqual(project(events), final)
        self.assertTrue(broker.reservations())
        self.assertTrue(all(item["status"] == "released" for item in broker.reservations()))

    def test_changed_host_after_admission_pauses_before_worker_turn(self):
        runner, _, store, broker = self.harness({"locality": "local"})
        original = runner._before_launch
        async def changed_host(*args):
            actual = local_attributes("developer_trusted")
            actual["locality"] = "remote"
            with patch("camol.execution_placement.local_attributes", return_value=actual):
                await original(*args)
        with patch.object(runner, "_before_launch", changed_host):
            final = asyncio.run(runner.run_until_terminal("capacity-run"))
        self.assertEqual(final["total_tokens"], 0)
        self.assertEqual(final["tasks"]["change"]["attempts"], 1)
        self.assertEqual(final["tasks"]["change"]["turn_count"], 0)
        self.assertIn("POLICY_DENIED", json.dumps(store.read("capacity-run")))
        self.assertTrue(broker.reservations())
        self.assertTrue(all(item["status"] == "released" for item in broker.reservations()))

    def test_missing_or_misbound_proof_refuses_launch_without_changing_replay(self):
        runner, _, store, _ = self.harness({"locality": "local"})
        final = asyncio.run(runner.run_until_terminal("capacity-run"))
        bundle = AdmissionBundle.from_dict(next(iter(final["admissions"].values())))
        policy = bundle.probe_policy
        receipt = bundle.receipt
        variants = [
            replace(bundle, probe_policy=replace(policy, required_probes=tuple(p for p in policy.required_probes if p.probe_id != "execution.placement"))),
            replace(bundle, receipt=replace(receipt, probes=tuple(p for p in receipt.probes if p.probe_id != "execution.placement"))),
        ]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ExecutionPlacementError):
                require_local_placement(final["tasks"]["change"], variant)
        changed = dict(final["tasks"]["change"], resource_requirements={"placement": {"os": "other-os"}})
        with self.assertRaises(ExecutionPlacementError):
            require_local_placement(changed, bundle)
        # Receipt validation already rejects a probe bound to another target.
        with self.assertRaises(SchemaError):
            replace(receipt, probes=tuple(replace(p, target_id="other-target") if p.probe_id == "execution.placement" else p for p in receipt.probes))
        self.assertEqual(project(store.read("capacity-run")), final)

    def test_doctor_is_read_only_and_does_not_promote_unproven_placement(self):
        self.fixture.state.mkdir()
        plan_path = self.fixture.state / "doctor-runbook.json"
        original = capacity_fixture.v6_plan()
        source_before = evaluation_fixture.git(self.fixture.source, "status", "--porcelain")
        for placement, expected in (({"locality": "local"}, "green"),
                                    ({"locality": "remote"}, "red"),
                                    ({"region": "somewhere"}, "red"),
                                    ({"trust_tier": "developer_trusted"}, "red"),
                                    ({"os": "unavailable-os"}, "red"),
                                    ({"architecture": "unavailable-architecture"}, "red")):
            with self.subTest(placement=placement):
                original["tasks"][0]["resource_requirements"]["placement"] = placement
                plan_path.write_text(json.dumps(original))
                before = sorted(str(p) for p in self.fixture.state.rglob("*"))
                report = run_doctor(DoctorOptions(plan_path, self.fixture.source, self.fixture.state, json_output=True), stdout=io.StringIO())
                candidate = report.payload["tasks"][0]["candidates"][0]
                result = next(p for p in candidate["agent_probes"] if p["probe_id"] == "execution.placement")
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["required"])
                if expected == "red":
                    self.assertNotEqual(report.exit_code, 0)
                self.assertEqual(sorted(str(p) for p in self.fixture.state.rglob("*")), before)
        self.assertEqual(evaluation_fixture.git(self.fixture.source, "status", "--porcelain"), source_before)


if __name__ == "__main__":
    unittest.main()
