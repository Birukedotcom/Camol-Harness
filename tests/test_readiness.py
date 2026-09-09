"""Readiness contracts, the READY_TO_LEASE predicate, and launch boundary."""

import json
import tempfile
import unittest
from pathlib import Path

from camol.events import LEGACY_EVENT_TYPES, new_event
from camol.orchestrator import Orchestrator
from camol.readiness import (
    FILESYSTEM_POLICIES,
    NON_RUNNABLE_REASONS,
    SUBJECT_FIELDS,
    TRUST_TIERS,
    AuthorityPolicy,
    BoxBinding,
    CapabilityGrant,
    CapacityReservation,
    LeaseFence,
    ProbePolicy,
    ProbeRequirement,
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
T_MINUS_1M = "2026-09-03T09:59:00+00:00"
T0 = "2026-09-03T10:00:00+00:00"
T0_NORMALIZED = "2026-09-03T10:00:00.000000+00:00"
T_PLUS_1M = "2026-09-03T10:01:00+00:00"
T_PLUS_2M = "2026-09-03T10:02:00+00:00"
T_PLUS_3M = "2026-09-03T10:03:00+00:00"
T_PLUS_5M = "2026-09-03T10:05:00+00:00"
T_PLUS_10M = "2026-09-03T10:10:00+00:00"
PLAN = canonical_digest({"plan": "fixture"})
EVALUATOR = canonical_digest({"evaluator": "fixture"})
CLEAN = canonical_digest([])
OTHER = canonical_digest(["other"])
GIT_PROBE_DEFINITION = canonical_digest({"implementation": "tests.GitVersionProbe", "version": 1})


# --------------------------------------------------------------------------- fixtures


def probe(**overrides):
    values = dict(
        probe_id="git-version",
        kind="git",
        status="green",
        observed_at=T0,
        expires_at=T_PLUS_5M,
        method="process",
        command=["git", "--version"],
        tool_version="2.45.0",
        summary="git 2.45.0 is installed",
        target_id="localhost",
        definition_digest=GIT_PROBE_DEFINITION,
    )
    values.update(overrides)
    if values["status"] != "green":
        values.setdefault("reason_code", "NEEDS_DOWNLOAD")
        values.setdefault("wake_condition", "install the tool")
        values.setdefault("missing_requirements", ["git"])
    return ProbeResult(**values)


def red_probe(**overrides):
    values = dict(status="red", expires_at=None)
    values.update(overrides)
    return probe(**values)


def workspace(**overrides):
    values = dict(
        workspace_id="ws-1",
        repository_id="github.com/example/repo",
        base_revision="0123456789abcdef0123456789abcdef01234567",
        branch="camol/task-frame",
        path="state/worktrees/frame",
        dirty_digest=CLEAN,
        filesystem_policy="isolated_worktree_write",
        cleanup_owner="camol",
        created_at=T0,
    )
    values.update(overrides)
    return WorkspaceReceipt(**values)


def binding(**overrides):
    values = dict(
        run_id="run-1",
        task_id="frame",
        box_id="box-1",
        worker_id="strategist",
        target_id="localhost",
        plan_digest=PLAN,
        workspace_id="ws-1",
        workspace_digest=workspace().digest(),
        bound_at=T0,
    )
    values.update(overrides)
    return BoxBinding(**values)


def authority(**overrides):
    values = dict(
        run_id="run-1",
        task_id="frame",
        required_capabilities=["write", "read", "execute"],
        filesystem_paths=["state/worktrees/frame"],
        trust_tier="developer_trusted",
    )
    values.update(overrides)
    return AuthorityPolicy(**values)


def probe_policy(**overrides):
    values = dict(
        run_id="run-1",
        task_id="frame",
        required_probes=[ProbeRequirement(probe_id="git-version", kind="git", target_bound=True, definition_digest=GIT_PROBE_DEFINITION)],
    )
    values.update(overrides)
    return ProbePolicy(**values)


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
        box_binding_digest=binding().digest(),
        workspace_digest=workspace().digest(),
        evaluator_digest=EVALUATOR,
        authority_digest=authority().digest(),
        probe_policy_digest=probe_policy().digest(),
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


def reservation(**overrides):
    values = dict(
        reservation_id="rsv-1",
        run_id="run-1",
        task_id="frame",
        box_id="box-1",
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
        worker_id="strategist",
        target_id="localhost",
        authority_digest=authority().digest(),
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
        target_id="localhost",
        epoch=1,
        plan_digest=PLAN,
        box_binding_digest=binding().digest(),
        evaluator_digest=EVALUATOR,
        workspace_digest=workspace().digest(),
        authority_digest=authority().digest(),
        probe_policy_digest=probe_policy().digest(),
        readiness_digest=receipt().digest(),
        grant_digest=grant().digest(),
        reservation_digest=reservation().digest(),
        issued_at=T0,
        expires_at=T_PLUS_5M,
    )
    values.update(overrides)
    return LeaseFence(**values)


def assess(**overrides):
    """All-green READY_TO_LEASE inputs for subject (run-1, frame, box-1, strategist, localhost)."""
    values = dict(
        now=T_PLUS_2M,
        binding=binding(),
        plan_digest=PLAN,
        plan_frozen=True,
        control_plane_ready=True,
        dependencies_green=True,
        workspace=workspace(),
        receipt=receipt(),
        evaluator_digest=EVALUATOR,
        evaluator_ready=True,
        authority_policy=authority(),
        grant=grant(),
        probe_policy=probe_policy(),
        reservation=reservation(),
    )
    values.update(overrides)
    return assess_ready_to_lease(**values)


ALL_CONTRACTS = (probe, workspace, binding, authority, probe_policy, receipt, reservation, grant, fence)


# --------------------------------------------------------------------------- enforced launch boundary


class ReadinessLaunchBoundaryTests(unittest.TestCase):
    """No persisted, green admission proof means no lease and no process launch."""

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

    def test_idle_static_capability_match_is_not_leased_without_admission_evidence(self):
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["readiness_receipts"], {}, "precondition: no receipt exists")
        assignments = self.orchestrator.lease_ready_tasks(self.run_id)
        self.assertEqual(assignments, [])
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(after["readiness_receipts"], {})
        self.assertTrue(all(agent["status"] == "idle" for agent in after["agents"].values()))
        self.assertEqual(
            {task["waiting"]["code"] for task in after["tasks"].values()},
            {"READINESS_STALE", "WAITING_DEPENDENCY"},
        )

    def test_no_lease_event_exists_without_bound_admission_digests(self):
        self.orchestrator.lease_ready_tasks(self.run_id)
        lease_events = [event for event in self.store.read(self.run_id) if event["type"] == "TASK_LEASED"]
        self.assertEqual(lease_events, [])

    def test_typed_wait_is_emitted_when_readiness_is_unproven(self):
        self.orchestrator.lease_ready_tasks(self.run_id)
        events = self.store.read(self.run_id)
        types = [event["type"] for event in events]
        self.assertIn("TASK_WAITING", types)
        self.assertNotIn("READINESS_RECORDED", types)
        reasons = [event["payload"]["reason"]["code"] for event in events if event["type"] == "TASK_WAITING"]
        self.assertIn("READINESS_STALE", reasons)

    def test_v2_plan_is_not_leased_without_any_receipt(self):
        raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text(encoding="utf-8"))
        raw["schema_version"] = 2
        raw["run"]["id"] = "v2-policy-not-enforced"
        raw["run"]["readiness_policy"] = {"receipt_ttl_seconds": 300}
        for agent in raw["agents"]:
            agent["trust_tier"] = "developer_trusted"
        state = self.orchestrator.initialize(raw)
        self.orchestrator.approve_plan(state["run_id"], "test-owner", state["plan_digest"])
        self.orchestrator.start(state["run_id"])
        assignments = self.orchestrator.lease_ready_tasks(state["run_id"])
        self.assertEqual(assignments, [])
        self.assertEqual(self.orchestrator.state(state["run_id"])["readiness_receipts"], {})

    def test_predicate_and_scheduler_both_deny_missing_proofs(self):
        state = self.orchestrator.state(self.run_id)
        decision = assess_ready_to_lease(
            now=T0,
            binding=binding(run_id=self.run_id, box_id="strategist", plan_digest=state["plan_digest"]),
            plan_digest=state["plan_digest"],
            plan_frozen=True,
            control_plane_ready=True,
            dependencies_green=True,
            workspace=None,
            receipt=None,
            evaluator_digest=None,
            evaluator_ready=False,
            authority_policy=None,
            grant=None,
            probe_policy=None,
            reservation=None,
        )
        self.assertFalse(decision.ready)
        self.assertEqual(
            [reason.code for reason in decision.reasons],
            [
                "WORKSPACE_CONFLICT",
                "READINESS_STALE",
                "READINESS_STALE",
                "EVALUATOR_NOT_READY",
                "APPROVAL_REQUIRED",
                "APPROVAL_REQUIRED",
                "CAPACITY_EXHAUSTED",
            ],
        )
        self.assertEqual(self.orchestrator.lease_ready_tasks(self.run_id), [])


