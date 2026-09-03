"""Versioned readiness, workspace, reservation, grant, and lease-fence contracts.

Status of this module: CONTRACTS ONLY. Nothing here probes a host, creates a
worktree, talks to a provider, reserves real capacity, or launches an agent. The
scheduler in :mod:`camol.orchestrator` does not yet consume these records; see
``tests/test_readiness.py`` for the characterization of that unsafe boundary.

Every contract:

* carries an explicit ``schema`` name and integer ``schema_version``;
* round-trips through ``to_dict`` / ``from_dict`` deterministically;
* rejects unknown fields, unknown versions, and malformed values with
  :class:`camol.schema.SchemaError`;
* hashes through :func:`camol.schema.canonical_digest`.

Timestamps are ISO-8601 strings normalized to UTC. Expiry is represented as an
explicit ``expires_at`` field plus a pure ``is_fresh(now)`` check; no contract
consults the wall clock on its own.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import (
    SchemaError,
    canonical_digest,
    parse_timestamp,
    reject_unknown_fields,
    require_choice,
    require_digest,
    require_identifier,
    require_non_negative_int,
    require_object,
    require_optional_digest,
    require_optional_string,
    require_optional_timestamp,
    require_positive_int,
    require_schema_header,
    require_string,
    require_string_list,
    require_timestamp,
)

__all__ = [
    "NON_RUNNABLE_REASONS",
    "WaitingReason",
    "PROBE_STATUSES",
    "RECEIPT_STATUSES",
    "RESERVATION_STATUSES",
    "TRUST_TIERS",
    "FILESYSTEM_POLICIES",
    "CAPABILITY_KINDS",
    "CLEANUP_OWNERS",
    "ProbeResult",
    "ReadinessReceipt",
    "WorkspaceReceipt",
    "CapacityReservation",
    "CapabilityGrant",
    "LeaseFence",
    "ReadinessDecision",
    "assess_ready_to_lease",
]


# --------------------------------------------------------------------------- vocabularies

NON_RUNNABLE_REASONS = frozenset(
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
)

PROBE_STATUSES = frozenset({"green", "red", "unknown"})
RECEIPT_STATUSES = frozenset({"green", "red"})
RESERVATION_STATUSES = frozenset({"reserved", "released", "expired"})
TRUST_TIERS = frozenset({"developer_trusted", "developer_sandboxed", "sandboxed"})
# What a box may actually do to source. Distinct from TRUST_TIERS, which describe
# the process/credential posture of the worker and its grant.
#   read_only                the box may read the workspace and write nothing
#   isolated_worktree_write  the box may write only inside a dedicated task worktree
#   shared_checkout_write    the box writes into a shared checkout; this is the
#                            pre-M2 simulator posture and can never back a claim
FILESYSTEM_POLICIES = frozenset({"read_only", "isolated_worktree_write", "shared_checkout_write"})
CAPABILITY_KINDS = frozenset({"read", "write", "execute", "network", "deploy", "credential", "destroy"})
CLEANUP_OWNERS = frozenset({"camol", "adopted"})


def _tuple(value: Any, label: str) -> Tuple[str, ...]:
    """Validate a string collection and freeze it as a tuple (deep immutability)."""
    if isinstance(value, tuple):
        value = list(value)
    return tuple(require_string_list(value, label, sort=True))


def _argv(value: Any, label: str) -> Tuple[str, ...]:
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SchemaError("{} must be an argv array of strings".format(label))
    return tuple(value)


def _fresh(expires_at: Optional[str], now: str, label: str) -> bool:
    """True when ``now`` is strictly before ``expires_at``; a missing expiry is never fresh."""
    if expires_at is None:
        return False
    return parse_timestamp(now, "now") < parse_timestamp(expires_at, label)


# --------------------------------------------------------------------------- WaitingReason


@dataclass(frozen=True)
class WaitingReason:
    """A typed, serializable reason a task or box is not runnable right now."""

    SCHEMA = "camol.waiting_reason"
    SCHEMA_VERSION = 1
    FIELDS = ("schema", "schema_version", "code", "detail", "wake_condition", "task_id", "box_id")

    code: str
    detail: str
    wake_condition: Optional[str] = None
    task_id: Optional[str] = None
    box_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.code not in NON_RUNNABLE_REASONS:
            raise SchemaError(
                "waiting reason code must be one of: {}".format(", ".join(sorted(NON_RUNNABLE_REASONS)))
            )
        require_string(self.detail, "waiting reason detail")
        require_optional_string(self.wake_condition, "waiting reason wake_condition")
        if self.task_id is not None:
            require_identifier(self.task_id, "waiting reason task_id")
        if self.box_id is not None:
            require_identifier(self.box_id, "waiting reason box_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "code": self.code,
            "detail": self.detail,
            "wake_condition": self.wake_condition,
            "task_id": self.task_id,
            "box_id": self.box_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WaitingReason":
        data = require_object(payload, "waiting reason")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "waiting reason")
        reject_unknown_fields(data, cls.FIELDS, "waiting reason")
        return cls(
            code=data.get("code"),
            detail=data.get("detail"),
            wake_condition=data.get("wake_condition"),
            task_id=data.get("task_id"),
            box_id=data.get("box_id"),
        )


# --------------------------------------------------------------------------- ProbeResult


@dataclass(frozen=True)
class ProbeResult:
    """One read-only observation about one readiness dimension.

    ``command`` is an argv array (empty when the probe was not a subprocess).
    ``summary`` and ``missing_requirements`` must already be redacted by the
    producer; the contract carries no secret-bearing field.
    """

    SCHEMA = "camol.probe_result"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "probe_id",
        "kind",
        "target_id",
        "status",
        "observed_at",
        "expires_at",
        "command",
        "tool_version",
        "summary",
        "missing_requirements",
        "evidence_digest",
    )

    probe_id: str
    kind: str
    status: str
    observed_at: str
    summary: str
    target_id: Optional[str] = None
    expires_at: Optional[str] = None
    command: Tuple[str, ...] = ()
    tool_version: Optional[str] = None
    missing_requirements: Tuple[str, ...] = ()
    evidence_digest: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "probe_id", require_identifier(self.probe_id, "probe_id"))
        object.__setattr__(self, "kind", require_identifier(self.kind, "probe kind"))
        if self.target_id is not None:
            object.__setattr__(self, "target_id", require_identifier(self.target_id, "probe target_id"))
        object.__setattr__(self, "status", require_choice(self.status, "probe status", PROBE_STATUSES))
        object.__setattr__(self, "observed_at", require_timestamp(self.observed_at, "probe observed_at"))
        object.__setattr__(self, "expires_at", require_optional_timestamp(self.expires_at, "probe expires_at"))
        if self.expires_at is not None and parse_timestamp(self.expires_at, "x") <= parse_timestamp(
            self.observed_at, "y"
        ):
            raise SchemaError("probe expires_at must be after observed_at")
        object.__setattr__(self, "command", _argv(self.command, "probe command"))
        object.__setattr__(self, "tool_version", require_optional_string(self.tool_version, "probe tool_version"))
        object.__setattr__(self, "summary", require_string(self.summary, "probe summary"))
        object.__setattr__(
            self, "missing_requirements", _tuple(self.missing_requirements, "probe missing_requirements")
        )
        object.__setattr__(
            self, "evidence_digest", require_optional_digest(self.evidence_digest, "probe evidence_digest")
        )
        if self.status == "green" and self.missing_requirements:
            raise SchemaError("a green probe cannot list missing requirements")
        if self.status == "green" and self.expires_at is None:
            raise SchemaError("a green probe must declare expires_at; proof without expiry is not proof")

    def is_fresh(self, now: str) -> bool:
        return self.status == "green" and _fresh(self.expires_at, now, "probe expires_at")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "probe_id": self.probe_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "status": self.status,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "command": list(self.command),
            "tool_version": self.tool_version,
            "summary": self.summary,
            "missing_requirements": list(self.missing_requirements),
            "evidence_digest": self.evidence_digest,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProbeResult":
        data = require_object(payload, "probe result")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "probe result")
        reject_unknown_fields(data, cls.FIELDS, "probe result")
        for required in ("probe_id", "kind", "status", "observed_at", "summary"):
            if required not in data:
                raise SchemaError("probe result is missing required field {}".format(required))
        return cls(
            probe_id=data["probe_id"],
            kind=data["kind"],
            target_id=data.get("target_id"),
            status=data["status"],
            observed_at=data["observed_at"],
            expires_at=data.get("expires_at"),
            command=data.get("command", []),
            tool_version=data.get("tool_version"),
            summary=data["summary"],
            missing_requirements=data.get("missing_requirements", []),
            evidence_digest=data.get("evidence_digest"),
        )


# --------------------------------------------------------------------------- WorkspaceReceipt


@dataclass(frozen=True)
class WorkspaceReceipt:
    """Identity of an isolated workspace a box may work in.

    ``path`` is recorded as a string and is not resolved, created, or checked in
    M0. ``dirty_digest`` is the canonical digest of the workspace's uncommitted
    state; ``sha256`` of an empty canonical list is the clean value.
    ``filesystem_policy`` describes source access (see ``FILESYSTEM_POLICIES``);
    the worker's trust tier lives on the grant, not here.
    """

    SCHEMA = "camol.workspace_receipt"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "workspace_id",
        "repository_id",
        "base_revision",
        "branch",
        "path",
        "dirty_digest",
        "filesystem_policy",
        "cleanup_owner",
        "created_at",
    )

    workspace_id: str
    repository_id: str
    base_revision: str
    branch: str
    path: str
    dirty_digest: str
    filesystem_policy: str
    cleanup_owner: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", require_identifier(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "repository_id", require_string(self.repository_id, "repository_id"))
        object.__setattr__(self, "base_revision", require_string(self.base_revision, "base_revision"))
        object.__setattr__(self, "branch", require_string(self.branch, "branch"))
        object.__setattr__(self, "path", require_string(self.path, "workspace path"))
        object.__setattr__(self, "dirty_digest", require_digest(self.dirty_digest, "dirty_digest"))
        object.__setattr__(
            self, "filesystem_policy", require_choice(self.filesystem_policy, "filesystem_policy", FILESYSTEM_POLICIES)
        )
        object.__setattr__(self, "cleanup_owner", require_choice(self.cleanup_owner, "cleanup_owner", CLEANUP_OWNERS))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, "workspace created_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "repository_id": self.repository_id,
            "base_revision": self.base_revision,
            "branch": self.branch,
            "path": self.path,
            "dirty_digest": self.dirty_digest,
            "filesystem_policy": self.filesystem_policy,
            "cleanup_owner": self.cleanup_owner,
            "created_at": self.created_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceReceipt":
        data = require_object(payload, "workspace receipt")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "workspace receipt")
        reject_unknown_fields(data, cls.FIELDS, "workspace receipt")
        missing = [name for name in cls.FIELDS if name not in data]
        if missing:
            raise SchemaError("workspace receipt is missing required fields: {}".format(", ".join(missing)))
        return cls(**{name: data[name] for name in cls.FIELDS if name not in ("schema", "schema_version")})


# --------------------------------------------------------------------------- CapacityReservation


@dataclass(frozen=True)
class CapacityReservation:
    """A bounded, expiring claim on concurrency, tokens, and optional spend."""

    SCHEMA = "camol.capacity_reservation"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "reservation_id",
        "run_id",
        "task_id",
        "worker_id",
        "target_id",
        "concurrency_slots",
        "max_tokens",
        "max_usd_cents",
        "status",
        "reserved_at",
        "expires_at",
    )

    reservation_id: str
    run_id: str
    task_id: str
    worker_id: str
    target_id: str
    concurrency_slots: int
    max_tokens: int
    status: str
    reserved_at: str
    expires_at: str
    max_usd_cents: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("reservation_id", "run_id", "task_id", "worker_id", "target_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "reservation " + name))
        object.__setattr__(self, "concurrency_slots", require_positive_int(self.concurrency_slots, "concurrency_slots"))
        object.__setattr__(self, "max_tokens", require_positive_int(self.max_tokens, "max_tokens"))
        if self.max_usd_cents is not None:
            object.__setattr__(self, "max_usd_cents", require_non_negative_int(self.max_usd_cents, "max_usd_cents"))
        object.__setattr__(self, "status", require_choice(self.status, "reservation status", RESERVATION_STATUSES))
        object.__setattr__(self, "reserved_at", require_timestamp(self.reserved_at, "reserved_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "reservation expires_at"))
        if parse_timestamp(self.expires_at, "x") <= parse_timestamp(self.reserved_at, "y"):
            raise SchemaError("reservation expires_at must be after reserved_at")

    def is_active(self, now: str) -> bool:
        return self.status == "reserved" and _fresh(self.expires_at, now, "reservation expires_at")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "reservation_id": self.reservation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "target_id": self.target_id,
            "concurrency_slots": self.concurrency_slots,
            "max_tokens": self.max_tokens,
            "max_usd_cents": self.max_usd_cents,
            "status": self.status,
            "reserved_at": self.reserved_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapacityReservation":
        data = require_object(payload, "capacity reservation")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "capacity reservation")
        reject_unknown_fields(data, cls.FIELDS, "capacity reservation")
        required = [name for name in cls.FIELDS if name not in ("schema", "schema_version", "max_usd_cents")]
        missing = [name for name in required if name not in data]
        if missing:
            raise SchemaError("capacity reservation is missing required fields: {}".format(", ".join(missing)))
        return cls(**{name: data[name] for name in required}, max_usd_cents=data.get("max_usd_cents"))


# --------------------------------------------------------------------------- CapabilityGrant


@dataclass(frozen=True)
class CapabilityGrant:
    """Deny-by-default authority attached to one task on one box.

    ``credential_refs`` are opaque reference names resolved only inside an
    authorized adapter; the grant never carries a secret value.
    """

    SCHEMA = "camol.capability_grant"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "grant_id",
        "run_id",
        "task_id",
        "box_id",
        "capabilities",
        "filesystem_paths",
        "network_destinations",
        "credential_refs",
        "trust_tier",
        "granted_by",
        "granted_at",
        "expires_at",
    )

    grant_id: str
    run_id: str
    task_id: str
    box_id: str
    capabilities: Tuple[str, ...]
    trust_tier: str
    granted_by: str
    granted_at: str
    expires_at: str
    filesystem_paths: Tuple[str, ...] = ()
    network_destinations: Tuple[str, ...] = ()
    credential_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("grant_id", "run_id", "task_id", "box_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "grant " + name))
        capabilities = _tuple(self.capabilities, "grant capabilities")
        unknown = sorted(set(capabilities) - CAPABILITY_KINDS)
        if unknown:
            raise SchemaError("grant capabilities contain unknown kinds: {}".format(", ".join(unknown)))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "filesystem_paths", _tuple(self.filesystem_paths, "filesystem_paths"))
        object.__setattr__(self, "network_destinations", _tuple(self.network_destinations, "network_destinations"))
        object.__setattr__(self, "credential_refs", _tuple(self.credential_refs, "credential_refs"))
        if self.credential_refs and "credential" not in capabilities:
            raise SchemaError("credential_refs require the credential capability")
        if self.network_destinations and "network" not in capabilities:
            raise SchemaError("network_destinations require the network capability")
        object.__setattr__(self, "trust_tier", require_choice(self.trust_tier, "trust_tier", TRUST_TIERS))
        object.__setattr__(self, "granted_by", require_string(self.granted_by, "granted_by"))
        object.__setattr__(self, "granted_at", require_timestamp(self.granted_at, "granted_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "grant expires_at"))
        if parse_timestamp(self.expires_at, "x") <= parse_timestamp(self.granted_at, "y"):
            raise SchemaError("grant expires_at must be after granted_at")

    def is_fresh(self, now: str) -> bool:
        return _fresh(self.expires_at, now, "grant expires_at")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "grant_id": self.grant_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "box_id": self.box_id,
            "capabilities": list(self.capabilities),
            "filesystem_paths": list(self.filesystem_paths),
            "network_destinations": list(self.network_destinations),
            "credential_refs": list(self.credential_refs),
            "trust_tier": self.trust_tier,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityGrant":
        data = require_object(payload, "capability grant")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "capability grant")
        reject_unknown_fields(data, cls.FIELDS, "capability grant")
        required = ("grant_id", "run_id", "task_id", "box_id", "capabilities", "trust_tier", "granted_by", "granted_at", "expires_at")
        missing = [name for name in required if name not in data]
        if missing:
            raise SchemaError("capability grant is missing required fields: {}".format(", ".join(missing)))
        return cls(
            **{name: data[name] for name in required},
            filesystem_paths=data.get("filesystem_paths", []),
            network_destinations=data.get("network_destinations", []),
            credential_refs=data.get("credential_refs", []),
        )


# --------------------------------------------------------------------------- ReadinessReceipt


@dataclass(frozen=True)
class ReadinessReceipt:
    """Proof that one task/box combination was ready at ``observed_at``.

    The receipt binds every identity and digest that the lease will later fence
    on. ``requested_model`` is an opaque profile string (vendor-neutral);
    ``credential_scopes`` are scope fingerprints, never secrets.
    """

    SCHEMA = "camol.readiness_receipt"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "receipt_id",
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
        "credential_scopes",
        "reservation_id",
        "probes",
        "status",
        "observed_at",
        "expires_at",
    )
    BINDING_FIELDS = (
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
    )

    receipt_id: str
    run_id: str
    plan_digest: str
    task_id: str
    box_id: str
    worker_id: str
    target_id: str
    transport_id: str
    runtime_id: str
    workspace_digest: str
    evaluator_digest: str
    adapter_kind: str
    requested_model: Optional[str]
    reservation_id: str
    probes: Tuple[ProbeResult, ...]
    status: str
    observed_at: str
    expires_at: str
    credential_scopes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "run_id",
            "task_id",
            "box_id",
            "worker_id",
            "target_id",
            "transport_id",
            "runtime_id",
            "adapter_kind",
            "reservation_id",
        ):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "receipt " + name))
        object.__setattr__(self, "plan_digest", require_digest(self.plan_digest, "receipt plan_digest"))
        object.__setattr__(self, "workspace_digest", require_digest(self.workspace_digest, "receipt workspace_digest"))
        object.__setattr__(self, "evaluator_digest", require_digest(self.evaluator_digest, "receipt evaluator_digest"))
        object.__setattr__(self, "requested_model", require_optional_string(self.requested_model, "requested_model"))
        object.__setattr__(self, "credential_scopes", _tuple(self.credential_scopes, "credential_scopes"))
        probes = list(self.probes) if isinstance(self.probes, (list, tuple)) else None
        if not probes:
            raise SchemaError("receipt probes must be a non-empty list of ProbeResult")
        if any(not isinstance(probe, ProbeResult) for probe in probes):
            raise SchemaError("receipt probes must be ProbeResult instances")
        probe_ids = [probe.probe_id for probe in probes]
        if len(set(probe_ids)) != len(probe_ids):
            raise SchemaError("receipt probe ids must be unique")
        object.__setattr__(self, "probes", tuple(sorted(probes, key=lambda probe: probe.probe_id)))
        object.__setattr__(self, "status", require_choice(self.status, "receipt status", RECEIPT_STATUSES))
        object.__setattr__(self, "observed_at", require_timestamp(self.observed_at, "receipt observed_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "receipt expires_at"))
        expires = parse_timestamp(self.expires_at, "x")
        if expires <= parse_timestamp(self.observed_at, "y"):
            raise SchemaError("receipt expires_at must be after observed_at")
        if self.status == "green":
            # Construction-time coherence: a green receipt cannot outlive its
            # weakest probe, and every probe must itself be green with an expiry.
            for probe in self.probes:
                if probe.status != "green":
                    raise SchemaError("a green receipt cannot contain a non-green probe ({})".format(probe.probe_id))
                if probe.expires_at is None:
                    raise SchemaError("a green receipt requires probe {} to declare expires_at".format(probe.probe_id))
                if expires > parse_timestamp(probe.expires_at, "z"):
                    raise SchemaError(
                        "receipt expires_at {} is later than probe {} expires_at {}".format(
                            self.expires_at, probe.probe_id, probe.expires_at
                        )
                    )

    def binding(self) -> Dict[str, Any]:
        """The identity/digest fields a lease fence must match exactly."""
        return {name: getattr(self, name) for name in self.BINDING_FIELDS}

    def binding_digest(self) -> str:
        return canonical_digest(self.binding())

    def is_fresh(self, now: str) -> bool:
        """Green, unexpired, and every underlying probe still green and unexpired."""
        if self.status != "green" or not _fresh(self.expires_at, now, "receipt expires_at"):
            return False
        return all(probe.is_fresh(now) for probe in self.probes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "plan_digest": self.plan_digest,
            "task_id": self.task_id,
            "box_id": self.box_id,
            "worker_id": self.worker_id,
            "target_id": self.target_id,
            "transport_id": self.transport_id,
            "runtime_id": self.runtime_id,
            "workspace_digest": self.workspace_digest,
            "evaluator_digest": self.evaluator_digest,
            "adapter_kind": self.adapter_kind,
            "requested_model": self.requested_model,
            "credential_scopes": list(self.credential_scopes),
            "reservation_id": self.reservation_id,
            "probes": [probe.to_dict() for probe in self.probes],
            "status": self.status,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReadinessReceipt":
        data = require_object(payload, "readiness receipt")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "readiness receipt")
        reject_unknown_fields(data, cls.FIELDS, "readiness receipt")
        required = [name for name in cls.FIELDS if name not in ("schema", "schema_version", "credential_scopes")]
        missing = [name for name in required if name not in data]
        if missing:
            raise SchemaError("readiness receipt is missing required fields: {}".format(", ".join(missing)))
        probes_raw = data["probes"]
        if not isinstance(probes_raw, list):
            raise SchemaError("readiness receipt probes must be a list")
        probes = tuple(ProbeResult.from_dict(item) for item in probes_raw)
        values = {name: data[name] for name in required if name != "probes"}
        return cls(probes=probes, credential_scopes=data.get("credential_scopes", []), **values)


# --------------------------------------------------------------------------- LeaseFence


@dataclass(frozen=True)
class LeaseFence:
    """Monotonic fencing token binding a lease to every input it was proven against."""

    SCHEMA = "camol.lease_fence"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "lease_id",
        "run_id",
        "task_id",
        "box_id",
        "worker_id",
        "epoch",
        "plan_digest",
        "evaluator_digest",
        "workspace_digest",
        "readiness_digest",
        "grant_digest",
        "reservation_digest",
        "issued_at",
        "expires_at",
    )
    BOUND_DIGESTS = (
        "plan_digest",
        "evaluator_digest",
        "workspace_digest",
        "readiness_digest",
        "grant_digest",
        "reservation_digest",
    )

    lease_id: str
    run_id: str
    task_id: str
    box_id: str
    worker_id: str
    epoch: int
    plan_digest: str
    evaluator_digest: str
    workspace_digest: str
    readiness_digest: str
    grant_digest: str
    reservation_digest: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        for name in ("lease_id", "run_id", "task_id", "box_id", "worker_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "fence " + name))
        object.__setattr__(self, "epoch", require_positive_int(self.epoch, "fence epoch"))
        for name in self.BOUND_DIGESTS:
            object.__setattr__(self, name, require_digest(getattr(self, name), "fence " + name))
        object.__setattr__(self, "issued_at", require_timestamp(self.issued_at, "fence issued_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "fence expires_at"))
        if parse_timestamp(self.expires_at, "x") <= parse_timestamp(self.issued_at, "y"):
            raise SchemaError("fence expires_at must be after issued_at")

    def is_fresh(self, now: str) -> bool:
        return _fresh(self.expires_at, now, "fence expires_at")

    def supersedes(self, other: "LeaseFence") -> bool:
        """True when this fence is a strictly newer epoch for the same task."""
        return (
            self.run_id == other.run_id
            and self.task_id == other.task_id
            and self.epoch > other.epoch
        )

    def bound_digests(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in self.BOUND_DIGESTS}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "box_id": self.box_id,
            "worker_id": self.worker_id,
            "epoch": self.epoch,
            "plan_digest": self.plan_digest,
            "evaluator_digest": self.evaluator_digest,
            "workspace_digest": self.workspace_digest,
            "readiness_digest": self.readiness_digest,
            "grant_digest": self.grant_digest,
            "reservation_digest": self.reservation_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LeaseFence":
        data = require_object(payload, "lease fence")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "lease fence")
        reject_unknown_fields(data, cls.FIELDS, "lease fence")
        required = [name for name in cls.FIELDS if name not in ("schema", "schema_version")]
        missing = [name for name in required if name not in data]
        if missing:
            raise SchemaError("lease fence is missing required fields: {}".format(", ".join(missing)))
        return cls(**{name: data[name] for name in required})


# --------------------------------------------------------------------------- READY_TO_LEASE


@dataclass(frozen=True)
class ReadinessDecision:
    """Outcome of :func:`assess_ready_to_lease`: ready, or the typed reasons why not."""

    ready: bool
    reasons: Tuple[WaitingReason, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"ready": self.ready, "reasons": [reason.to_dict() for reason in self.reasons]}


def assess_ready_to_lease(
    *,
    now: str,
    run_id: str,
    task_id: str,
    box_id: str,
    worker_id: str,
    target_id: str,
    plan_digest: Optional[str],
    plan_frozen: bool,
    control_plane_ready: bool,
    dependencies_green: bool,
    workspace_digest: Optional[str],
    receipt: Optional[ReadinessReceipt],
    evaluator_digest: Optional[str],
    evaluator_ready: bool,
    grant: Optional[CapabilityGrant],
    reservation: Optional[CapacityReservation],
) -> ReadinessDecision:
    """Pure evaluation of the READY_TO_LEASE conjunction over typed inputs.

    This function is NOT called by the scheduler in M0. It exists so the
    predicate has one deterministic definition that M3 can wire in and that
    tests can exercise now. Every failed conjunct yields a typed reason; the
    caller sees all of them, not just the first.

    The expected subject is ``(run_id, task_id, box_id, worker_id, target_id)``
    plus the expected ``plan_digest``, ``workspace_digest``, and
    ``evaluator_digest``. Every supplied receipt, grant, and reservation must
    bind to exactly that subject; any cross-run or cross-subject combination is
    ``POLICY_DENIED`` and can never be ready.
    """
    reasons: List[WaitingReason] = []

    def wait(code: str, detail: str, wake: str) -> None:
        reasons.append(WaitingReason(code=code, detail=detail, wake_condition=wake, task_id=task_id, box_id=box_id))

    def bound(record: Any, label: str, expected: Sequence[Tuple[str, Any]]) -> bool:
        """Emit POLICY_DENIED for every field of ``record`` that differs from the subject."""
        consistent = True
        for name, value in expected:
            if getattr(record, name) != value:
                consistent = False
                wait(
                    "POLICY_DENIED",
                    "{} {} {!r} does not match expected {!r}".format(label, name, getattr(record, name), value),
                    "{} re-issued for this subject".format(label),
                )
        return consistent

    if not plan_frozen or plan_digest is None:
        wait("APPROVAL_REQUIRED", "PLAN_FROZEN is false: the plan digest has not been approved", "PLAN_APPROVED")
    if not control_plane_ready:
        wait("OPERATOR_ATTENTION", "CONTROL_PLANE_READY is false", "control-plane probes green")
    if not dependencies_green:
        wait("WAITING_DEPENDENCY", "TASK_DEPENDENCIES_GREEN is false", "all depends_on tasks succeeded")
    if workspace_digest is None:
        wait("WORKSPACE_CONFLICT", "no workspace receipt digest for this task", "workspace receipt recorded")

    if receipt is None:
        wait("READINESS_STALE", "BOX_READINESS_FRESH is false: no readiness receipt", "readiness receipt recorded")
    else:
        bound(
            receipt,
            "readiness receipt",
            [
                ("run_id", run_id),
                ("task_id", task_id),
                ("box_id", box_id),
                ("worker_id", worker_id),
                ("target_id", target_id),
            ],
        )
        if plan_digest is not None and receipt.plan_digest != plan_digest:
            wait("READINESS_STALE", "readiness receipt is bound to a different plan digest", "receipt re-probed")
        if workspace_digest is not None and receipt.workspace_digest != workspace_digest:
            wait("WORKSPACE_CONFLICT", "readiness receipt is bound to a different workspace digest", "receipt re-probed")
        if receipt.status != "green":
            wait("READINESS_STALE", "readiness receipt is red", "receipt re-probed green")
        elif not receipt.is_fresh(now):
            wait("READINESS_STALE", "readiness receipt or one of its probes has expired", "receipt re-probed")

    if not evaluator_ready or evaluator_digest is None:
        wait("EVALUATOR_NOT_READY", "EVALUATOR_READY is false", "evaluator bundle frozen and launchable")
    elif receipt is not None and receipt.evaluator_digest != evaluator_digest:
        wait("EVALUATOR_NOT_READY", "receipt evaluator digest does not match the frozen evaluator", "receipt re-probed")

    if grant is None:
        wait("APPROVAL_REQUIRED", "AUTHORITY_GRANTED is false: no capability grant", "capability grant issued")
    else:
        bound(grant, "capability grant", [("run_id", run_id), ("task_id", task_id), ("box_id", box_id)])
        if not grant.is_fresh(now):
            wait("APPROVAL_REQUIRED", "capability grant has expired", "capability grant renewed")

    if reservation is None:
        wait("CAPACITY_EXHAUSTED", "CAPACITY_RESERVED is false: no reservation", "capacity reservation recorded")
    else:
        bound(
            reservation,
            "capacity reservation",
            [("run_id", run_id), ("task_id", task_id), ("worker_id", worker_id), ("target_id", target_id)],
        )
        if not reservation.is_active(now):
            wait("CAPACITY_EXHAUSTED", "capacity reservation is not active", "reservation renewed")
        if receipt is not None and receipt.reservation_id != reservation.reservation_id:
            wait("READINESS_STALE", "readiness receipt names a different reservation", "receipt re-probed")

    return ReadinessDecision(ready=not reasons, reasons=tuple(reasons))
