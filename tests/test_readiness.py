"""M0 readiness contracts and the characterization of the current unsafe boundary.

The ``UnsafeBoundaryCharacterization`` class documents what the scheduler does
TODAY: it leases an idle worker to any ready task whose static capability list
is a subset of the worker's, without any runtime, workspace, provider,
evaluator, or task-specific readiness evidence. These tests pass on purpose.

M3 replaces them: when the scheduler starts consuming ``ReadinessReceipt``,
``CapabilityGrant``, and ``CapacityReservation``, every ``test_CURRENT_UNSAFE_*``
assertion below must be inverted (the lease must NOT happen and a typed
``TASK_WAITING`` event must appear instead). Keeping them here, named loudly,
makes that flip a deliberate, reviewable change rather than a silent one.
"""

import json
import tempfile
import unittest
from pathlib import Path

from camol.events import LEGACY_EVENT_TYPES, new_event
from camol.orchestrator import Orchestrator
from camol.readiness import (
    NON_RUNNABLE_REASONS,
    CapabilityGrant,
    CapacityReservation,
    LeaseFence,
    ProbeResult,
    ReadinessReceipt,
    WaitingReason,
    WorkspaceReceipt,
    assess_ready_to_lease,
)
from camol.runbook import load_runbook
from camol.schema import SchemaError, canonical_digest
from camol.state import apply_event, empty_state, project
from camol.store import SQLiteEventStore


ROOT = Path(__file__).resolve().parents[1]
T0 = "2026-09-03T10:00:00+00:00"
T0_NORMALIZED = "2026-09-03T10:00:00.000000+00:00"
T_PLUS_5M = "2026-09-03T10:05:00+00:00"
T_PLUS_2M = "2026-09-03T10:02:00+00:00"
T_PLUS_10M = "2026-09-03T10:10:00+00:00"
PLAN = canonical_digest({"plan": "fixture"})
EVALUATOR = canonical_digest({"evaluator": "fixture"})
WORKSPACE = canonical_digest({"workspace": "fixture"})
CLEAN = canonical_digest([])


def probe(**overrides):
    values = dict(
        probe_id="git-version",
        kind="git",
        status="green",
        observed_at=T0,
        expires_at=T_PLUS_5M,
        command=["git", "--version"],
        tool_version="2.45.0",
        summary="git 2.45.0 is installed",
    )
    values.update(overrides)
    return ProbeResult(**values)


def receipt(**overrides):
    values = dict(
        receipt_id="rcpt-1",
        run_id="run-1",
        plan_digest=PLAN,
        task_id="frame",
        box_id="box-1",
        worker_id="strategist",
        target_id="localhost",
        transport_id="local-process",
        runtime_id="fake-agent",
        workspace_digest=WORKSPACE,
        evaluator_digest=EVALUATOR,
        adapter_kind="process",
        requested_model=None,
        credential_scopes=[],
        reservation_id="rsv-1",
        probes=[probe()],
        status="green",
        observed_at=T0,
        expires_at=T_PLUS_5M,
    )
    values.update(overrides)
    return ReadinessReceipt(**values)


def workspace(**overrides):
    values = dict(
        workspace_id="ws-1",
        repository_id="github.com/example/repo",
        base_revision="0123456789abcdef0123456789abcdef01234567",
        branch="camol/task-frame",
        path="state/worktrees/frame",
        dirty_digest=CLEAN,
        filesystem_policy="developer_trusted",
        cleanup_owner="camol",
        created_at=T0,
    )
    values.update(overrides)
    return WorkspaceReceipt(**values)


def reservation(**overrides):
    values = dict(
        reservation_id="rsv-1",
        run_id="run-1",
        task_id="frame",
        worker_id="strategist",
        target_id="localhost",
        concurrency_slots=1,
        max_tokens=4000,
        max_usd_cents=None,
        status="reserved",
        reserved_at=T0,
        expires_at=T_PLUS_5M,
    )
    values.update(overrides)
    return CapacityReservation(**values)