# --------------------------------------------------------------------------- vocabulary


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


# --------------------------------------------------------------------------- round trips


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
        for factory in ALL_CONTRACTS:
            payload = self.assertRoundTrips(factory())
            for item in payload.values():
                self.assertNotIsInstance(item, tuple)

    def test_normalization_sorts_lists_and_timestamps(self):
        value = grant(capabilities=["write", "credential", "read"], credential_refs=["b-ref", "a-ref"])
        self.assertEqual(value.capabilities, ("credential", "read", "write"))
        self.assertEqual(value.credential_refs, ("a-ref", "b-ref"))
        self.assertEqual(value.granted_at, T0_NORMALIZED)
        self.assertEqual(grant(granted_at="2026-09-03T12:00:00+02:00").digest(), grant().digest())
        r = receipt(probes=[probe(probe_id="z"), probe(probe_id="a")])
        self.assertEqual([item.probe_id for item in r.probes], ["a", "z"])
        policy = probe_policy(required_probes=[ProbeRequirement("z", "git", False, OTHER), ProbeRequirement("a", "git", True, OTHER)])
        self.assertEqual([item.probe_id for item in policy.required_probes], ["a", "z"])

    def test_unknown_version_and_unknown_field_fail_clearly_for_every_contract(self):
        for factory in ALL_CONTRACTS:
            value = factory()
            cls = type(value)
            with self.assertRaisesRegex(SchemaError, "schema_version 99 is not supported"):
                cls.from_dict(dict(value.to_dict(), schema_version=99))
            with self.assertRaisesRegex(SchemaError, "unknown fields: surprise"):
                cls.from_dict(dict(value.to_dict(), surprise=True))
            with self.assertRaisesRegex(SchemaError, "schema must be"):
                cls.from_dict(dict(value.to_dict(), schema="camol.other"))

    def test_missing_required_fields_fail_clearly(self):
        payload = fence().to_dict()
        del payload["readiness_digest"]
        with self.assertRaisesRegex(SchemaError, "missing required fields: readiness_digest"):
            LeaseFence.from_dict(payload)
        payload = probe().to_dict()
        del payload["summary"]
        with self.assertRaisesRegex(SchemaError, "missing required field summary"):
            ProbeResult.from_dict(payload)
        payload = grant().to_dict()
        del payload["authority_digest"]
        with self.assertRaisesRegex(SchemaError, "missing required fields: authority_digest"):
            CapabilityGrant.from_dict(payload)
        payload = receipt().to_dict()
        del payload["box_binding_digest"]
        with self.assertRaisesRegex(SchemaError, "missing required fields: box_binding_digest"):
            ReadinessReceipt.from_dict(payload)


