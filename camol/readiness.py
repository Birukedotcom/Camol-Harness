"""Versioned readiness, workspace, reservation, grant, and lease-fence contracts.

Status of this module: CONTRACTS AND A PURE PREDICATE. Nothing here probes a host,
creates a worktree, talks to a provider, reserves real capacity, or launches an
agent. Read-only probing lives in :mod:`camol.probes` / :mod:`camol.doctor`; the
scheduler in :mod:`camol.orchestrator` does not yet consume these records (see
``tests/test_readiness.py`` for the characterization of that unsafe boundary).

Identity model
--------------
One lease subject is the tuple ``(run_id, task_id, box_id, worker_id, target_id)``.
:class:`BoxBinding` is the immutable record that fixes that subject together with
the plan digest and the exact workspace it owns. Every other proof record either
carries the full subject or the binding's digest, so cross-run, cross-task,
cross-box, cross-worker, cross-target and cross-workspace mixtures are rejected
structurally, not by convention.

* :class:`AuthorityPolicy` is the frozen authority a task requires. A
  :class:`CapabilityGrant` binds to it by digest and must match it exactly:
  missing authority is insufficient, extra authority is unauthorized.
* :class:`ProbePolicy` is the frozen set of probes a receipt must contain. A
  :class:`ReadinessReceipt` binds to it by digest and must cover it exactly.
* :class:`LeaseFence` binds the digests of everything above.

Temporal validity
-----------------
Every time-bound record is valid on the closed/open interval
``start <= now < expires_at``. A future-dated record is never fresh. A green
receipt requires every probe to be green, observed no later than the receipt,
and to expire no earlier than the receipt.

Every contract carries an explicit ``schema`` and integer ``schema_version``,
round-trips through ``to_dict``/``from_dict`` deterministically, rejects unknown
fields and versions with :class:`camol.schema.SchemaError`, stores collections as
tuples (deep immutability), and hashes through
:func:`camol.schema.canonical_digest`.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import (
    SchemaError,
    canonical_digest,
    parse_timestamp,
    reject_unknown_fields,
    require_bool,
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
    "PROBE_METHODS",
    "CLOCK_SOURCES",
    "RECEIPT_STATUSES",
    "RESERVATION_STATUSES",
    "TRUST_TIERS",
    "FILESYSTEM_POLICIES",
    "CAPABILITY_KINDS",
    "CLEANUP_OWNERS",
    "SUBJECT_FIELDS",
    "BoxBinding",
    "AuthorityPolicy",
    "ProbeRequirement",
    "ProbePolicy",
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
# How a probe observed: a subprocess (argv recorded), the filesystem, a socket
# connect/close, or in-process introspection of the control plane itself.
PROBE_METHODS = frozenset({"process", "filesystem", "socket", "in_process"})
# Where a receipt's observation instant came from. Only ``system`` evidence can
# ever satisfy READY_TO_LEASE; ``synthetic`` (an injected --now) is for fixtures.
CLOCK_SOURCES = frozenset({"system", "synthetic"})
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
SUBJECT_FIELDS = ("run_id", "task_id", "box_id", "worker_id", "target_id")


def _tuple(value: Any, label: str) -> Tuple[str, ...]:
    """Validate a string collection and freeze it as a sorted tuple."""
    if isinstance(value, tuple):
        value = list(value)
    return tuple(require_string_list(value, label, sort=True))


def _argv(value: Any, label: str) -> Tuple[str, ...]:
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SchemaError("{} must be an argv array of strings".format(label))
    return tuple(value)


def _interval_fresh(start: str, expires_at: Optional[str], now: str) -> bool:
    """``start <= now < expires_at``. A missing expiry or a future start is never fresh."""
    if expires_at is None:
        return False
    instant = parse_timestamp(now, "now")
    return parse_timestamp(start, "start") <= instant < parse_timestamp(expires_at, "expires_at")


def _require_after(start: str, end: str, label: str) -> None:
    if parse_timestamp(end, "end") <= parse_timestamp(start, "start"):
        raise SchemaError(label)


def _identifiers(record: Any, names: Sequence[str], label: str) -> None:
    for name in names:
        object.__setattr__(record, name, require_identifier(getattr(record, name), "{} {}".format(label, name)))


def _required(cls: Any, data: Mapping[str, Any], optional: Sequence[str], label: str) -> List[str]:
    required = [name for name in cls.FIELDS if name not in ("schema", "schema_version") and name not in optional]
    missing = [name for name in required if name not in data]
    if missing:
        raise SchemaError("{} is missing required fields: {}".format(label, ", ".join(missing)))
    return required


def _header(cls: Any, payload: Mapping[str, Any], label: str) -> Dict[str, Any]:
    data = require_object(payload, label)
    require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, label)
    reject_unknown_fields(data, cls.FIELDS, label)
    return data


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
        data = _header(cls, payload, "waiting reason")
        return cls(
            code=data.get("code"),
            detail=data.get("detail"),
            wake_condition=data.get("wake_condition"),
            task_id=data.get("task_id"),
            box_id=data.get("box_id"),
        )


# --------------------------------------------------------------------------- BoxBinding


@dataclass(frozen=True)
class BoxBinding:
    """Immutable statement of which run/task/box/worker/target owns which workspace.

    The binding digest is what every other proof record refers to when it claims
    to be "about this lease subject". ``workspace_digest`` is the digest of the
    :class:`WorkspaceReceipt` the box will work in.
    """

    SCHEMA = "camol.box_binding"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "run_id",
        "task_id",
        "box_id",
        "worker_id",
        "target_id",
        "plan_digest",
        "workspace_id",
        "workspace_digest",
        "bound_at",
    )

    run_id: str
    task_id: str
    box_id: str
    worker_id: str
    target_id: str
    plan_digest: str
    workspace_id: str
    workspace_digest: str
    bound_at: str

    def __post_init__(self) -> None:
        _identifiers(self, SUBJECT_FIELDS + ("workspace_id",), "box binding")
        object.__setattr__(self, "plan_digest", require_digest(self.plan_digest, "box binding plan_digest"))
        object.__setattr__(self, "workspace_digest", require_digest(self.workspace_digest, "box binding workspace_digest"))
        object.__setattr__(self, "bound_at", require_timestamp(self.bound_at, "box binding bound_at"))

    def subject(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in SUBJECT_FIELDS}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "box_id": self.box_id,
            "worker_id": self.worker_id,
            "target_id": self.target_id,
            "plan_digest": self.plan_digest,
            "workspace_id": self.workspace_id,
            "workspace_digest": self.workspace_digest,
            "bound_at": self.bound_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BoxBinding":
        data = _header(cls, payload, "box binding")
        required = _required(cls, data, (), "box binding")
        return cls(**{name: data[name] for name in required})


# --------------------------------------------------------------------------- AuthorityPolicy


def _validate_authority_shape(capabilities: Tuple[str, ...], network: Tuple[str, ...], credentials: Tuple[str, ...], label: str) -> None:
    unknown = sorted(set(capabilities) - CAPABILITY_KINDS)
    if unknown:
        raise SchemaError("{} capabilities contain unknown kinds: {}".format(label, ", ".join(unknown)))
    if credentials and "credential" not in capabilities:
        raise SchemaError("{} credential_refs require the credential capability".format(label))
    if network and "network" not in capabilities:
        raise SchemaError("{} network_destinations require the network capability".format(label))


@dataclass(frozen=True)
class AuthorityPolicy:
    """The frozen authority a task requires. A grant must match it exactly.

    ``required_capabilities`` must be non-empty: a task that runs anything needs
    at least one capability, and an empty policy would make an empty grant
    "sufficient", which is the hole this contract exists to close. A grant is
    never allowed to exceed the policy that authorized it.
    """

    SCHEMA = "camol.authority_policy"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "run_id",
        "task_id",
        "required_capabilities",
        "filesystem_paths",
        "network_destinations",
        "credential_refs",
        "trust_tier",
    )
    CONSTRAINT_FIELDS = ("filesystem_paths", "network_destinations", "credential_refs", "trust_tier")

    run_id: str
    task_id: str
    required_capabilities: Tuple[str, ...]
    trust_tier: str
    filesystem_paths: Tuple[str, ...] = ()
    network_destinations: Tuple[str, ...] = ()
    credential_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifiers(self, ("run_id", "task_id"), "authority policy")
        object.__setattr__(self, "required_capabilities", _tuple(self.required_capabilities, "authority required_capabilities"))
        object.__setattr__(self, "filesystem_paths", _tuple(self.filesystem_paths, "authority filesystem_paths"))
        object.__setattr__(self, "network_destinations", _tuple(self.network_destinations, "authority network_destinations"))
        object.__setattr__(self, "credential_refs", _tuple(self.credential_refs, "authority credential_refs"))
        if not self.required_capabilities:
            raise SchemaError("authority policy required_capabilities must not be empty")
        _validate_authority_shape(self.required_capabilities, self.network_destinations, self.credential_refs, "authority policy")
        object.__setattr__(self, "trust_tier", require_choice(self.trust_tier, "authority trust_tier", TRUST_TIERS))

    def grant_mismatches(self, grant: "CapabilityGrant") -> List[Tuple[str, str]]:
        """Return ``(kind, detail)`` pairs; empty means the grant equals this policy.

        ``kind`` is ``insufficient`` (grant lacks required authority) or
        ``unauthorized`` (grant carries authority the policy did not freeze).
        """
        problems: List[Tuple[str, str]] = []
        missing = sorted(set(self.required_capabilities) - set(grant.capabilities))
        extra = sorted(set(grant.capabilities) - set(self.required_capabilities))
        if missing:
            problems.append(("insufficient", "grant lacks required capabilities: {}".format(", ".join(missing))))
        if extra:
            problems.append(("unauthorized", "grant carries capabilities the policy did not authorize: {}".format(", ".join(extra))))
        for name in self.CONSTRAINT_FIELDS:
            expected = getattr(self, name)
            actual = getattr(grant, name)
            if expected != actual:
                problems.append(("unauthorized", "grant {} {!r} differs from policy {!r}".format(name, actual, expected)))
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "required_capabilities": list(self.required_capabilities),
            "filesystem_paths": list(self.filesystem_paths),
            "network_destinations": list(self.network_destinations),
            "credential_refs": list(self.credential_refs),
            "trust_tier": self.trust_tier,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorityPolicy":
        data = _header(cls, payload, "authority policy")
        optional = ("filesystem_paths", "network_destinations", "credential_refs")
        required = _required(cls, data, optional, "authority policy")
        return cls(**{name: data[name] for name in required}, **{name: data.get(name, []) for name in optional})


# --------------------------------------------------------------------------- ProbePolicy


@dataclass(frozen=True)
class ProbeRequirement:
    """One probe a receipt must contain.

    ``definition_digest`` pins the exact probe implementation, version, and
    configuration that must have produced the result; a probe with the same id
    but a different definition does not satisfy the policy. ``target_bound``
    probes must name the receipt's target.
    """

    SCHEMA = "camol.probe_requirement"
    SCHEMA_VERSION = 1
    FIELDS = ("schema", "schema_version", "probe_id", "kind", "target_bound", "definition_digest")

    probe_id: str
    kind: str
    target_bound: bool
    definition_digest: str

    def __post_init__(self) -> None:
        _identifiers(self, ("probe_id", "kind"), "probe requirement")
        require_bool(self.target_bound, "probe requirement target_bound")
        object.__setattr__(self, "definition_digest", require_digest(self.definition_digest, "probe requirement definition_digest"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "probe_id": self.probe_id,
            "kind": self.kind,
            "target_bound": self.target_bound,
            "definition_digest": self.definition_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProbeRequirement":
        data = _header(cls, payload, "probe requirement")
        required = _required(cls, data, (), "probe requirement")
        return cls(**{name: data[name] for name in required})


@dataclass(frozen=True)
class ProbePolicy:
    """The frozen, non-empty set of probes a readiness receipt must contain exactly."""

    SCHEMA = "camol.probe_policy"
    SCHEMA_VERSION = 1
    FIELDS = ("schema", "schema_version", "run_id", "task_id", "required_probes")

    run_id: str
    task_id: str
    required_probes: Tuple[ProbeRequirement, ...]

    def __post_init__(self) -> None:
        _identifiers(self, ("run_id", "task_id"), "probe policy")
        probes = list(self.required_probes) if isinstance(self.required_probes, (list, tuple)) else None
        if not probes:
            raise SchemaError("probe policy required_probes must be a non-empty list of ProbeRequirement")
        if any(not isinstance(item, ProbeRequirement) for item in probes):
            raise SchemaError("probe policy required_probes must be ProbeRequirement instances")
        ids = [item.probe_id for item in probes]
        if len(set(ids)) != len(ids):
            raise SchemaError("probe policy probe ids must be unique")
        object.__setattr__(self, "required_probes", tuple(sorted(probes, key=lambda item: item.probe_id)))

    def coverage_errors(self, receipt: "ReadinessReceipt") -> List[Tuple[str, str]]:
        """``(reason_code, detail)`` for every way ``receipt`` fails to contain exactly these probes.

        Missing, mis-kinded, or unbound required probes are ``READINESS_STALE``
        (re-probing fixes them). A probe the policy never listed is
        ``POLICY_DENIED``: unlisted proof is not proof.
        """
        errors: List[Tuple[str, str]] = []
        by_id = {probe.probe_id: probe for probe in receipt.probes}
        for requirement in self.required_probes:
            probe = by_id.get(requirement.probe_id)
            if probe is None:
                errors.append(("READINESS_STALE", "required probe {} is missing".format(requirement.probe_id)))
                continue
            if probe.kind != requirement.kind:
                errors.append(("READINESS_STALE", "probe {} has kind {!r}, policy requires {!r}".format(requirement.probe_id, probe.kind, requirement.kind)))
            if probe.definition_digest != requirement.definition_digest:
                errors.append(("POLICY_DENIED", "probe {} was produced by a different probe definition than the policy froze".format(requirement.probe_id)))
            if requirement.target_bound and probe.target_id != receipt.target_id:
                errors.append(("READINESS_STALE", "probe {} must be bound to target {!r}, found {!r}".format(requirement.probe_id, receipt.target_id, probe.target_id)))
        required_ids = {item.probe_id for item in self.required_probes}
        for extra in sorted(set(by_id) - required_ids):
            errors.append(("POLICY_DENIED", "probe {} is not part of the frozen probe policy".format(extra)))
        return errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "required_probes": [item.to_dict() for item in self.required_probes],
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProbePolicy":
        data = _header(cls, payload, "probe policy")
        _required(cls, data, (), "probe policy")
        raw = data["required_probes"]
        if not isinstance(raw, list):
            raise SchemaError("probe policy required_probes must be a list")
        return cls(
            run_id=data["run_id"],
            task_id=data["task_id"],
            required_probes=tuple(ProbeRequirement.from_dict(item) for item in raw),
        )


# --------------------------------------------------------------------------- ProbeResult


@dataclass(frozen=True)
class ProbeResult:
    """One read-only observation about one readiness dimension.

    ``method`` says how the observation was made (``PROBE_METHODS``); ``command``
    is the redacted argv and is non-empty exactly when ``method == "process"``.
    ``summary`` and ``missing_requirements`` must already be redacted by the
    producer; the contract carries no secret-bearing field. A non-green probe
    must carry a typed ``reason_code`` (one of ``NON_RUNNABLE_REASONS``), a
    ``wake_condition`` naming what would clear it, and at least one exact
    ``missing_requirements`` entry; a green probe carries none of these.
    ``definition_digest`` identifies the exact probe implementation, version, and
    configuration that produced the result. Valid on
    ``observed_at <= now < expires_at``.
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
        "method",
        "command",
        "tool_version",
        "summary",
        "missing_requirements",
        "evidence_digest",
        "reason_code",
        "wake_condition",
        "definition_digest",
    )

    probe_id: str
    kind: str
    status: str
    observed_at: str
    summary: str
    definition_digest: str
    target_id: Optional[str] = None
    expires_at: Optional[str] = None
    method: str = "in_process"
    command: Tuple[str, ...] = ()
    tool_version: Optional[str] = None
    missing_requirements: Tuple[str, ...] = ()
    evidence_digest: Optional[str] = None
    reason_code: Optional[str] = None
    wake_condition: Optional[str] = None

    def __post_init__(self) -> None:
        _identifiers(self, ("probe_id", "kind"), "probe")
        object.__setattr__(self, "definition_digest", require_digest(self.definition_digest, "probe definition_digest"))
        if self.target_id is not None:
            object.__setattr__(self, "target_id", require_identifier(self.target_id, "probe target_id"))
        object.__setattr__(self, "status", require_choice(self.status, "probe status", PROBE_STATUSES))
        object.__setattr__(self, "observed_at", require_timestamp(self.observed_at, "probe observed_at"))
        object.__setattr__(self, "expires_at", require_optional_timestamp(self.expires_at, "probe expires_at"))
        if self.expires_at is not None:
            _require_after(self.observed_at, self.expires_at, "probe expires_at must be after observed_at")
        object.__setattr__(self, "method", require_choice(self.method, "probe method", PROBE_METHODS))
        object.__setattr__(self, "command", _argv(self.command, "probe command"))
        if self.method == "process" and not self.command:
            raise SchemaError("a process probe must record its argv")
        if self.method != "process" and self.command:
            raise SchemaError("a {} probe cannot record an argv".format(self.method))
        object.__setattr__(self, "tool_version", require_optional_string(self.tool_version, "probe tool_version"))
        object.__setattr__(self, "summary", require_string(self.summary, "probe summary"))
        object.__setattr__(self, "missing_requirements", _tuple(self.missing_requirements, "probe missing_requirements"))
        object.__setattr__(self, "evidence_digest", require_optional_digest(self.evidence_digest, "probe evidence_digest"))
        object.__setattr__(self, "wake_condition", require_optional_string(self.wake_condition, "probe wake_condition"))
        if self.reason_code is not None and self.reason_code not in NON_RUNNABLE_REASONS:
            raise SchemaError("probe reason_code must be one of: {}".format(", ".join(sorted(NON_RUNNABLE_REASONS))))
        if self.status == "green":
            if self.missing_requirements:
                raise SchemaError("a green probe cannot list missing requirements")
            if self.expires_at is None:
                raise SchemaError("a green probe must declare expires_at; proof without expiry is not proof")
            if self.reason_code is not None or self.wake_condition is not None:
                raise SchemaError("a green probe cannot carry a reason_code or wake_condition")
        else:
            if self.reason_code is None:
                raise SchemaError("a {} probe must carry a typed reason_code".format(self.status))
            if self.wake_condition is None:
                raise SchemaError("a {} probe must state its wake_condition".format(self.status))
            if not self.missing_requirements:
                raise SchemaError("a {} probe must name at least one missing requirement".format(self.status))

    def is_fresh(self, now: str) -> bool:
        return self.status == "green" and _interval_fresh(self.observed_at, self.expires_at, now)

    def waiting_reason(self) -> Optional[WaitingReason]:
        if self.status == "green":
            return None
        detail = self.summary
        if self.missing_requirements:
            detail = "{} (missing: {})".format(self.summary, ", ".join(self.missing_requirements))
        return WaitingReason(code=self.reason_code, detail=detail, wake_condition=self.wake_condition)

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
            "method": self.method,
            "command": list(self.command),
            "tool_version": self.tool_version,
            "summary": self.summary,
            "missing_requirements": list(self.missing_requirements),
            "evidence_digest": self.evidence_digest,
            "reason_code": self.reason_code,
            "wake_condition": self.wake_condition,
            "definition_digest": self.definition_digest,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProbeResult":
        data = _header(cls, payload, "probe result")
        for required in ("probe_id", "kind", "status", "observed_at", "summary", "definition_digest"):
            if required not in data:
                raise SchemaError("probe result is missing required field {}".format(required))
        return cls(
            probe_id=data["probe_id"],
            kind=data["kind"],
            target_id=data.get("target_id"),
            status=data["status"],
            observed_at=data["observed_at"],
            expires_at=data.get("expires_at"),
            method=data.get("method", "in_process"),
            command=data.get("command", []),
            tool_version=data.get("tool_version"),
            summary=data["summary"],
            missing_requirements=data.get("missing_requirements", []),
            evidence_digest=data.get("evidence_digest"),
            reason_code=data.get("reason_code"),
            wake_condition=data.get("wake_condition"),
            definition_digest=data["definition_digest"],
        )


