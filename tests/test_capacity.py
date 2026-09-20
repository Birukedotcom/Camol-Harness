import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from camol.capacity import CapacityBroker, CapacityError, validate_supply
from camol.capacity_runtime import request_for
from camol.orchestrator import Orchestrator
from camol.runbook import RunbookError, validate_runbook
from camol.state import empty_state, apply_event
from camol.events import new_event
from tests.test_gate_runtime import v5_plan


NOW = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)


def policy(**updates):
    value = dict(namespace="testing", allocation="saturate_connected", reservation_ttl_seconds=90,
                 allow_owner_declared_supply=False, rate_scope="harness_turn_estimate")
    value.update(updates)
    return value


def supply(pool_id, kind, *, now=NOW, slots=2, **updates):
    resources = dict(slots=slots, cpu_millis=4000, memory_bytes=8192, gpu_millis=1000, vram_bytes=8192, disk_bytes=8192) if kind == "target" else {"concurrency": slots}
    value = dict(schema="camol.capacity_supply", schema_version=1, namespace="testing", pool_id=pool_id, kind=kind,
                 limits=resources, outside_usage={name: 0 for name in resources}, capabilities=["code"],
                 attributes=dict(os="linux", architecture="arm64", locality="local"),
                 rate_limit=(dict(window_seconds=60, max_requests=1, max_tokens=100, scope="harness_turn_estimate") if kind == "provider" else None),
                 status="ready", provenance="observed", source="test-probe", observed_at=now.isoformat(), expires_at=(now + timedelta(seconds=300)).isoformat())
    value.update(updates)
    return validate_supply(value)


def request(run_id="run-a", task_id="task-a", *, provider=False, controller_id="controller-a", amount=100, **updates):
    needs = [dict(pool_id="target", kind="target", resources=dict(slots=1, cpu_millis=amount, memory_bytes=amount, gpu_millis=0, vram_bytes=0, disk_bytes=amount)),
             dict(pool_id="runtime", kind="runtime", resources={"concurrency": 1})]
    if provider:
        needs.append(dict(pool_id="provider", kind="provider", resources={"concurrency": 1}))
    value = dict(schema="camol.capacity_request", schema_version=1, namespace="testing", controller_id=controller_id, run_id=run_id,
                 plan_digest="sha256:" + "a" * 64, task_id=task_id, agent_id="agent-a", attempt=1, policy=policy(), needs=needs,
                 capabilities=["code"], placement={"architecture": "arm64"})
    value.update(updates)
    return value


def v6_plan(identifier="capacity-run"):
    plan = v5_plan(identifier)
    plan["schema_version"] = 6
    plan["run"]["capacity_policy"] = policy()
    plan["agents"][0]["capacity_pools"] = dict(target="target", runtime="runtime", provider=None)
    plan["tasks"][0]["resource_requirements"] = dict(cpu_millis=100, memory_bytes=100, gpu_millis=0, vram_bytes=0, disk_bytes=100, placement={})
    return validate_runbook(plan)


class CapacityBrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="camol-capacity-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "capacity.sqlite3"
        self.now = NOW
        self.broker = CapacityBroker(self.path, clock=lambda: self.now)
        self.addCleanup(self.broker.close)
        for kind in ("target", "runtime", "provider"):
            self.broker.publish(supply(kind, kind))

    def release(self, receipt):
        self.broker.release(receipt["reservation_id"], controller_id=receipt["request"]["controller_id"], run_id=receipt["request"]["run_id"], reconciliation_digest="sha256:" + "b" * 64)

    def test_concurrent_independent_brokers_never_oversubscribe_shared_pools(self):
        barrier = Barrier(8)
        def reserve(index):
            broker = CapacityBroker(self.path, clock=lambda: NOW)
            try:
                barrier.wait()
                return broker.reserve(request("same-run-id", controller_id="controller-" + str(index)), local_reservation_id="local-" + str(index))
            finally:
                broker.close()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(reserve, range(8)))
        self.assertEqual(sum(item["status"] == "granted" for item in results), 2)
        self.assertEqual(sum(item["status"] == "active" for item in self.broker.reservations()), 2)

    def test_expiry_is_suspect_not_free_and_wrong_controller_cannot_release(self):
        for index in range(2):
            self.broker.reserve(request("run-" + str(index)), local_reservation_id="local-" + str(index))
        self.now += timedelta(seconds=91)
        result = self.broker.reserve(request("late"), local_reservation_id="local-late")
        self.assertEqual(result["status"], "waiting")
        reservations = self.broker.reservations()
        self.assertEqual({item["status"] for item in reservations}, {"suspect"})
        receipt = reservations[0]["reservation"]
        with self.assertRaisesRegex(CapacityError, "another"):
            self.broker.release(receipt["reservation_id"], controller_id="other", run_id=receipt["request"]["run_id"], reconciliation_digest="sha256:" + "b" * 64)
        with self.assertRaisesRegex(CapacityError, "reconciliation"):
            self.broker.renew(receipt["reservation_id"], controller_id=receipt["request"]["controller_id"], run_id=receipt["request"]["run_id"])
        self.release(receipt)
        self.assertEqual(self.broker.reserve(request("late"), local_reservation_id="local-late")["status"], "granted")

    def test_round_robin_prevents_busy_run_from_jumping_queued_run(self):
        first = self.broker.reserve(request("run-a", "one"), local_reservation_id="local-1")["reservation"]
        second = self.broker.reserve(request("run-a", "two"), local_reservation_id="local-2")["reservation"]
        self.assertEqual(self.broker.reserve(request("run-b"), local_reservation_id="local-b")["status"], "waiting")
        self.release(first)
        busy = self.broker.reserve(request("run-a", "three"), local_reservation_id="local-3")
        self.assertIn("FAIR_QUEUE_WAIT", {item["reason"] for item in busy["reasons"]})
        self.assertEqual(self.broker.reserve(request("run-b"), local_reservation_id="local-b")["status"], "granted")
        self.release(second)
        self.assertEqual(self.broker.reserve(request("run-a", "three"), local_reservation_id="local-3")["status"], "granted")

    def test_crashed_fair_queue_owner_has_an_explicit_wake_deadline(self):
        first = self.broker.reserve(request("run-a", "one"), local_reservation_id="local-1")["reservation"]
        second = self.broker.reserve(request("run-a", "two"), local_reservation_id="local-2")["reservation"]
        self.broker.reserve(request("dead-run"), local_reservation_id="local-dead")
        self.release(first)
        waiting = self.broker.reserve(request("run-a", "three"), local_reservation_id="local-3")
        self.assertEqual(waiting["wake_at"], (NOW + timedelta(seconds=3600)).isoformat())
        self.now += timedelta(seconds=3600)
        # Supply refresh changes no queue ordering; deadline expiration does.
        for kind in ("target", "runtime", "provider"):
            self.broker.publish(supply(kind, kind, now=self.now))
        granted = self.broker.reserve(request("run-a", "three"), local_reservation_id="local-3")
        self.assertEqual(granted["status"], "granted", granted)
        self.release(second)

    def test_stale_unknown_unsuitable_and_owner_declared_supply_fail_closed(self):
        cases = [dict(status="unknown"), dict(provenance="owner_declared"), dict(capabilities=[]),
                 dict(attributes={"architecture": "x86_64"})]
        for index, changes in enumerate(cases):
            self.now += timedelta(seconds=1)
            self.broker.publish(supply("target", "target", now=self.now, **changes))
            result = self.broker.reserve(request("run-" + str(index)), local_reservation_id="local-" + str(index))
            self.assertEqual(result["status"], "waiting", changes)
        self.now += timedelta(seconds=400)
        result = self.broker.reserve(request("stale"), local_reservation_id="local-stale")
        self.assertIn("SUPPLY_STALE_OR_UNREADY", {item["reason"] for item in result["reasons"]})

    def test_outside_load_is_subtracted_and_nonfinite_supply_rejected(self):
        self.now += timedelta(seconds=1)
        self.broker.publish(supply("target", "target", now=self.now, outside_usage=dict(slots=2, cpu_millis=0, memory_bytes=0, gpu_millis=0, vram_bytes=0, disk_bytes=0)))
        self.assertEqual(self.broker.reserve(request(), local_reservation_id="local")["status"], "waiting")
        for amount in (True, -1, float("nan"), float("inf")):
            item = supply("target", "target")
            item["limits"]["memory_bytes"] = amount
            with self.subTest(amount=amount), self.assertRaises(CapacityError):
                validate_supply(item)

    def test_rate_debit_survives_release_and_reset_is_half_open(self):
        first = self.broker.reserve(request("first", provider=True), local_reservation_id="local-first")["reservation"]
        second = self.broker.reserve(request("second", provider=True), local_reservation_id="local-second")["reservation"]
        debit = self.broker.reserve_call(first["reservation_id"], call_id="call-first", max_requests=1, max_tokens=40, scope="harness_turn_estimate")
        self.assertEqual(debit["status"], "granted")
        self.assertEqual(self.broker.reserve_call(first["reservation_id"], call_id="call-first", max_requests=1, max_tokens=40, scope="harness_turn_estimate"), debit)
        self.release(first)
        result = self.broker.reserve_call(second["reservation_id"], call_id="call-second", max_requests=1, max_tokens=40, scope="harness_turn_estimate")
        self.assertEqual(result["status"], "waiting")
        self.assertIsNotNone(result["wake_at"])
        self.now += timedelta(seconds=60)
        self.assertEqual(self.broker.reserve_call(second["reservation_id"], call_id="call-second", max_requests=1, max_tokens=40, scope="harness_turn_estimate")["status"], "granted")
        with self.assertRaisesRegex(CapacityError, "different charge"):
            self.broker.reserve_call(second["reservation_id"], call_id="call-second", max_requests=1, max_tokens=41, scope="harness_turn_estimate")

    def test_read_only_inspection_never_creates_database_or_publishes(self):
        absent = self.path.parent / "absent" / "capacity.sqlite3"
        with self.assertRaises(CapacityError):
            CapacityBroker(absent, read_only=True)
        self.assertFalse(absent.parent.exists())
        before = self.path.stat().st_mtime_ns
        inspector = CapacityBroker(self.path, read_only=True, clock=lambda: self.now)
        try:
            self.assertEqual(len(inspector.inventory("testing")), 3)
            self.assertEqual(inspector.reservations(), [])
            with self.assertRaisesRegex(CapacityError, "read-only"):
                inspector.publish(supply("new", "runtime"))
        finally:
            inspector.close()
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_alternative_placement_does_not_queue_behind_itself_or_leave_a_ghost(self):
        self.now += timedelta(seconds=1)
        exhausted = supply("target", "target", now=self.now)
        exhausted["outside_usage"]["slots"] = exhausted["limits"]["slots"]
        self.broker.publish(exhausted)
        self.broker.publish(supply("other-target", "target", now=self.now))
        first = request()
        self.assertEqual(self.broker.reserve(first, local_reservation_id="local-a")["status"], "waiting")
        alternative = copy.deepcopy(first)
        alternative["agent_id"] = "agent-b"
        alternative["needs"][0]["pool_id"] = "other-target"
        granted = self.broker.reserve(alternative, local_reservation_id="local-b")
        self.assertEqual(granted["status"], "granted", granted)
        self.assertEqual(self.broker.connection.execute("SELECT COUNT(*) FROM capacity_queue WHERE status='waiting'").fetchone()[0], 0)
        # A second placement is not candidate replication; it remains the same
        # exclusive logical task/attempt even if a third pool has room.
        duplicate = self.broker.reserve(first, local_reservation_id="local-c")
        self.assertEqual(duplicate["reasons"], [{"reason": "TASK_ALREADY_RESERVED"}])


class CapacitySchemaTests(unittest.TestCase):
    def test_v6_requires_explicit_pool_namespace_and_resource_contract(self):
        plan = v6_plan()
        self.assertEqual(validate_runbook(plan), plan)
        for field in ("namespace", "rate_scope", "allow_owner_declared_supply"):
            broken = copy.deepcopy(plan)
            del broken["run"]["capacity_policy"][field]
            with self.subTest(field=field), self.assertRaises(RunbookError):
                validate_runbook(broken)
        old = v5_plan()
        old["run"]["capacity_policy"] = policy()
        with self.assertRaises(RunbookError):
            validate_runbook(old)