class DeepImmutabilityTests(unittest.TestCase):
    def assertImmutableCollection(self, contract, attribute):
        before = contract.digest()
        collection = getattr(contract, attribute)
        self.assertIsInstance(collection, tuple, attribute)
        with self.assertRaises(AttributeError, msg=attribute):
            collection.append("mutation")
        with self.assertRaises(TypeError, msg=attribute):
            collection[0] = "mutation"
        with self.assertRaises(AttributeError, msg=attribute):
            setattr(contract, attribute, ())
        self.assertEqual(contract.digest(), before, attribute)

    def test_receipt_probes_cannot_be_appended(self):
        r = receipt()
        before = r.digest()
        with self.assertRaises(AttributeError):
            r.probes.append(probe(probe_id="late"))
        self.assertEqual(r.digest(), before)
        self.assertImmutableCollection(r, "probes")
        self.assertImmutableCollection(r, "credential_scopes")

    def test_grant_policy_and_probe_collections_are_immutable(self):
        g = grant(capabilities=["network", "credential", "read"], network_destinations=["a:443"], credential_refs=["ref-1"])
        for attribute in ("capabilities", "filesystem_paths", "network_destinations", "credential_refs"):
            self.assertImmutableCollection(g, attribute)
        a = authority(required_capabilities=["network", "read"], network_destinations=["a:443"])
        for attribute in ("required_capabilities", "filesystem_paths", "network_destinations", "credential_refs"):
            self.assertImmutableCollection(a, attribute)
        self.assertImmutableCollection(probe_policy(), "required_probes")
        p = red_probe(missing_requirements=["docker"])
        self.assertImmutableCollection(p, "missing_requirements")
        self.assertImmutableCollection(p, "command")

    def test_caller_lists_are_copied_not_aliased(self):
        probes = [probe()]
        capabilities = ["read"]
        command = ["git", "--version"]
        r = receipt(probes=probes)
        g = grant(capabilities=capabilities)
        p = probe(command=command)
        digests = (r.digest(), g.digest(), p.digest())
        probes.append(probe(probe_id="late"))
        capabilities.append("destroy")
        command.append("--evil")
        self.assertEqual((r.digest(), g.digest(), p.digest()), digests)
        self.assertEqual(len(r.probes), 1)
        self.assertEqual(g.capabilities, ("read",))
        self.assertEqual(p.command, ("git", "--version"))


# --------------------------------------------------------------------------- validation


class ValidationFailureTests(unittest.TestCase):
    def test_probe_validation(self):
        with self.assertRaisesRegex(SchemaError, "status must be one of"):
            probe(status="ok")
        with self.assertRaisesRegex(SchemaError, "expires_at must be after observed_at"):
            probe(expires_at=T0)
        with self.assertRaisesRegex(SchemaError, "green probe cannot list missing"):
            probe(missing_requirements=["docker"])
        with self.assertRaisesRegex(SchemaError, "green probe must declare expires_at"):
            probe(expires_at=None)
        with self.assertRaisesRegex(SchemaError, "green probe cannot carry a reason_code"):
            probe(reason_code="AUTH_REQUIRED", wake_condition="login")
        with self.assertRaisesRegex(SchemaError, "red probe must carry a typed reason_code"):
            ProbeResult(probe_id="x", kind="git", status="red", observed_at=T0, summary="s", definition_digest=GIT_PROBE_DEFINITION)
        with self.assertRaisesRegex(SchemaError, "must state its wake_condition"):
            ProbeResult(probe_id="x", kind="git", status="unknown", observed_at=T0, summary="s", reason_code="AUTH_REQUIRED", definition_digest=GIT_PROBE_DEFINITION)
        with self.assertRaisesRegex(SchemaError, "must name at least one missing requirement"):
            ProbeResult(probe_id="x", kind="git", status="red", observed_at=T0, summary="s", reason_code="AUTH_REQUIRED", wake_condition="w", definition_digest=GIT_PROBE_DEFINITION)
        with self.assertRaisesRegex(SchemaError, "reason_code must be one of"):
            probe(status="red", reason_code="NOPE", wake_condition="w", expires_at=None)
        with self.assertRaisesRegex(SchemaError, "argv array"):
            probe(command="git --version")
        with self.assertRaisesRegex(SchemaError, "process probe must record its argv"):
            probe(command=[])
        with self.assertRaisesRegex(SchemaError, "filesystem probe cannot record an argv"):
            probe(method="filesystem")
        with self.assertRaisesRegex(SchemaError, "method must be one of"):
            probe(method="telepathy")
        self.assertEqual(probe(method="filesystem", command=[]).method, "filesystem")
        with self.assertRaisesRegex(SchemaError, "evidence_digest must look like"):
            probe(evidence_digest="abc")
        red = red_probe(missing_requirements=["docker daemon"])
        self.assertEqual(red.missing_requirements, ("docker daemon",))
        self.assertEqual(red.waiting_reason().code, "NEEDS_DOWNLOAD")
        self.assertIn("docker daemon", red.waiting_reason().detail)
        self.assertIsNone(probe().waiting_reason())

    def test_receipt_validation(self):
        with self.assertRaisesRegex(SchemaError, "plan_digest must look like"):
            receipt(plan_digest="plan")
        with self.assertRaisesRegex(SchemaError, "non-empty list"):
            receipt(probes=[])
        with self.assertRaisesRegex(SchemaError, "probe ids must be unique"):
            receipt(probes=[probe(), probe()])
        with self.assertRaisesRegex(SchemaError, "green receipt cannot contain a non-green probe"):
            receipt(probes=[red_probe()])
        with self.assertRaisesRegex(SchemaError, "expires_at must be after observed_at"):
            receipt(expires_at=T0)
        with self.assertRaisesRegex(SchemaError, "bound to target 'vm-7', receipt target is 'localhost'"):
            receipt(probes=[probe(target_id="vm-7")])
        red = receipt(status="red", probes=[probe(status="unknown")])
        self.assertFalse(red.is_fresh(T0))
        with self.assertRaisesRegex(SchemaError, "red receipt must contain at least one non-green probe"):
            receipt(status="red")
        self.assertIsNone(receipt(reservation_id=None).reservation_id)

    def test_workspace_filesystem_policy_is_not_a_trust_tier(self):
        self.assertEqual(FILESYSTEM_POLICIES, frozenset({"read_only", "isolated_worktree_write", "shared_checkout_write"}))
        self.assertFalse(FILESYSTEM_POLICIES & TRUST_TIERS)
        for tier in sorted(TRUST_TIERS):
            with self.assertRaisesRegex(SchemaError, "filesystem_policy must be one of"):
                workspace(filesystem_policy=tier)
        for policy in sorted(FILESYSTEM_POLICIES):
            self.assertEqual(workspace(filesystem_policy=policy).filesystem_policy, policy)
            with self.assertRaisesRegex(SchemaError, "trust_tier must be one of"):
                grant(trust_tier=policy)
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
        with self.assertRaisesRegex(SchemaError, "reservation box_id"):
            reservation(box_id="")

    def test_grant_and_authority_validation(self):
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
        with self.assertRaisesRegex(SchemaError, "authority_digest must look like"):
            grant(authority_digest="policy")
        with self.assertRaisesRegex(SchemaError, "unknown kinds: sudo"):
            authority(required_capabilities=["sudo"])
        with self.assertRaisesRegex(SchemaError, "credential_refs require the credential capability"):
            authority(credential_refs=["ref"])
        with self.assertRaisesRegex(SchemaError, "required_capabilities must not be empty"):
            authority(required_capabilities=[])

    def test_probe_policy_validation(self):
        with self.assertRaisesRegex(SchemaError, "non-empty list"):
            probe_policy(required_probes=[])
        with self.assertRaisesRegex(SchemaError, "probe ids must be unique"):
            probe_policy(required_probes=[ProbeRequirement("a", "git", True, OTHER), ProbeRequirement("a", "git", False, OTHER)])
        with self.assertRaisesRegex(SchemaError, "target_bound must be a boolean"):
            ProbeRequirement("a", "git", "yes", OTHER)
        with self.assertRaisesRegex(SchemaError, "definition_digest must look like"):
            ProbeRequirement("a", "git", True, "v1")
        with self.assertRaisesRegex(SchemaError, "probe definition_digest must look like"):
            probe(definition_digest="git-1.0")
        with self.assertRaisesRegex(SchemaError, "missing required field definition_digest"):
            ProbeResult.from_dict({k: v for k, v in probe().to_dict().items() if k != "definition_digest"})

    def test_fence_and_binding_validation(self):
        with self.assertRaisesRegex(SchemaError, "epoch must be a positive integer"):
            fence(epoch=0)
        with self.assertRaisesRegex(SchemaError, "grant_digest must look like"):
            fence(grant_digest="nope")
        with self.assertRaisesRegex(SchemaError, "expires_at must be after issued_at"):
            fence(expires_at=T0)
        with self.assertRaisesRegex(SchemaError, "box binding workspace_digest must look like"):
            binding(workspace_digest="ws")
        with self.assertRaisesRegex(SchemaError, "box binding target_id"):
            binding(target_id="")