def grant(**overrides):
    values = dict(
        grant_id="grant-1",
        run_id="run-1",
        task_id="frame",
        box_id="box-1",
        capabilities=["write", "read", "execute"],
        filesystem_paths=["state/worktrees/frame"],
        trust_tier="developer_trusted",
        granted_by="test-owner",
        granted_at=T0,
        expires_at=T_PLUS_5M,
    )
    values.update(overrides)
    return CapabilityGrant(**values)


def fence(**overrides):
    values = dict(
        lease_id="lease-1",
        run_id="run-1",
        task_id="frame",
        box_id="box-1",
        worker_id="strategist",
        epoch=1,
        plan_digest=PLAN,
        evaluator_digest=EVALUATOR,
        workspace_digest=WORKSPACE,
        readiness_digest=receipt().digest(),
        grant_digest=grant().digest(),
        reservation_digest=reservation().digest(),
        issued_at=T0,
        expires_at=T_PLUS_5M,
    )
    values.update(overrides)
    return LeaseFence(**values)


class UnsafeBoundaryCharacterization(unittest.TestCase):
    """CURRENT BEHAVIOR, NOT DESIRED BEHAVIOR. See module docstring."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEventStore(Path(self.temporary.name) / "events.sqlite3")
        self.orchestrator = Orchestrator(self.store)
        state = self.orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
        self.run_id = state["run_id"]
        self.orchestrator.approve_plan(self.run_id, "test-owner", state["plan_digest"])
        self.orchestrator.start(self.run_id)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_CURRENT_UNSAFE_idle_static_capability_match_is_leased_with_no_readiness_evidence(self):
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["readiness_receipts"], {}, "precondition: no receipt exists")

        assignments = self.orchestrator.lease_ready_tasks(self.run_id)

        # Every ready task was leased purely on static capability subset matching.
        self.assertEqual({item["task_id"] for item in assignments}, {"frame", "inventory", "challenge"})
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(after["readiness_receipts"], {})
        for assignment in assignments:
            task = after["tasks"][assignment["task_id"]]
            agent = after["agents"][assignment["agent_id"]]
            self.assertEqual(task["status"], "leased")
            self.assertTrue(set(task["capabilities"]).issubset(agent["capabilities"]))

    def test_CURRENT_UNSAFE_lease_event_binds_no_readiness_workspace_grant_or_reservation_digest(self):
        self.orchestrator.lease_ready_tasks(self.run_id)
        lease_events = [event for event in self.store.read(self.run_id) if event["type"] == "TASK_LEASED"]
        self.assertTrue(lease_events)
        for event in lease_events:
            self.assertEqual(set(event["payload"]), {"task_id", "agent_id", "lease_id"})
            for bound in LeaseFence.BOUND_DIGESTS:
                self.assertNotIn(bound, event["payload"])
            self.assertNotIn("epoch", event["payload"])

    def test_CURRENT_UNSAFE_no_typed_wait_is_emitted_when_readiness_is_unproven(self):
        self.orchestrator.lease_ready_tasks(self.run_id)
        types = [event["type"] for event in self.store.read(self.run_id)]
        self.assertNotIn("TASK_WAITING", types)
        self.assertNotIn("READINESS_RECORDED", types)

    def test_CURRENT_UNSAFE_scheduler_ignores_v2_readiness_policy(self):
        # A v2 plan that says "require a readiness receipt" is still leased without one.
        raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8"))
        raw["schema_version"] = 2
        raw["run"]["id"] = "v2-policy-ignored"
        raw["run"]["readiness_policy"] = {"receipt_ttl_seconds": 300, "require_readiness_receipt": True}
        for agent in raw["agents"]:
            agent["trust_tier"] = "developer_trusted"
        state = self.orchestrator.initialize(raw)
        self.orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
        self.orchestrator.start(state["run_id"])

        assignments = self.orchestrator.lease_ready_tasks(state["run_id"])

        self.assertEqual(len(assignments), 3)
        self.assertEqual(self.orchestrator.state(state["run_id"])["readiness_receipts"], {})

    def test_desired_predicate_disagrees_with_the_current_scheduler(self):
        # The pure READY_TO_LEASE assessment says the same lease is NOT ready.
        state = self.orchestrator.state(self.run_id)
        decision = assess_ready_to_lease(
            now=T0,
            plan_digest=state["plan_digest"],
            plan_frozen=True,
            control_plane_ready=True,
            dependencies_green=True,
            receipt=None,
            evaluator_digest=None,
            evaluator_ready=False,
            grant=None,
            reservation=None,
            task_id="frame",
            box_id="strategist",
        )
        self.assertFalse(decision.ready)
        self.assertEqual(
            [reason.code for reason in decision.reasons],
            ["READINESS_STALE", "EVALUATOR_NOT_READY", "APPROVAL_REQUIRED", "CAPACITY_EXHAUSTED"],
        )
        self.assertEqual(len(self.orchestrator.lease_ready_tasks(self.run_id)), 3)


class WaitingReasonTests(unittest.TestCase):
    def test_all_twelve_reasons_are_frozen(self):
        self.assertEqual(
            NON_RUNNABLE_REASONS,
            frozenset(
                {
                    "WAITING_DEPENDENCY",
                    "AUTH_REQUIRED",
                    "NEEDS_DOWNLOAD",
                    "TARGET_UNREACHABLE",
                    "WORKSPACE_CONFLICT",
                    "CAPACITY_EXHAUSTED",
                    "EVALUATOR_NOT_READY",
                    "APPROVAL_REQUIRED",
                    "READINESS_STALE",
                    "POLICY_DENIED",
                    "EFFECT_UNKNOWN",
                    "OPERATOR_ATTENTION",
                }
            ),
        )
        for code in sorted(NON_RUNNABLE_REASONS):
            reason = WaitingReason(code=code, detail="d", wake_condition="w", task_id="t1")
            self.assertEqual(WaitingReason.from_dict(reason.to_dict()), reason)

    def test_unknown_code_and_unknown_field_are_rejected(self):
        with self.assertRaisesRegex(SchemaError, "code must be one of"):
            WaitingReason(code="SCHEDULER_DEADLOCK", detail="x")
        payload = WaitingReason(code="AUTH_REQUIRED", detail="x").to_dict()
        payload["severity"] = "high"
        with self.assertRaisesRegex(SchemaError, "unknown fields: severity"):
            WaitingReason.from_dict(payload)
        payload = WaitingReason(code="AUTH_REQUIRED", detail="x").to_dict()
        payload["schema_version"] = 2
        with self.assertRaisesRegex(SchemaError, "schema_version 2 is not supported"):
            WaitingReason.from_dict(payload)


class ContractRoundTripTests(unittest.TestCase):
    def assertRoundTrips(self, value):
        payload = value.to_dict()
        restored = type(value).from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored, value)
        self.assertEqual(restored.to_dict(), payload)
        self.assertEqual(restored.digest(), value.digest())
        self.assertEqual(payload["schema"], type(value).SCHEMA)
        self.assertEqual(payload["schema_version"], 1)
        return payload

    def test_every_contract_round_trips_and_normalizes_deterministically(self):
        for value in (probe(), receipt(), workspace(), reservation(), grant(), fence()):
            payload = self.assertRoundTrips(value)
            self.assertEqual(json.dumps(payload, sort_keys=True), json.dumps(payload, sort_keys=True))

    def test_normalization_sorts_lists_and_timestamps(self):
        value = grant(capabilities=["write", "credential", "read"], credential_refs=["b-ref", "a-ref"])
        self.assertEqual(value.capabilities, ["credential", "read", "write"])
        self.assertEqual(value.credential_refs, ["a-ref", "b-ref"])
        self.assertEqual(value.granted_at, T0_NORMALIZED)
        self.assertEqual(grant(granted_at="2026-09-03T12:00:00+02:00").digest(), grant().digest())
        r = receipt(probes=[probe(probe_id="z"), probe(probe_id="a")])
        self.assertEqual([item.probe_id for item in r.probes], ["a", "z"])

    def test_unknown_version_and_unknown_field_fail_clearly_for_every_contract(self):
        for value in (probe(), receipt(), workspace(), reservation(), grant(), fence()):
            cls = type(value)
            bumped = dict(value.to_dict(), schema_version=99)
            with self.assertRaisesRegex(SchemaError, "schema_version 99 is not supported"):
                cls.from_dict(bumped)
            extra = dict(value.to_dict(), surprise=True)
            with self.assertRaisesRegex(SchemaError, "unknown fields: surprise"):
                cls.from_dict(extra)
            wrong_schema = dict(value.to_dict(), schema="camol.other")
            with self.assertRaisesRegex(SchemaError, "schema must be"):
                cls.from_dict(wrong_schema)

    def test_missing_required_fields_fail_clearly(self):
        payload = fence().to_dict()
        del payload["readiness_digest"]
        with self.assertRaisesRegex(SchemaError, "missing required fields: readiness_digest"):
            LeaseFence.from_dict(payload)
        payload = probe().to_dict()
        del payload["summary"]
        with self.assertRaisesRegex(SchemaError, "missing required field summary"):
            ProbeResult.from_dict(payload)


class ValidationFailureTests(unittest.TestCase):
    def test_probe_validation(self):
        with self.assertRaisesRegex(SchemaError, "status must be one of"):
            probe(status="ok")
        with self.assertRaisesRegex(SchemaError, "expires_at must be after observed_at"):
            probe(expires_at=T0)
        with self.assertRaisesRegex(SchemaError, "green probe cannot list missing"):
            probe(missing_requirements=["docker"])
        with self.assertRaisesRegex(SchemaError, "argv array"):
            probe(command="git --version")
        with self.assertRaisesRegex(SchemaError, "evidence_digest must look like"):
            probe(evidence_digest="abc")
        red = probe(status="red", missing_requirements=["docker daemon"], expires_at=None)
        self.assertEqual(red.missing_requirements, ["docker daemon"])

    def test_receipt_validation(self):
        with self.assertRaisesRegex(SchemaError, "plan_digest must look like"):
            receipt(plan_digest="plan")
        with self.assertRaisesRegex(SchemaError, "non-empty list"):
            receipt(probes=[])
        with self.assertRaisesRegex(SchemaError, "probe ids must be unique"):
            receipt(probes=[probe(), probe()])
        with self.assertRaisesRegex(SchemaError, "green receipt cannot contain a non-green probe"):
            receipt(probes=[probe(status="red", expires_at=None)])
        with self.assertRaisesRegex(SchemaError, "expires_at must be after observed_at"):
            receipt(expires_at=T0)
        red = receipt(status="red", probes=[probe(status="unknown")])
        self.assertFalse(red.is_fresh(T0))

    def test_workspace_validation(self):
        with self.assertRaisesRegex(SchemaError, "filesystem_policy must be one of"):
            workspace(filesystem_policy="yolo")
        with self.assertRaisesRegex(SchemaError, "cleanup_owner must be one of"):
            workspace(cleanup_owner="user")
        with self.assertRaisesRegex(SchemaError, "dirty_digest must look like"):
            workspace(dirty_digest="dirty")

    def test_reservation_validation(self):
        with self.assertRaisesRegex(SchemaError, "concurrency_slots must be a positive integer"):
            reservation(concurrency_slots=0)
        with self.assertRaisesRegex(SchemaError, "max_usd_cents must be a non-negative integer"):
            reservation(max_usd_cents=-1)
        with self.assertRaisesRegex(SchemaError, "status must be one of"):
            reservation(status="pending")
        with self.assertRaisesRegex(SchemaError, "positive integer"):
            reservation(max_tokens=True)

    def test_grant_validation(self):
        with self.assertRaisesRegex(SchemaError, "unknown kinds: sudo"):
            grant(capabilities=["sudo"])
        with self.assertRaisesRegex(SchemaError, "credential_refs require the credential capability"):
            grant(credential_refs=["keychain:provider"])
        with self.assertRaisesRegex(SchemaError, "network_destinations require the network capability"):
            grant(network_destinations=["api.example.com:443"])
        with self.assertRaisesRegex(SchemaError, "trust_tier must be one of"):
            grant(trust_tier="root")
        with self.assertRaisesRegex(SchemaError, "must not contain duplicates"):
            grant(capabilities=["read", "read"])

    def test_fence_validation(self):
        with self.assertRaisesRegex(SchemaError, "epoch must be a positive integer"):
            fence(epoch=0)
        with self.assertRaisesRegex(SchemaError, "grant_digest must look like"):
            fence(grant_digest="nope")
        with self.assertRaisesRegex(SchemaError, "expires_at must be after issued_at"):
            fence(expires_at=T0)


class ExpiryAndBindingTests(unittest.TestCase):
    def test_receipt_expiry_is_explicit_and_pure(self):
        r = receipt()
        self.assertEqual(r.expires_at, "2026-09-03T10:05:00.000000+00:00")
        self.assertTrue(r.is_fresh(T0))
        self.assertTrue(r.is_fresh("2026-09-03T10:04:59.999999+00:00"))
        self.assertFalse(r.is_fresh(T_PLUS_5M), "expiry instant is not fresh")
        self.assertFalse(r.is_fresh(T_PLUS_10M))
        self.assertFalse(receipt(status="red").is_fresh(T0), "red is never fresh")

    def test_probe_without_expiry_is_never_fresh(self):
        self.assertFalse(probe(expires_at=None).is_fresh(T0))

    def test_reservation_and_grant_and_fence_expiry(self):
        self.assertTrue(reservation().is_active(T0))
        self.assertFalse(reservation().is_active(T_PLUS_5M))
        self.assertFalse(reservation(status="released").is_active(T0))
        self.assertTrue(grant().is_fresh(T_PLUS_2M))
        self.assertFalse(grant().is_fresh(T_PLUS_10M))
        self.assertTrue(fence().is_fresh(T_PLUS_2M))
        self.assertFalse(fence().is_fresh(T_PLUS_5M))

    def test_receipt_binding_fields_and_digest(self):
        r = receipt()
        self.assertEqual(
            ReadinessReceipt.BINDING_FIELDS,
            (
                "run_id",
                "plan_digest",
                "task_id",
                "box_id",
                "worker_id",
                "target_id",
                "transport_id",
                "runtime_id",
                "workspace_digest",
                "evaluator_digest",
                "adapter_kind",
                "requested_model",
                "reservation_id",
            ),
        )
        binding = r.binding()
        self.assertEqual(set(binding), set(ReadinessReceipt.BINDING_FIELDS))
        self.assertEqual(r.binding_digest(), canonical_digest(binding))
        # Changing any bound field changes the binding digest ...
        for name in ReadinessReceipt.BINDING_FIELDS:
            changed = {"requested_model": "other-model"} if name == "requested_model" else None
            if changed is None:
                current = getattr(r, name)
                changed = {name: canonical_digest(["other"]) if current.startswith("sha256:") else current + "-x"}
            self.assertNotEqual(receipt(**changed).binding_digest(), r.binding_digest(), name)
        # ... while a new observation time alone does not.
        self.assertEqual(receipt(observed_at="2026-09-03T10:01:00+00:00").binding_digest(), r.binding_digest())

    def test_fence_binds_every_input_digest_and_supersedes_by_epoch(self):
        f = fence()
        self.assertEqual(
            set(f.bound_digests()),
            {"plan_digest", "evaluator_digest", "workspace_digest", "readiness_digest", "grant_digest", "reservation_digest"},
        )
        self.assertEqual(f.readiness_digest, receipt().digest())
        self.assertEqual(f.grant_digest, grant().digest())
        self.assertEqual(f.reservation_digest, reservation().digest())
        # Re-probing produces a new receipt digest and therefore a mismatched fence.
        self.assertNotEqual(receipt(observed_at="2026-09-03T10:01:00+00:00").digest(), f.readiness_digest)
        newer = fence(lease_id="lease-2", epoch=2)
        self.assertTrue(newer.supersedes(f))
        self.assertFalse(f.supersedes(newer))
        self.assertFalse(fence(task_id="inventory", epoch=2).supersedes(f))

    def test_credential_scopes_are_fingerprints_not_secrets(self):
        r = receipt(credential_scopes=["provider:scope-fingerprint-1"])
        self.assertNotIn("token", json.dumps(r.to_dict()).lower())
        self.assertEqual(r.credential_scopes, ["provider:scope-fingerprint-1"])


class ReadyToLeasePredicateTests(unittest.TestCase):
    def all_green(self, **overrides):
        values = dict(
            now=T_PLUS_2M,
            plan_digest=PLAN,
            plan_frozen=True,
            control_plane_ready=True,
            dependencies_green=True,
            receipt=receipt(),
            evaluator_digest=EVALUATOR,
            evaluator_ready=True,
            grant=grant(),
            reservation=reservation(),
            task_id="frame",
            box_id="box-1",
        )
        values.update(overrides)
        return assess_ready_to_lease(**values)

    def test_all_conjuncts_green_is_ready(self):
        decision = self.all_green()
        self.assertTrue(decision.ready)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.to_dict(), {"ready": True, "reasons": []})

    def test_each_failed_conjunct_yields_its_typed_reason(self):
        cases = [
            ({"plan_frozen": False}, "APPROVAL_REQUIRED"),
            ({"control_plane_ready": False}, "OPERATOR_ATTENTION"),
            ({"dependencies_green": False}, "WAITING_DEPENDENCY"),
            ({"receipt": None}, "READINESS_STALE"),
            ({"now": T_PLUS_10M}, "READINESS_STALE"),
            ({"receipt": receipt(status="red", probes=[probe(status="red", expires_at=None)])}, "READINESS_STALE"),
            ({"receipt": receipt(plan_digest=canonical_digest("other plan"))}, "READINESS_STALE"),
            ({"receipt": receipt(task_id="inventory")}, "POLICY_DENIED"),
            ({"evaluator_ready": False}, "EVALUATOR_NOT_READY"),
            ({"evaluator_digest": canonical_digest("other evaluator")}, "EVALUATOR_NOT_READY"),
            ({"grant": None}, "APPROVAL_REQUIRED"),
            ({"grant": grant(box_id="box-2")}, "POLICY_DENIED"),
            ({"reservation": None}, "CAPACITY_EXHAUSTED"),
            ({"reservation": reservation(status="released")}, "CAPACITY_EXHAUSTED"),
            ({"reservation": reservation(reservation_id="rsv-2")}, "READINESS_STALE"),
        ]
        for overrides, expected in cases:
            decision = self.all_green(**overrides)
            self.assertFalse(decision.ready, overrides)
            self.assertIn(expected, [reason.code for reason in decision.reasons], overrides)
            for reason in decision.reasons:
                self.assertEqual(reason.task_id, "frame")
                self.assertEqual(reason.box_id, "box-1")
                self.assertTrue(reason.wake_condition)

    def test_reasons_are_serializable_contracts(self):
        decision = self.all_green(receipt=None, grant=None)
        payload = decision.to_dict()
        restored = [WaitingReason.from_dict(item) for item in payload["reasons"]]
        self.assertEqual(tuple(restored), decision.reasons)


class EventAndProjectionTests(unittest.TestCase):
    def _run_created(self):
        runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        return new_event(
            "three-agent-demo",
            "RUN_CREATED",
            "orchestrator",
            {"runbook": runbook, "plan_digest": canonical_digest(runbook)},
        )

    def test_legacy_event_stream_replays_to_the_same_projection_plus_empty_receipts(self):
        created = self._run_created()
        legacy_state = project([created])
        self.assertEqual(legacy_state["readiness_receipts"], {})
        stripped = {key: value for key, value in legacy_state.items() if key != "readiness_receipts"}
        expected_keys = set(empty_state()) - {"readiness_receipts"}
        self.assertEqual(set(stripped), expected_keys)
        self.assertTrue(LEGACY_EVENT_TYPES)
        self.assertNotIn("READINESS_RECORDED", LEGACY_EVENT_TYPES)
        self.assertNotIn("TASK_WAITING", LEGACY_EVENT_TYPES)
        for task in legacy_state["tasks"].values():
            self.assertNotIn("waiting", task)

    def test_readiness_recorded_projects_a_validated_receipt(self):
        created = self._run_created()
        state = project([created])
        recorded = new_event("three-agent-demo", "READINESS_RECORDED", "doctor", {"receipt": receipt().to_dict()})
        state = apply_event(state, recorded)
        self.assertEqual(state["readiness_receipts"]["rcpt-1"], receipt().to_dict())
        corrupt = dict(receipt().to_dict(), schema_version=3)
        with self.assertRaisesRegex(SchemaError, "schema_version 3 is not supported"):
            apply_event(state, new_event("three-agent-demo", "READINESS_RECORDED", "doctor", {"receipt": corrupt}))

    def test_task_waiting_and_cleared_round_trip_through_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteEventStore(Path(temporary) / "events.sqlite3")
            try:
                run_id = "three-agent-demo"
                store.append(self._run_created())
                reason = WaitingReason(
                    code="EVALUATOR_NOT_READY",
                    detail="frozen evaluator bundle is not launchable",
                    wake_condition="evaluator probe green",
                    task_id="frame",
                )
                store.append(new_event(run_id, "TASK_WAITING", "orchestrator", {"task_id": "frame", "reason": reason.to_dict()}))
                waiting = project(store.read(run_id))
                self.assertEqual(waiting["tasks"]["frame"]["status"], "waiting")
                self.assertEqual(WaitingReason.from_dict(waiting["tasks"]["frame"]["waiting"]), reason)
                store.append(new_event(run_id, "TASK_WAIT_CLEARED", "orchestrator", {"task_id": "frame"}))
                cleared = project(store.read(run_id))
                self.assertEqual(cleared["tasks"]["frame"]["status"], "pending")
                self.assertIsNone(cleared["tasks"]["frame"]["waiting"])
            finally:
                store.close()

    def test_waiting_transitions_are_guarded(self):
        state = project([self._run_created()])
        with self.assertRaisesRegex(ValueError, "not waiting"):
            apply_event(state, new_event("three-agent-demo", "TASK_WAIT_CLEARED", "o", {"task_id": "frame"}))
        bad_reason = {"schema": "camol.waiting_reason", "schema_version": 1, "code": "NOPE", "detail": "x"}
        with self.assertRaisesRegex(SchemaError, "code must be one of"):
            apply_event(state, new_event("three-agent-demo", "TASK_WAITING", "o", {"task_id": "frame", "reason": bad_reason}))

    def test_waiting_task_is_not_offered_to_the_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteEventStore(Path(temporary) / "events.sqlite3")
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
                run_id = state["run_id"]
                orchestrator.approve_plan(run_id, "test-owner", state["plan_digest"])
                orchestrator.start(run_id)
                reason = WaitingReason(code="AUTH_REQUIRED", detail="provider login required", task_id="frame")
                store.append(new_event(run_id, "TASK_WAITING", "orchestrator", {"task_id": "frame", "reason": reason.to_dict()}))
                assignments = orchestrator.lease_ready_tasks(run_id)
                self.assertEqual({item["task_id"] for item in assignments}, {"inventory", "challenge"})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