# --------------------------------------------------------------------------- WorkspaceReceipt


@dataclass(frozen=True)
class WorkspaceReceipt:
    """Identity of a workspace a box may work in.

    Ownership (which run/task/box this workspace belongs to) is stated by the
    :class:`BoxBinding` that names this receipt's ``workspace_id`` and digest;
    the receipt itself describes only the checkout. ``path`` is recorded as a
    string and is not resolved, created, or checked here. ``dirty_digest`` is the
    canonical digest of the workspace's uncommitted state; the digest of an empty
    list is the clean value. ``filesystem_policy`` describes source access (see
    ``FILESYSTEM_POLICIES``); the worker's trust tier lives on the grant.
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
        data = _header(cls, payload, "workspace receipt")
        required = _required(cls, data, (), "workspace receipt")
        return cls(**{name: data[name] for name in required})


# --------------------------------------------------------------------------- CapacityReservation


@dataclass(frozen=True)
class CapacityReservation:
    """A bounded, expiring claim on concurrency, tokens, and optional spend for one subject."""

    SCHEMA = "camol.capacity_reservation"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema",
        "schema_version",
        "reservation_id",
        "run_id",
        "task_id",
        "box_id",
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
    box_id: str
    worker_id: str
    target_id: str
    concurrency_slots: int
    max_tokens: int
    status: str
    reserved_at: str
    expires_at: str
    max_usd_cents: Optional[int] = None

    def __post_init__(self) -> None:
        _identifiers(self, ("reservation_id",) + SUBJECT_FIELDS, "reservation")
        object.__setattr__(self, "concurrency_slots", require_positive_int(self.concurrency_slots, "concurrency_slots"))
        object.__setattr__(self, "max_tokens", require_positive_int(self.max_tokens, "max_tokens"))
        if self.max_usd_cents is not None:
            object.__setattr__(self, "max_usd_cents", require_non_negative_int(self.max_usd_cents, "max_usd_cents"))
        object.__setattr__(self, "status", require_choice(self.status, "reservation status", RESERVATION_STATUSES))
        object.__setattr__(self, "reserved_at", require_timestamp(self.reserved_at, "reserved_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "reservation expires_at"))
        _require_after(self.reserved_at, self.expires_at, "reservation expires_at must be after reserved_at")

    def is_active(self, now: str) -> bool:
        return self.status == "reserved" and _interval_fresh(self.reserved_at, self.expires_at, now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "reservation_id": self.reservation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "box_id": self.box_id,
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
        data = _header(cls, payload, "capacity reservation")
        required = _required(cls, data, ("max_usd_cents",), "capacity reservation")
        return cls(**{name: data[name] for name in required}, max_usd_cents=data.get("max_usd_cents"))


# --------------------------------------------------------------------------- CapabilityGrant


@dataclass(frozen=True)
class CapabilityGrant:
    """Deny-by-default authority attached to one subject, bound to an AuthorityPolicy.

    ``authority_digest`` is the digest of the :class:`AuthorityPolicy` that
    authorized this grant; :func:`assess_ready_to_lease` requires the grant's
    content to equal that policy exactly. ``credential_refs`` are opaque
    reference names resolved only inside an authorized adapter; the grant never
    carries a secret value. Valid on ``granted_at <= now < expires_at``.
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
        "worker_id",
        "target_id",
        "authority_digest",
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
    worker_id: str
    target_id: str
    authority_digest: str
    capabilities: Tuple[str, ...]
    trust_tier: str
    granted_by: str
    granted_at: str
    expires_at: str
    filesystem_paths: Tuple[str, ...] = ()
    network_destinations: Tuple[str, ...] = ()
    credential_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifiers(self, ("grant_id",) + SUBJECT_FIELDS, "grant")
        object.__setattr__(self, "authority_digest", require_digest(self.authority_digest, "grant authority_digest"))
        object.__setattr__(self, "capabilities", _tuple(self.capabilities, "grant capabilities"))
        object.__setattr__(self, "filesystem_paths", _tuple(self.filesystem_paths, "filesystem_paths"))
        object.__setattr__(self, "network_destinations", _tuple(self.network_destinations, "network_destinations"))
        object.__setattr__(self, "credential_refs", _tuple(self.credential_refs, "credential_refs"))
        _validate_authority_shape(self.capabilities, self.network_destinations, self.credential_refs, "grant")
        object.__setattr__(self, "trust_tier", require_choice(self.trust_tier, "trust_tier", TRUST_TIERS))
        object.__setattr__(self, "granted_by", require_string(self.granted_by, "granted_by"))
        object.__setattr__(self, "granted_at", require_timestamp(self.granted_at, "granted_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "grant expires_at"))
        _require_after(self.granted_at, self.expires_at, "grant expires_at must be after granted_at")

    def is_fresh(self, now: str) -> bool:
        return _interval_fresh(self.granted_at, self.expires_at, now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "grant_id": self.grant_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "box_id": self.box_id,
            "worker_id": self.worker_id,
            "target_id": self.target_id,
            "authority_digest": self.authority_digest,
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
        data = _header(cls, payload, "capability grant")
        optional = ("filesystem_paths", "network_destinations", "credential_refs")
        required = _required(cls, data, optional, "capability grant")
        return cls(**{name: data[name] for name in required}, **{name: data.get(name, []) for name in optional})


# --------------------------------------------------------------------------- ReadinessReceipt


@dataclass(frozen=True)
class ReadinessReceipt:
    """Proof that one subject was ready at ``observed_at``.

    The receipt carries the full subject, the digests of the :class:`BoxBinding`,
    :class:`AuthorityPolicy`, and :class:`ProbePolicy` it was produced under,
    and the probes themselves. ``requested_model`` is an opaque profile string
    (vendor-neutral); ``credential_scopes`` are scope fingerprints, never secrets.
    ``reservation_id`` is ``None`` for a pre-reservation receipt (as emitted by
    ``camol doctor``); the lease predicate requires it to name the active
    reservation before anything may be leased. ``clock`` records whether
    ``observed_at`` came from the system clock or an injected synthetic instant;
    synthetic receipts are fixtures, never evidence, and the predicate rejects them.

    Construction-time coherence for a green receipt: every probe is green, has
    an expiry no earlier than the receipt's, was observed no later than the
    receipt, and any target-bound probe names the receipt's target. Runtime
    freshness (``is_fresh``) is ``observed_at <= now < expires_at`` and every
    probe fresh.
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
        "box_binding_digest",
        "workspace_digest",
        "evaluator_digest",
        "authority_digest",
        "probe_policy_digest",
        "adapter_kind",
        "requested_model",
        "credential_scopes",
        "reservation_id",
        "probes",
        "status",
        "observed_at",
        "expires_at",
        "clock",
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
        "box_binding_digest",
        "workspace_digest",
        "evaluator_digest",
        "authority_digest",
        "probe_policy_digest",
        "adapter_kind",
        "requested_model",
        "reservation_id",
        "clock",
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
    box_binding_digest: str
    workspace_digest: str
    evaluator_digest: str
    authority_digest: str
    probe_policy_digest: str
    adapter_kind: str
    requested_model: Optional[str]
    reservation_id: Optional[str]
    probes: Tuple[ProbeResult, ...]
    status: str
    observed_at: str
    expires_at: str
    clock: str = "system"
    credential_scopes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifiers(self, ("receipt_id",) + SUBJECT_FIELDS + ("transport_id", "runtime_id", "adapter_kind"), "receipt")
        object.__setattr__(self, "clock", require_choice(self.clock, "receipt clock", CLOCK_SOURCES))
        if self.reservation_id is not None:
            object.__setattr__(self, "reservation_id", require_identifier(self.reservation_id, "receipt reservation_id"))
        for name in ("plan_digest", "box_binding_digest", "workspace_digest", "evaluator_digest", "authority_digest", "probe_policy_digest"):
            object.__setattr__(self, name, require_digest(getattr(self, name), "receipt " + name))
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
        _require_after(self.observed_at, self.expires_at, "receipt expires_at must be after observed_at")
        observed = parse_timestamp(self.observed_at, "observed_at")
        expires = parse_timestamp(self.expires_at, "expires_at")
        for probe in self.probes:
            if probe.target_id is not None and probe.target_id != self.target_id:
                raise SchemaError(
                    "probe {} is bound to target {!r}, receipt target is {!r}".format(probe.probe_id, probe.target_id, self.target_id)
                )
            if parse_timestamp(probe.observed_at, "probe observed_at") > observed:
                raise SchemaError(
                    "probe {} was observed at {} after the receipt observation {}".format(
                        probe.probe_id, probe.observed_at, self.observed_at
                    )
                )
        if self.status == "red" and all(probe.status == "green" for probe in self.probes):
            raise SchemaError("a red receipt must contain at least one non-green probe")
        if self.status == "green":
            for probe in self.probes:
                if probe.status != "green":
                    raise SchemaError("a green receipt cannot contain a non-green probe ({})".format(probe.probe_id))
                if probe.expires_at is None:
                    raise SchemaError("a green receipt requires probe {} to declare expires_at".format(probe.probe_id))
                if expires > parse_timestamp(probe.expires_at, "probe expires_at"):
                    raise SchemaError(
                        "receipt expires_at {} is later than probe {} expires_at {}".format(
                            self.expires_at, probe.probe_id, probe.expires_at
                        )
                    )

    def subject(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in SUBJECT_FIELDS}

    def binding(self) -> Dict[str, Any]:
        """The identity/digest fields a lease fence must match exactly."""
        return {name: getattr(self, name) for name in self.BINDING_FIELDS}

    def binding_digest(self) -> str:
        return canonical_digest(self.binding())

    def is_fresh(self, now: str) -> bool:
        """Green, inside its own validity interval, and every probe fresh."""
        if self.status != "green" or not _interval_fresh(self.observed_at, self.expires_at, now):
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
            "box_binding_digest": self.box_binding_digest,
            "workspace_digest": self.workspace_digest,
            "evaluator_digest": self.evaluator_digest,
            "authority_digest": self.authority_digest,
            "probe_policy_digest": self.probe_policy_digest,
            "adapter_kind": self.adapter_kind,
            "requested_model": self.requested_model,
            "credential_scopes": list(self.credential_scopes),
            "reservation_id": self.reservation_id,
            "probes": [probe.to_dict() for probe in self.probes],
            "status": self.status,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "clock": self.clock,
        }

    def is_evidence(self) -> bool:
        """Only system-clock receipts are evidence; synthetic receipts are fixtures."""
        return self.clock == "system"

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReadinessReceipt":
        data = _header(cls, payload, "readiness receipt")
        required = _required(cls, data, ("credential_scopes",), "readiness receipt")
        probes_raw = data["probes"]
        if not isinstance(probes_raw, list):
            raise SchemaError("readiness receipt probes must be a list")
        probes = tuple(ProbeResult.from_dict(item) for item in probes_raw)
        values = {name: data[name] for name in required if name != "probes"}
        return cls(probes=probes, credential_scopes=data.get("credential_scopes", []), **values)


# --------------------------------------------------------------------------- LeaseFence


@dataclass(frozen=True)
class LeaseFence:
    """Monotonic fencing token binding a lease to every input it was proven against.

    Valid on ``issued_at <= now < expires_at``.
    """

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
        "target_id",
        "epoch",
        "plan_digest",
        "box_binding_digest",
        "evaluator_digest",
        "workspace_digest",
        "authority_digest",
        "probe_policy_digest",
        "readiness_digest",
        "grant_digest",
        "reservation_digest",
        "issued_at",
        "expires_at",
    )
    BOUND_DIGESTS = (
        "plan_digest",
        "box_binding_digest",
        "evaluator_digest",
        "workspace_digest",
        "authority_digest",
        "probe_policy_digest",
        "readiness_digest",
        "grant_digest",
        "reservation_digest",
    )

    lease_id: str
    run_id: str
    task_id: str
    box_id: str
    worker_id: str
    target_id: str
    epoch: int
    plan_digest: str
    box_binding_digest: str
    evaluator_digest: str
    workspace_digest: str
    authority_digest: str
    probe_policy_digest: str
    readiness_digest: str
    grant_digest: str
    reservation_digest: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _identifiers(self, ("lease_id",) + SUBJECT_FIELDS, "fence")
        object.__setattr__(self, "epoch", require_positive_int(self.epoch, "fence epoch"))
        for name in self.BOUND_DIGESTS:
            object.__setattr__(self, name, require_digest(getattr(self, name), "fence " + name))
        object.__setattr__(self, "issued_at", require_timestamp(self.issued_at, "fence issued_at"))
        object.__setattr__(self, "expires_at", require_timestamp(self.expires_at, "fence expires_at"))
        _require_after(self.issued_at, self.expires_at, "fence expires_at must be after issued_at")

    def is_fresh(self, now: str) -> bool:
        return _interval_fresh(self.issued_at, self.expires_at, now)

    def supersedes(self, other: "LeaseFence") -> bool:
        """True when this fence is a strictly newer epoch for the same run and task."""
        return self.run_id == other.run_id and self.task_id == other.task_id and self.epoch > other.epoch

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
            "target_id": self.target_id,
            "epoch": self.epoch,
            "plan_digest": self.plan_digest,
            "box_binding_digest": self.box_binding_digest,
            "evaluator_digest": self.evaluator_digest,
            "workspace_digest": self.workspace_digest,
            "authority_digest": self.authority_digest,
            "probe_policy_digest": self.probe_policy_digest,
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
        data = _header(cls, payload, "lease fence")
        required = _required(cls, data, (), "lease fence")
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
    binding: BoxBinding,
    plan_digest: Optional[str],
    plan_frozen: bool,
    control_plane_ready: bool,
    dependencies_green: bool,
    workspace: Optional[WorkspaceReceipt],
    receipt: Optional[ReadinessReceipt],
    evaluator_digest: Optional[str],
    evaluator_ready: bool,
    authority_policy: Optional[AuthorityPolicy],
    grant: Optional[CapabilityGrant],
    probe_policy: Optional[ProbePolicy],
    reservation: Optional[CapacityReservation],
) -> ReadinessDecision:
    """Pure evaluation of the READY_TO_LEASE conjunction over typed inputs.

    This function is NOT called by the scheduler yet (M3). It exists so the
    predicate has one deterministic definition. Every failed conjunct yields a
    typed reason; the caller sees all of them, not just the first.

    ``binding`` fixes the subject and the workspace. Every other record must
    bind to exactly that subject and to the frozen plan, workspace, evaluator,
    authority policy, probe policy, and reservation; any mixture is
    ``POLICY_DENIED`` and can never be ready.
    """
    reasons: List[WaitingReason] = []
    subject = binding.subject()

    def wait(code: str, detail: str, wake: str) -> None:
        reasons.append(
            WaitingReason(code=code, detail=detail, wake_condition=wake, task_id=binding.task_id, box_id=binding.box_id)
        )

    def bound(record: Any, label: str, expected: Sequence[Tuple[str, Any]]) -> None:
        for name, value in expected:
            actual = getattr(record, name)
            if actual != value:
                wait(
                    "POLICY_DENIED",
                    "{} {} {!r} does not match expected {!r}".format(label, name, actual, value),
                    "{} re-issued for this subject".format(label),
                )

    subject_pairs = list(subject.items())

    # PLAN_FROZEN
    if not plan_frozen or plan_digest is None:
        wait("APPROVAL_REQUIRED", "PLAN_FROZEN is false: the plan digest has not been approved", "PLAN_APPROVED")
    elif binding.plan_digest != plan_digest:
        wait("POLICY_DENIED", "box binding is bound to plan {!r}, expected {!r}".format(binding.plan_digest, plan_digest), "box re-bound to the frozen plan")

    # CONTROL_PLANE_READY / TASK_DEPENDENCIES_GREEN
    if not control_plane_ready:
        wait("OPERATOR_ATTENTION", "CONTROL_PLANE_READY is false", "control-plane probes green")
    if not dependencies_green:
        wait("WAITING_DEPENDENCY", "TASK_DEPENDENCIES_GREEN is false", "all depends_on tasks succeeded")

    # Workspace ownership
    if workspace is None:
        wait("WORKSPACE_CONFLICT", "no workspace receipt for this box binding", "workspace receipt recorded")
    else:
        if workspace.workspace_id != binding.workspace_id:
            wait("WORKSPACE_CONFLICT", "workspace receipt {!r} is not the bound workspace {!r}".format(workspace.workspace_id, binding.workspace_id), "workspace re-bound")
        if workspace.digest() != binding.workspace_digest:
            wait("WORKSPACE_CONFLICT", "workspace receipt digest does not match the box binding", "workspace re-bound")

    # BOX_READINESS_FRESH
    if receipt is None:
        wait("READINESS_STALE", "BOX_READINESS_FRESH is false: no readiness receipt", "readiness receipt recorded")
    else:
        bound(receipt, "readiness receipt", subject_pairs)
        if receipt.box_binding_digest != binding.digest():
            wait("POLICY_DENIED", "readiness receipt is bound to a different box binding", "receipt re-probed for this binding")
        if plan_digest is not None and receipt.plan_digest != plan_digest:
            wait("READINESS_STALE", "readiness receipt is bound to a different plan digest", "receipt re-probed")
        if receipt.workspace_digest != binding.workspace_digest:
            wait("WORKSPACE_CONFLICT", "readiness receipt is bound to a different workspace digest", "receipt re-probed")
        if not receipt.is_evidence():
            wait("POLICY_DENIED", "readiness receipt was produced with a synthetic clock and is not evidence", "receipt re-probed with the system clock")
        if receipt.status != "green":
            wait("READINESS_STALE", "readiness receipt is red", "receipt re-probed green")
        elif not receipt.is_fresh(now):
            wait("READINESS_STALE", "readiness receipt is outside its validity interval or a probe is stale", "receipt re-probed")

    # Probe policy coverage
    if probe_policy is None:
        wait("READINESS_STALE", "no frozen probe policy for this task", "probe policy frozen with the plan")
    else:
        bound(probe_policy, "probe policy", [("run_id", binding.run_id), ("task_id", binding.task_id)])
        if receipt is not None:
            if receipt.probe_policy_digest != probe_policy.digest():
                wait("POLICY_DENIED", "readiness receipt was produced under a different probe policy", "receipt re-probed under the frozen probe policy")
            for code, error in probe_policy.coverage_errors(receipt):
                wait(code, "probe policy not satisfied: {}".format(error), "receipt re-probed under the frozen probe policy")

    # EVALUATOR_READY
    if not evaluator_ready or evaluator_digest is None:
        wait("EVALUATOR_NOT_READY", "EVALUATOR_READY is false", "evaluator bundle frozen and launchable")
    elif receipt is not None and receipt.evaluator_digest != evaluator_digest:
        wait("EVALUATOR_NOT_READY", "receipt evaluator digest does not match the frozen evaluator", "receipt re-probed")

    # AUTHORITY_GRANTED
    if authority_policy is None:
        wait("APPROVAL_REQUIRED", "AUTHORITY_GRANTED is false: no frozen authority policy", "authority policy frozen with the plan")
    else:
        bound(authority_policy, "authority policy", [("run_id", binding.run_id), ("task_id", binding.task_id)])
        if receipt is not None and receipt.authority_digest != authority_policy.digest():
            wait("POLICY_DENIED", "readiness receipt was produced under a different authority policy", "receipt re-probed under the frozen authority policy")
    if grant is None:
        wait("APPROVAL_REQUIRED", "AUTHORITY_GRANTED is false: no capability grant", "capability grant issued")
    else:
        bound(grant, "capability grant", subject_pairs)
        if authority_policy is not None:
            if grant.authority_digest != authority_policy.digest():
                wait("POLICY_DENIED", "capability grant is bound to a different authority policy", "grant re-issued under the frozen authority policy")
            for kind, detail in authority_policy.grant_mismatches(grant):
                code = "APPROVAL_REQUIRED" if kind == "insufficient" else "POLICY_DENIED"
                wait(code, detail, "grant re-issued to equal the frozen authority policy")
        if not grant.is_fresh(now):
            wait("APPROVAL_REQUIRED", "capability grant is outside its validity interval", "capability grant renewed")

    # CAPACITY_RESERVED
    if reservation is None:
        wait("CAPACITY_EXHAUSTED", "CAPACITY_RESERVED is false: no reservation", "capacity reservation recorded")
    else:
        bound(reservation, "capacity reservation", subject_pairs)
        if not reservation.is_active(now):
            wait("CAPACITY_EXHAUSTED", "capacity reservation is not active", "reservation renewed")
        if receipt is not None and receipt.reservation_id is None:
            wait("READINESS_STALE", "readiness receipt was issued before capacity was reserved", "receipt re-probed against the reservation")
        elif receipt is not None and receipt.reservation_id != reservation.reservation_id:
            wait("READINESS_STALE", "readiness receipt names a different reservation", "receipt re-probed")

    return ReadinessDecision(ready=not reasons, reasons=tuple(reasons))