# --------------------------------------------------------------------------- temporal validity


class TemporalValidityTests(unittest.TestCase):
    """Every time-bound record is valid on start <= now < expires_at."""

    def test_exact_start_is_fresh_and_exact_expiry_is_not(self):
        cases = [
            (probe(), "is_fresh"),
            (receipt(), "is_fresh"),
            (grant(), "is_fresh"),
            (reservation(), "is_active"),
            (fence(), "is_fresh"),
        ]
        for record, method in cases:
            check = getattr(record, method)
            self.assertTrue(check(T0), (type(record).__name__, "exact start"))
            self.assertTrue(check("2026-09-03T10:04:59.999999+00:00"), (type(record).__name__, "last microsecond"))
            self.assertFalse(check(T_PLUS_5M), (type(record).__name__, "exact expiry"))
            self.assertFalse(check(T_PLUS_10M), (type(record).__name__, "after expiry"))

    def test_future_dated_records_are_never_fresh(self):
        for record, method in [
            (probe(), "is_fresh"),
            (receipt(), "is_fresh"),
            (grant(), "is_fresh"),
            (reservation(), "is_active"),
            (fence(), "is_fresh"),
        ]:
            self.assertFalse(getattr(record, method)(T_MINUS_1M), type(record).__name__)
        # A receipt observed in the future relative to `now` is stale even if its probes were observed earlier.
        future = receipt(observed_at=T_PLUS_2M, expires_at=T_PLUS_5M, probes=[probe(observed_at=T0)])
        self.assertFalse(future.is_fresh(T_PLUS_1M))
        self.assertTrue(future.is_fresh(T_PLUS_2M))

    def test_red_or_released_records_are_never_fresh(self):
        self.assertFalse(red_probe().is_fresh(T0))
        self.assertFalse(probe(status="unknown", expires_at=None).is_fresh(T0))
        self.assertFalse(receipt(status="red", probes=[red_probe()]).is_fresh(T0))
        self.assertFalse(reservation(status="released").is_active(T0))
        self.assertFalse(reservation(status="expired").is_active(T0))


class TransitiveFreshnessTests(unittest.TestCase):
    """A green receipt can never outlive, or predate the observation of, its weakest probe."""

    def test_receipt_expiring_after_a_child_probe_is_rejected_at_construction(self):
        with self.assertRaisesRegex(SchemaError, "later than probe git-version expires_at"):
            receipt(expires_at=T_PLUS_5M, probes=[probe(expires_at=T_PLUS_2M)])
        with self.assertRaisesRegex(SchemaError, "later than probe weak expires_at"):
            receipt(expires_at=T_PLUS_5M, probes=[probe(), probe(probe_id="weak", expires_at=T_PLUS_3M)])

    def test_probe_observed_after_the_receipt_is_rejected(self):
        with self.assertRaisesRegex(SchemaError, "observed at .* after the receipt observation"):
            receipt(observed_at=T0, probes=[probe(observed_at=T_PLUS_1M)])
        # Also for red receipts: aggregation cannot claim to predate its inputs.
        with self.assertRaisesRegex(SchemaError, "after the receipt observation"):
            receipt(status="red", observed_at=T0, probes=[red_probe(observed_at=T_PLUS_1M)])

    def test_receipt_expiring_at_or_before_every_child_is_accepted(self):
        r = receipt(expires_at=T_PLUS_2M, probes=[probe(), probe(probe_id="weak", expires_at=T_PLUS_2M)])
        self.assertTrue(r.is_fresh(T_PLUS_1M))

    def test_green_child_probe_without_expiry_is_rejected(self):
        with self.assertRaisesRegex(SchemaError, "green probe must declare expires_at"):
            probe(expires_at=None)
        with self.assertRaisesRegex(SchemaError, "non-green probe"):
            receipt(probes=[red_probe()])

    def test_runtime_freshness_requires_every_probe_fresh(self):
        r = receipt(expires_at=T_PLUS_2M, probes=[probe(expires_at=T_PLUS_2M), probe(probe_id="other", expires_at=T_PLUS_5M)])
        self.assertTrue(r.is_fresh(T_PLUS_1M))
        self.assertFalse(r.is_fresh(T_PLUS_2M))
        stale_child = probe(expires_at=T_PLUS_1M)
        object.__setattr__(r, "probes", (stale_child,))
        self.assertFalse(r.is_fresh(T_PLUS_1M))


# --------------------------------------------------------------------------- binding fields


class BindingDigestTests(unittest.TestCase):
    def test_receipt_binding_fields_and_digest(self):
        r = receipt()
        self.assertEqual(set(r.binding()), set(ReadinessReceipt.BINDING_FIELDS))
        self.assertTrue(set(SUBJECT_FIELDS).issubset(ReadinessReceipt.BINDING_FIELDS))
        for name in ("box_binding_digest", "authority_digest", "probe_policy_digest", "workspace_digest", "evaluator_digest", "plan_digest", "reservation_id"):
            self.assertIn(name, ReadinessReceipt.BINDING_FIELDS)
        self.assertEqual(r.binding_digest(), canonical_digest(r.binding()))
        for name in ReadinessReceipt.BINDING_FIELDS:
            current = getattr(r, name)
            if name == "requested_model":
                changed = {name: "other-model"}
            elif current.startswith("sha256:"):
                changed = {name: OTHER}
            elif name == "target_id":
                changed = {name: "vm-7", "probes": [probe(target_id="vm-7")]}
            elif name == "clock":
                changed = {name: "synthetic"}
            else:
                changed = {name: current + "-x"}
            self.assertNotEqual(receipt(**changed).binding_digest(), r.binding_digest(), name)
        self.assertEqual(receipt(observed_at=T_PLUS_1M).binding_digest(), r.binding_digest())

    def test_fence_binds_every_input_digest_and_supersedes_by_epoch(self):
        f = fence()
        self.assertEqual(
            set(f.bound_digests()),
            {
                "plan_digest",
                "box_binding_digest",
                "evaluator_digest",
                "workspace_digest",
                "authority_digest",
                "probe_policy_digest",
                "readiness_digest",
                "grant_digest",
                "reservation_digest",
            },
        )
        self.assertEqual(f.readiness_digest, receipt().digest())
        self.assertEqual(f.box_binding_digest, binding().digest())
        self.assertNotEqual(receipt(observed_at=T_PLUS_1M).digest(), f.readiness_digest)
        newer = fence(lease_id="lease-2", epoch=2)
        self.assertTrue(newer.supersedes(f))
        self.assertFalse(f.supersedes(newer))
        self.assertFalse(fence(task_id="inventory", epoch=2).supersedes(f))

    def test_box_binding_digest_changes_with_every_field(self):
        base = binding()
        for name in ("run_id", "task_id", "box_id", "worker_id", "target_id", "workspace_id"):
            self.assertNotEqual(binding(**{name: getattr(base, name) + "-x"}).digest(), base.digest(), name)
        for name in ("plan_digest", "workspace_digest"):
            self.assertNotEqual(binding(**{name: OTHER}).digest(), base.digest(), name)
        self.assertEqual(base.subject(), {"run_id": "run-1", "task_id": "frame", "box_id": "box-1", "worker_id": "strategist", "target_id": "localhost"})

    def test_credential_scopes_are_fingerprints_not_secrets(self):
        r = receipt(credential_scopes=["provider:scope-fingerprint-1"])
        self.assertNotIn("token", json.dumps(r.to_dict()).lower())
        self.assertEqual(r.credential_scopes, ("provider:scope-fingerprint-1",))


# --------------------------------------------------------------------------- predicate


class ReadyToLeasePredicateTests(unittest.TestCase):
    def test_all_conjuncts_green_is_ready(self):
        decision = assess()
        self.assertTrue(decision.ready, decision.to_dict())
        self.assertEqual(decision.reasons, ())

    def test_each_failed_conjunct_yields_its_typed_reason(self):
        cases = [
            ({"plan_frozen": False}, "APPROVAL_REQUIRED"),
            ({"plan_digest": OTHER}, "POLICY_DENIED"),
            ({"control_plane_ready": False}, "OPERATOR_ATTENTION"),
            ({"dependencies_green": False}, "WAITING_DEPENDENCY"),
            ({"workspace": None}, "WORKSPACE_CONFLICT"),
            ({"receipt": None}, "READINESS_STALE"),
            ({"now": T_PLUS_10M}, "READINESS_STALE"),
            ({"now": T_MINUS_1M}, "READINESS_STALE"),
            ({"receipt": receipt(status="red", probes=[red_probe()])}, "READINESS_STALE"),
            ({"receipt": receipt(reservation_id=None)}, "READINESS_STALE"),
            ({"evaluator_ready": False}, "EVALUATOR_NOT_READY"),
            ({"evaluator_digest": OTHER}, "EVALUATOR_NOT_READY"),
            ({"authority_policy": None}, "APPROVAL_REQUIRED"),
            ({"grant": None}, "APPROVAL_REQUIRED"),
            ({"grant": grant(expires_at=T_PLUS_1M)}, "APPROVAL_REQUIRED"),
            ({"probe_policy": None}, "READINESS_STALE"),
            ({"reservation": None}, "CAPACITY_EXHAUSTED"),
            ({"reservation": reservation(status="released")}, "CAPACITY_EXHAUSTED"),
            ({"reservation": reservation(reservation_id="rsv-2")}, "READINESS_STALE"),
        ]
        for overrides, expected in cases:
            decision = assess(**overrides)
            self.assertFalse(decision.ready, overrides)
            self.assertIn(expected, [reason.code for reason in decision.reasons], overrides)
            for reason in decision.reasons:
                self.assertEqual(reason.task_id, "frame")
                self.assertEqual(reason.box_id, "box-1")
                self.assertTrue(reason.wake_condition)

    def test_expired_child_probe_makes_the_lease_not_ready(self):
        r = receipt(expires_at=T_PLUS_2M, probes=[probe(expires_at=T_PLUS_2M)])
        self.assertTrue(assess(receipt=r, now=T_PLUS_1M).ready)
        decision = assess(receipt=r, now=T_PLUS_3M)
        self.assertFalse(decision.ready)
        self.assertEqual([reason.code for reason in decision.reasons], ["READINESS_STALE"])

    def test_reasons_are_serializable_contracts(self):
        decision = assess(receipt=None, grant=None)
        restored = [WaitingReason.from_dict(item) for item in decision.to_dict()["reasons"]]
        self.assertEqual(tuple(restored), decision.reasons)


class AuthoritySufficiencyTests(unittest.TestCase):
    """AUTHORITY_GRANTED means the grant equals the frozen authority policy, not that a grant exists."""

    def codes(self, decision):
        return [reason.code for reason in decision.reasons]

    def test_empty_grant_is_insufficient(self):
        empty = grant(capabilities=[], filesystem_paths=[])
        decision = assess(grant=empty)
        self.assertFalse(decision.ready)
        self.assertIn("APPROVAL_REQUIRED", self.codes(decision))
        self.assertTrue(any("lacks required capabilities: execute, read, write" in reason.detail for reason in decision.reasons))

    def test_partial_grant_is_insufficient(self):
        decision = assess(grant=grant(capabilities=["read"]))
        self.assertFalse(decision.ready)
        self.assertIn("APPROVAL_REQUIRED", self.codes(decision))
        self.assertNotIn("POLICY_DENIED", self.codes(decision))

    def test_excessive_grant_is_unauthorized_not_silently_accepted(self):
        decision = assess(grant=grant(capabilities=["read", "write", "execute", "destroy"]))
        self.assertFalse(decision.ready)
        self.assertIn("POLICY_DENIED", self.codes(decision))
        self.assertTrue(any("did not authorize: destroy" in reason.detail for reason in decision.reasons))
        # Extra constraints (paths, network, credentials, trust tier) are also unauthorized.
        for overrides in (
            {"filesystem_paths": ["state/worktrees/frame", "/"]},
            {"capabilities": ["read", "write", "execute", "network"], "network_destinations": ["evil:443"]},
            {"capabilities": ["read", "write", "execute", "credential"], "credential_refs": ["keychain:prod"]},
            {"trust_tier": "sandboxed"},
        ):
            decision = assess(grant=grant(**overrides))
            self.assertFalse(decision.ready, overrides)
            self.assertIn("POLICY_DENIED", self.codes(decision), overrides)

    def test_stale_grant_is_not_authority(self):
        decision = assess(grant=grant(granted_at=T_MINUS_1M, expires_at=T_PLUS_1M))
        self.assertFalse(decision.ready)
        self.assertIn("APPROVAL_REQUIRED", self.codes(decision))

    def test_grant_bound_to_a_different_authority_policy_is_denied(self):
        decision = assess(grant=grant(authority_digest=OTHER))
        self.assertFalse(decision.ready)
        self.assertEqual(self.codes(decision), ["POLICY_DENIED"])

    def test_an_empty_authority_policy_cannot_exist_so_an_empty_grant_can_never_suffice(self):
        with self.assertRaisesRegex(SchemaError, "required_capabilities must not be empty"):
            authority(required_capabilities=[])
        # Even against a minimal one-capability policy, an empty grant is insufficient.
        minimal = authority(required_capabilities=["read"], filesystem_paths=[])
        empty = grant(authority_digest=minimal.digest(), capabilities=[], filesystem_paths=[])
        r = receipt(authority_digest=minimal.digest())
        decision = assess(authority_policy=minimal, grant=empty, receipt=r)
        self.assertFalse(decision.ready)
        self.assertIn("APPROVAL_REQUIRED", self.codes(decision))
        exact = grant(authority_digest=minimal.digest(), capabilities=["read"], filesystem_paths=[])
        self.assertTrue(assess(authority_policy=minimal, grant=exact, receipt=r).ready)


class IdentityBindingTests(unittest.TestCase):
    """No cross-run or cross-subject mixture of records may ever be ready."""

    def assertDenied(self, decision, label, code="POLICY_DENIED"):
        self.assertFalse(decision.ready, label)
        self.assertIn(code, [reason.code for reason in decision.reasons], label)

    def test_reported_scenario_three_runs_and_mismatched_worker_and_target(self):
        decision = assess(
            receipt=receipt(run_id="run-A"),
            grant=grant(run_id="run-B"),
            reservation=reservation(run_id="run-C", worker_id="builder", target_id="vm-7"),
        )
        self.assertFalse(decision.ready)
        details = "\n".join(reason.detail for reason in decision.reasons)
        for expected in ("readiness receipt run_id 'run-A'", "capability grant run_id 'run-B'", "capacity reservation run_id 'run-C'", "worker_id 'builder'", "target_id 'vm-7'"):
            self.assertIn(expected, details)
        self.assertEqual({reason.code for reason in decision.reasons}, {"POLICY_DENIED"})

    def test_receipt_subject_mismatches(self):
        for name in SUBJECT_FIELDS:
            overrides = {name: "other"}
            if name == "target_id":
                overrides["probes"] = [probe(target_id="other")]
            self.assertDenied(assess(receipt=receipt(**overrides)), "receipt." + name)

    def test_grant_subject_mismatches(self):
        for name in SUBJECT_FIELDS:
            self.assertDenied(assess(grant=grant(**{name: "other"})), "grant." + name)

    def test_reservation_subject_mismatches(self):
        for name in SUBJECT_FIELDS:
            self.assertDenied(assess(reservation=reservation(**{name: "other"})), "reservation." + name)

    def test_policy_subject_mismatches(self):
        for name in ("run_id", "task_id"):
            self.assertDenied(assess(authority_policy=authority(**{name: "other"})), "authority." + name)
            self.assertDenied(assess(probe_policy=probe_policy(**{name: "other"})), "probe_policy." + name)

    def test_binding_subject_mismatch_denies_every_consistent_record(self):
        # Records agree with each other but the binding names another subject.
        for name in SUBJECT_FIELDS:
            decision = assess(binding=binding(**{name: "other"}))
            self.assertDenied(decision, "binding." + name)

    def test_cross_workspace_evaluator_reservation_authority_and_probe_policy(self):
        other_ws = workspace(workspace_id="ws-2")
        self.assertDenied(assess(workspace=other_ws), "cross-workspace receipt", "WORKSPACE_CONFLICT")
        self.assertDenied(assess(binding=binding(workspace_digest=OTHER)), "binding workspace digest", "WORKSPACE_CONFLICT")
        self.assertDenied(assess(receipt=receipt(workspace_digest=OTHER)), "receipt workspace digest", "WORKSPACE_CONFLICT")
        self.assertDenied(assess(receipt=receipt(evaluator_digest=OTHER)), "cross-evaluator", "EVALUATOR_NOT_READY")
        self.assertDenied(assess(receipt=receipt(reservation_id="rsv-9")), "cross-reservation", "READINESS_STALE")
        self.assertDenied(assess(receipt=receipt(authority_digest=OTHER)), "cross-authority receipt")
        self.assertDenied(assess(receipt=receipt(box_binding_digest=OTHER)), "cross-binding receipt")
        self.assertDenied(assess(receipt=receipt(probe_policy_digest=OTHER)), "wrong probe policy digest")

    def test_wrong_probe_policy_content_is_not_readiness(self):
        wider = probe_policy(required_probes=[ProbeRequirement("git-version", "git", True, GIT_PROBE_DEFINITION), ProbeRequirement("docker", "container", True, OTHER)])
        r = receipt(probe_policy_digest=wider.digest())
        decision = assess(probe_policy=wider, receipt=r)
        self.assertDenied(decision, "missing required probe", "READINESS_STALE")
        self.assertTrue(any("required probe docker is missing" in reason.detail for reason in decision.reasons))
        # Extra probes not in the policy are also rejected: a receipt is not "some green probes".
        extra = receipt(probes=[probe(), probe(probe_id="extra", kind="git")])
        self.assertDenied(assess(receipt=extra), "extra probe is unlisted proof", "POLICY_DENIED")
        # Kind mismatch and unbound target-bound probe.
        wrong_kind = receipt(probes=[probe(kind="container")])
        self.assertDenied(assess(receipt=wrong_kind), "kind mismatch", "READINESS_STALE")
        unbound = receipt(probes=[probe(target_id=None)])
        self.assertDenied(assess(receipt=unbound), "target-bound probe without target", "READINESS_STALE")

    def test_probe_from_a_different_definition_does_not_satisfy_the_policy(self):
        # Same probe id, kind, and target, but produced by another implementation/version/config.
        other_impl = receipt(probes=[probe(definition_digest=OTHER)])
        decision = assess(receipt=other_impl)
        self.assertDenied(decision, "definition digest mismatch")
        self.assertTrue(any("different probe definition" in reason.detail for reason in decision.reasons))
        self.assertEqual(
            probe_policy().coverage_errors(other_impl),
            [("POLICY_DENIED", "probe git-version was produced by a different probe definition than the policy froze")],
        )

    def test_synthetic_clock_receipts_are_never_evidence(self):
        synthetic = receipt(clock="synthetic")
        self.assertFalse(synthetic.is_evidence())
        self.assertTrue(receipt().is_evidence())
        self.assertNotEqual(synthetic.digest(), receipt().digest())
        self.assertNotEqual(synthetic.binding_digest(), receipt().binding_digest(), "clock is a binding field")
        decision = assess(receipt=synthetic)
        self.assertDenied(decision, "synthetic clock")
        self.assertTrue(any("synthetic clock" in reason.detail for reason in decision.reasons))
        with self.assertRaisesRegex(SchemaError, "clock must be one of"):
            receipt(clock="wall")
        self.assertEqual(ReadinessReceipt.from_dict(synthetic.to_dict()), synthetic)

    def test_target_bound_probe_must_name_the_receipt_target(self):
        with self.assertRaisesRegex(SchemaError, "bound to target 'vm-7'"):
            receipt(probes=[probe(target_id="vm-7")])

    def test_every_denial_names_the_offending_field_and_values(self):
        decision = assess(grant=grant(run_id="run-B"))
        self.assertEqual(len(decision.reasons), 1)
        self.assertEqual(decision.reasons[0].detail, "capability grant run_id 'run-B' does not match expected 'run-1'")


# --------------------------------------------------------------------------- events


class EventAndProjectionTests(unittest.TestCase):
    RUN = "three-agent-demo"

    def _run_created(self):
        runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        return new_event(self.RUN, "RUN_CREATED", "orchestrator", {"runbook": runbook, "plan_digest": canonical_digest(runbook)})

    def _ledger_receipt(self, state, **overrides):
        values = dict(run_id=self.RUN, plan_digest=state["plan_digest"], task_id="frame", worker_id="strategist")
        values.update(overrides)
        return receipt(**values)

    def test_legacy_event_stream_replays_to_the_same_projection_plus_empty_receipts(self):
        legacy_state = project([self._run_created()])
        self.assertEqual(legacy_state["readiness_receipts"], {})
        stripped = {key: value for key, value in legacy_state.items() if key != "readiness_receipts"}
        self.assertEqual(set(stripped), set(empty_state()) - {"readiness_receipts"})
        self.assertNotIn("READINESS_RECORDED", LEGACY_EVENT_TYPES)
        self.assertNotIn("TASK_WAITING", LEGACY_EVENT_TYPES)
        for task in legacy_state["tasks"].values():
            self.assertNotIn("waiting", task)

    def test_readiness_recorded_projects_a_validated_bound_receipt(self):
        state = project([self._run_created()])
        bound = self._ledger_receipt(state)
        state = apply_event(state, new_event(self.RUN, "READINESS_RECORDED", "doctor", {"receipt": bound.to_dict()}))
        self.assertEqual(state["readiness_receipts"]["rcpt-1"], bound.to_dict())

    def test_corrupted_ledger_receipts_are_rejected_on_replay(self):
        state = project([self._run_created()])
        good = self._ledger_receipt(state)
        cases = [
            (dict(good.to_dict(), schema_version=3), SchemaError, "schema_version 3 is not supported"),
            (self._ledger_receipt(state, run_id="other-run").to_dict(), ValueError, "run_id 'other-run' does not match event run_id"),
            (self._ledger_receipt(state, plan_digest=OTHER).to_dict(), ValueError, "different plan digest"),
            (self._ledger_receipt(state, task_id="ghost").to_dict(), ValueError, "unknown task 'ghost'"),
            (self._ledger_receipt(state, worker_id="ghost").to_dict(), ValueError, "unknown worker 'ghost'"),
            (dict(good.to_dict(), probes=[dict(probe().to_dict(), target_id="vm-7")]), SchemaError, "bound to target 'vm-7'"),
        ]
        for payload, error, message in cases:
            with self.assertRaisesRegex(error, message):
                apply_event(state, new_event(self.RUN, "READINESS_RECORDED", "doctor", {"receipt": payload}))
        with_first = apply_event(state, new_event(self.RUN, "READINESS_RECORDED", "doctor", {"receipt": good.to_dict()}))
        with self.assertRaisesRegex(ValueError, "receipt id already exists"):
            apply_event(with_first, new_event(self.RUN, "READINESS_RECORDED", "doctor", {"receipt": good.to_dict()}))

    def test_foreign_receipt_cannot_be_smuggled_through_a_matching_event_run_id(self):
        state = project([self._run_created()])
        foreign = receipt(run_id="run-1", plan_digest=state["plan_digest"])
        with self.assertRaisesRegex(ValueError, "run_id 'run-1' does not match event run_id 'three-agent-demo'"):
            apply_event(state, new_event(self.RUN, "READINESS_RECORDED", "doctor", {"receipt": foreign.to_dict()}))

    def test_task_waiting_and_cleared_round_trip_through_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteEventStore(Path(temporary) / "events.sqlite3")
            try:
                store.append(self._run_created())
                reason = WaitingReason(code="EVALUATOR_NOT_READY", detail="frozen evaluator bundle is not launchable", wake_condition="evaluator probe green", task_id="frame")
                store.append(new_event(self.RUN, "TASK_WAITING", "orchestrator", {"task_id": "frame", "reason": reason.to_dict()}))
                waiting = project(store.read(self.RUN))
                self.assertEqual(waiting["tasks"]["frame"]["status"], "waiting")
                self.assertEqual(WaitingReason.from_dict(waiting["tasks"]["frame"]["waiting"]), reason)
                store.append(new_event(self.RUN, "TASK_WAIT_CLEARED", "orchestrator", {"task_id": "frame"}))
                cleared = project(store.read(self.RUN))
                self.assertEqual(cleared["tasks"]["frame"]["status"], "pending")
                self.assertIsNone(cleared["tasks"]["frame"]["waiting"])
            finally:
                store.close()

    def test_cross_task_and_unbound_waiting_reasons_are_rejected(self):
        state = project([self._run_created()])
        cross = WaitingReason(code="AUTH_REQUIRED", detail="x", task_id="inventory").to_dict()
        with self.assertRaisesRegex(ValueError, "reason task_id 'inventory' does not match payload task_id 'frame'"):
            apply_event(state, new_event(self.RUN, "TASK_WAITING", "o", {"task_id": "frame", "reason": cross}))
        unbound = WaitingReason(code="AUTH_REQUIRED", detail="x").to_dict()
        with self.assertRaisesRegex(ValueError, "reason task_id None does not match payload task_id 'frame'"):
            apply_event(state, new_event(self.RUN, "TASK_WAITING", "o", {"task_id": "frame", "reason": unbound}))
        ghost = WaitingReason(code="AUTH_REQUIRED", detail="x", task_id="ghost").to_dict()
        with self.assertRaisesRegex(ValueError, "unknown task 'ghost'"):
            apply_event(state, new_event(self.RUN, "TASK_WAITING", "o", {"task_id": "ghost", "reason": ghost}))
        with self.assertRaisesRegex(ValueError, "unknown task 'ghost'"):
            apply_event(state, new_event(self.RUN, "TASK_WAIT_CLEARED", "o", {"task_id": "ghost"}))

    def test_waiting_transitions_are_guarded(self):
        state = project([self._run_created()])
        with self.assertRaisesRegex(ValueError, "not waiting"):
            apply_event(state, new_event(self.RUN, "TASK_WAIT_CLEARED", "o", {"task_id": "frame"}))
        bad_reason = {"schema": "camol.waiting_reason", "schema_version": 1, "code": "NOPE", "detail": "x", "task_id": "frame"}
        with self.assertRaisesRegex(SchemaError, "code must be one of"):
            apply_event(state, new_event(self.RUN, "TASK_WAITING", "o", {"task_id": "frame", "reason": bad_reason}))

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
                self.assertEqual(assignments, [])
                projected = orchestrator.state(run_id)
                self.assertEqual(projected["tasks"]["frame"]["waiting"]["code"], "AUTH_REQUIRED")
                self.assertEqual(projected["tasks"]["inventory"]["waiting"]["code"], "READINESS_STALE")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
