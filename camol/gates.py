"""Pure, provider-independent invariant and obligation gate contracts.

Callers own execution and authorization boundaries. This module consumes their
evidence; it never executes a predicate string or trusts worker assertions as
an oracle. Every result remains bound to the evaluated candidate and plan.
"""

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from .evidence import EvidenceRecord
from .events import EVIDENCE_KINDS
from .schema import canonical_digest, require_bool, require_digest, require_identifier, require_schema_header, require_string, require_timestamp


class GateError(ValueError):
    """A gate contract is malformed, inconsistent, or attempts to change authority."""


VERDICTS = frozenset({"DISPROVED", "NOT_DISPROVED_WITHIN_BUDGET", "STATISTICALLY_REGRESSED",
                      "SUPPORTED_BY_REQUIRED_EVIDENCE", "OBSERVATION_INCOMPLETE", "EVIDENCE_CONFLICT", "HUMAN_ACCEPTED"})
FAMILIES = frozenset({"deterministic", "property", "metamorphic", "adaptive", "integration", "real_boundary", "rollback"})
LEVEL_FAMILIES = {"basic": ("deterministic",),
                  "backed": ("deterministic", "property", "metamorphic", "adaptive"),
                  "critical": ("deterministic", "property", "metamorphic", "adaptive", "rollback")}


def _strings(value, label, *, choices=None, nonempty=True, identifiers=True):
    if not isinstance(value, tuple) or (nonempty and not value):
        raise GateError(label + " must be a non-empty tuple" if nonempty else label + " must be a tuple")
    for item in value:
        (require_identifier if identifiers else require_string)(item, label)
        if choices is not None and item not in choices:
            raise GateError(label + " contains an unknown value")
    if len(set(value)) != len(value):
        raise GateError(label + " contains duplicates")


class _Record:
    SCHEMA_VERSION = 1

    def to_dict(self) -> Dict[str, Any]:
        result = {"schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION}
        for field in fields(self):
            value = getattr(self, field.name)
            result[field.name] = value.to_dict() if isinstance(value, (_Record, EvidenceRecord)) else list(value) if isinstance(value, tuple) else value
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise GateError(cls.SCHEMA + " must be an object")
        require_schema_header(value, cls.SCHEMA, cls.SCHEMA_VERSION, cls.SCHEMA)
        names = {field.name for field in fields(cls)}
        if set(value) != names | {"schema", "schema_version"}:
            raise GateError(cls.SCHEMA + " has missing or unknown fields")
        data = {name: value[name] for name in names}
        for name in getattr(cls, "ARRAY_FIELDS", ()):
            if not isinstance(data[name], list):
                raise GateError(name + " must be an array")
            data[name] = tuple(data[name])
        for name, record_type in getattr(cls, "RECORD_FIELDS", {}).items():
            data[name] = record_type.from_dict(data[name])
        return cls(**data)


@dataclass(frozen=True)
class GateBinding(_Record):
    SCHEMA = "camol.gate_binding"
    run_id: str
    plan_digest: str
    evaluator_digest: str
    artifact_digest: str
    environment_digest: str

    def __post_init__(self):
        require_identifier(self.run_id, "gate run_id")
        for name in ("plan_digest", "evaluator_digest", "artifact_digest", "environment_digest"):
            require_digest(getattr(self, name), "gate " + name)


@dataclass(frozen=True)
class Invariant(_Record):
    SCHEMA = "camol.invariant"
    ARRAY_FIELDS = ("preconditions", "observables", "falsification_strategies", "required_evidence")
    invariant_id: str
    revision: int
    owner: str
    scope: str
    modality: str
    predicate: str
    preconditions: Tuple[str, ...]
    observables: Tuple[str, ...]
    severity: str
    falsification_strategies: Tuple[str, ...]
    required_evidence: Tuple[str, ...]
    positive_evidence_required: bool
    approval_policy: str

    def __post_init__(self):
        for name in ("invariant_id", "owner", "scope"):
            require_identifier(getattr(self, name), "invariant " + name)
        if type(self.revision) is not int or self.revision < 1:
            raise GateError("invariant revision must be positive")
        if self.modality not in {"always", "never", "eventually", "until", "preserved", "at_most_once", "exactly_once"}:
            raise GateError("invariant modality is invalid")
        require_string(self.predicate, "invariant predicate")
        _strings(self.preconditions, "invariant preconditions", nonempty=False, identifiers=False)
        _strings(self.observables, "invariant observables", identifiers=False)
        _strings(self.falsification_strategies, "invariant falsification strategies", nonempty=False, identifiers=False)
        _strings(self.required_evidence, "invariant required evidence", choices=EVIDENCE_KINDS)
        if self.severity not in {"normal", "high", "critical"}:
            raise GateError("invariant severity is invalid")
        require_bool(self.positive_evidence_required, "invariant positive_evidence_required")
        if self.approval_policy not in {"preauthorized", "human"}:
            raise GateError("invariant approval policy is invalid")


@dataclass(frozen=True)
class Obligation(_Record):
    SCHEMA = "camol.obligation"
    ARRAY_FIELDS = ("invariant_ids", "task_ids")
    obligation_id: str
    owner: str
    target_state: str
    invariant_ids: Tuple[str, ...]
    task_ids: Tuple[str, ...]

    def __post_init__(self):
        for name in ("obligation_id", "owner", "target_state"):
            require_identifier(getattr(self, name), "obligation " + name)
        _strings(self.invariant_ids, "obligation invariant ids")
        _strings(self.task_ids, "obligation task ids")


@dataclass(frozen=True)
class GatePolicy(_Record):
    SCHEMA = "camol.gate_policy"
    ARRAY_FIELDS = ("required_families",)
    policy_id: str
    level: str
    required_families: Tuple[str, ...]
    independent_verifier: bool
    human_approval: bool

    def __post_init__(self):
        require_identifier(self.policy_id, "gate policy id")
        if self.level not in LEVEL_FAMILIES:
            raise GateError("gate threshold must be basic, backed, or critical")
        _strings(self.required_families, "gate required families", choices=FAMILIES)
        if not set(LEVEL_FAMILIES[self.level]).issubset(self.required_families):
            raise GateError("gate cannot weaken its named threshold")
        require_bool(self.independent_verifier, "gate independent verifier")
        require_bool(self.human_approval, "gate human approval")
        if self.level == "critical" and not (self.independent_verifier and self.human_approval):
            raise GateError("critical gates require independent verification and human approval")

    @classmethod
    def compile(cls, policy_id: str, level: str, *, integration=False, real_boundary=False):
        if level not in LEVEL_FAMILIES:
            raise GateError("unknown gate threshold")
        families = list(LEVEL_FAMILIES[level])
        if integration:
            families.append("integration")
        if real_boundary:
            families.append("real_boundary")
        return cls(policy_id, level, tuple(families), level == "critical", level == "critical")


@dataclass(frozen=True)
class GateObservation(_Record):
    SCHEMA = "camol.gate_observation"
    RECORD_FIELDS = {"binding": GateBinding, "evidence": EvidenceRecord}
    observation_id: str
    binding: GateBinding
    invariant_id: str
    evaluator_family: str
    verifier_id: str
    verdict: str
    evidence: EvidenceRecord

    def __post_init__(self):
        for name in ("observation_id", "invariant_id", "verifier_id"):
            require_identifier(getattr(self, name), "gate observation " + name)
        if not isinstance(self.binding, GateBinding) or not isinstance(self.evidence, EvidenceRecord):
            raise GateError("gate observation requires strict binding and evidence records")
        if self.evaluator_family not in FAMILIES or self.verdict not in VERDICTS:
            raise GateError("gate observation has an unknown evaluator family or verdict")
        if self.evidence.run_id != self.binding.run_id:
            raise GateError("gate observation evidence belongs to another run")


@dataclass(frozen=True)
class GateApproval(_Record):
    SCHEMA = "camol.gate_approval"
    RECORD_FIELDS = {"binding": GateBinding}
    approval_id: str
    binding: GateBinding
    policy_digest: str
    approved_by: str
    created_at: str
    expires_at: str

    def __post_init__(self):
        require_identifier(self.approval_id, "gate approval id")
        require_identifier(self.approved_by, "gate approved_by")
        if not isinstance(self.binding, GateBinding):
            raise GateError("gate approval requires a strict binding")
        require_digest(self.policy_digest, "gate approval policy digest")
        for name in ("created_at", "expires_at"):
            object.__setattr__(self, name, require_timestamp(getattr(self, name), "gate approval " + name))
        if datetime.fromisoformat(self.created_at) >= datetime.fromisoformat(self.expires_at):
            raise GateError("gate approval validity window is empty")

    def fresh(self, now: str) -> bool:
        moment = datetime.fromisoformat(require_timestamp(now, "gate now"))
        return datetime.fromisoformat(self.created_at) <= moment < datetime.fromisoformat(self.expires_at)


@dataclass(frozen=True)
class Waiver(_Record):
    SCHEMA = "camol.gate_waiver"
    RECORD_FIELDS = {"approval": GateApproval}
    ARRAY_FIELDS = ("compensating_evidence_ids",)
    waiver_id: str
    invariant_id: str
    reason: str
    remediation_obligation_id: str
    compensating_evidence_ids: Tuple[str, ...]
    approval: GateApproval

    def __post_init__(self):
        for name in ("waiver_id", "invariant_id", "remediation_obligation_id"):
            require_identifier(getattr(self, name), "waiver " + name)
        require_string(self.reason, "waiver reason")
        _strings(self.compensating_evidence_ids, "waiver compensating evidence")
        if not isinstance(self.approval, GateApproval):
            raise GateError("waiver requires a strict approval")


@dataclass(frozen=True)
class GateAssessment(_Record):
    SCHEMA = "camol.gate_assessment"
    RECORD_FIELDS = {"binding": GateBinding}
    ARRAY_FIELDS = ("satisfied_obligations", "missing_obligations", "evidence_ids", "waiver_ids", "reasons")
    binding: GateBinding
    policy_digest: str
    verdict: str
    status: str
    satisfied_obligations: Tuple[str, ...]
    missing_obligations: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    waiver_ids: Tuple[str, ...]
    reasons: Tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.binding, GateBinding):
            raise GateError("gate assessment requires a strict binding")
        require_digest(self.policy_digest, "gate assessment policy digest")
        if self.verdict not in VERDICTS or self.status not in {"GREEN", "GREEN_WITH_WAIVER", "BLOCKED", "AWAITING_HUMAN"}:
            raise GateError("gate assessment has an invalid verdict or status")
        for name in self.ARRAY_FIELDS:
            _strings(getattr(self, name), "gate assessment " + name, nonempty=False)
        if set(self.satisfied_obligations) & set(self.missing_obligations):
            raise GateError("gate cannot satisfy and miss the same obligation")
        if self.status in {"GREEN", "GREEN_WITH_WAIVER"} and (self.missing_obligations or self.reasons or self.verdict not in {"SUPPORTED_BY_REQUIRED_EVIDENCE", "HUMAN_ACCEPTED"}):
            raise GateError("green gate cannot contain unresolved failures")
        if self.status in {"GREEN", "GREEN_WITH_WAIVER"} and (self.status == "GREEN_WITH_WAIVER") != bool(self.waiver_ids):
            raise GateError("gate waiver status is inconsistent")

    @property
    def passed(self):
        return self.status in {"GREEN", "GREEN_WITH_WAIVER"}


def inherit_invariants(*layers: Iterable[Invariant]) -> Tuple[Invariant, ...]:
    """Inherited contracts may be repeated or augmented, never silently changed.

    A different predicate is not provably stronger by text comparison. Any edit
    to an existing identifier requires an explicit plan amendment.
    """
    combined = {}
    for layer in layers:
        for invariant in layer:
            if not isinstance(invariant, Invariant):
                raise GateError("inheritance requires invariant records")
            previous = combined.get(invariant.invariant_id)
            if previous is not None and previous != invariant:
                raise GateError("inherited invariant changed without a plan amendment: " + invariant.invariant_id)
            combined[invariant.invariant_id] = invariant
    return tuple(combined[key] for key in sorted(combined))


def assess_gate(*, binding: GateBinding, policy: GatePolicy, invariants: Iterable[Invariant],
                obligations: Iterable[Obligation], observations: Iterable[GateObservation],
                builder_ids: Iterable[str], now: str, approval: Optional[GateApproval] = None,
                waivers: Iterable[Waiver] = ()) -> GateAssessment:
    """Adjudicate one frozen state exit without executing code or modifying state."""
    require_timestamp(now, "gate now")
    invariant_map = {item.invariant_id: item for item in inherit_invariants(invariants)}
    obligation_map = {}
    for obligation in obligations:
        if obligation.obligation_id in obligation_map:
            raise GateError("duplicate obligation id")
        if not set(obligation.invariant_ids).issubset(invariant_map):
            raise GateError("obligation refers to an unknown invariant")
        obligation_map[obligation.obligation_id] = obligation
    if not invariant_map or not obligation_map:
        raise GateError("a gate needs invariants and obligations")
    covered = {identifier for item in obligation_map.values() for identifier in item.invariant_ids}
    if covered != set(invariant_map):
        raise GateError("every effective invariant must be attached to an obligation")
    builders = set(builder_ids)
    if not builders:
        raise GateError("gate requires explicit builder identities")
    valid, reasons, observation_ids, evidence_ids = [], [], set(), {}
    for observation in observations:
        previous = evidence_ids.get(observation.evidence.evidence_id)
        if observation.observation_id in observation_ids or (previous is not None and previous != observation.evidence):
            raise GateError("duplicate gate observation or conflicting evidence id")
        observation_ids.add(observation.observation_id)
        evidence_ids[observation.evidence.evidence_id] = observation.evidence
        if observation.binding != binding:
            reasons.append("stale_or_foreign_evidence:" + observation.observation_id)
            continue
        if observation.invariant_id not in invariant_map:
            raise GateError("observation names an unknown invariant")
        if observation.evidence.producer not in {"verifier", "collector", "adapter"} or not observation.evidence.satisfies_gate() or observation.evidence.epistemic_status in {"UNVERIFIED", "INFERRED", "HUMAN_REPORTED", "CONTRADICTED"}:
            reasons.append("unverified_evidence:" + observation.observation_id)
            continue
        if policy.independent_verifier and observation.verifier_id in builders:
            reasons.append("self_verification:" + observation.observation_id)
            continue
        if observation.verdict == "HUMAN_ACCEPTED":
            reasons.append("human_verdict_requires_approval:" + observation.observation_id)
            continue
        valid.append(observation)
    policy_digest = policy.digest()
    def authorized(item):
        return item.binding == binding and item.policy_digest == policy_digest and item.approved_by not in builders and item.fresh(now)
    waiver_map = {}
    for waiver in waivers:
        if waiver.invariant_id not in invariant_map or waiver.invariant_id in waiver_map:
            raise GateError("waiver names an unknown or duplicate invariant")
        supporting = {item.evidence.evidence_id for item in valid if item.verdict == "SUPPORTED_BY_REQUIRED_EVIDENCE"}
        if not authorized(waiver.approval) or not set(waiver.compensating_evidence_ids).issubset(supporting):
            reasons.append("invalid_or_expired_waiver:" + waiver.waiver_id)
            continue
        if waiver.remediation_obligation_id not in obligation_map:
            raise GateError("waiver remediation obligation is undeclared")
        waiver_map[waiver.invariant_id] = waiver
    satisfied = set()
    failures = []
    for identifier, invariant in invariant_map.items():
        relevant = [item for item in valid if item.invariant_id == identifier]
        negative = [item for item in relevant if item.verdict in {"DISPROVED", "STATISTICALLY_REGRESSED", "EVIDENCE_CONFLICT"}]
        if negative and identifier not in waiver_map:
            failures.extend(item.verdict for item in negative)
            reasons.append("rejected_invariant:" + identifier)
            continue
        if identifier in waiver_map:
            satisfied.add(identifier)
            continue
        positive = [item for item in relevant if item.verdict == "SUPPORTED_BY_REQUIRED_EVIDENCE" or
                    (item.verdict == "NOT_DISPROVED_WITHIN_BUDGET" and not invariant.positive_evidence_required)]
        # Adaptive non-disproof can demonstrate completed search, but never the
        # positive target evidence required by an invariant.
        completed_families = {item.evaluator_family for item in relevant if item.verdict in {"SUPPORTED_BY_REQUIRED_EVIDENCE", "NOT_DISPROVED_WITHIN_BUDGET"}}
        present = {item.evidence.kind for item in positive}
        if not set(invariant.required_evidence).issubset(present):
            reasons.append("missing_positive_evidence:" + identifier)
        elif not set(policy.required_families).issubset(completed_families):
            reasons.append("incomplete_evaluator_cascade:" + identifier)
        else:
            satisfied.add(identifier)
    accepted_obligations, missing = [], []
    for identifier, obligation in obligation_map.items():
        supporting_tasks = {item.evidence.task_id for item in valid if item.invariant_id in obligation.invariant_ids and item.verdict in {"SUPPORTED_BY_REQUIRED_EVIDENCE", "NOT_DISPROVED_WITHIN_BUDGET"}}
        complete = set(obligation.invariant_ids).issubset(satisfied) and set(obligation.task_ids).issubset(supporting_tasks)
        (accepted_obligations if complete else missing).append(identifier)
    needs_human = policy.human_approval or any(item.approval_policy == "human" for item in invariant_map.values())
    if failures:
        verdict = "EVIDENCE_CONFLICT" if "EVIDENCE_CONFLICT" in failures else "DISPROVED" if "DISPROVED" in failures else "STATISTICALLY_REGRESSED"
        status = "BLOCKED"
    elif missing or reasons:
        verdict, status = "OBSERVATION_INCOMPLETE", "BLOCKED"
    elif needs_human and (approval is None or not authorized(approval)):
        verdict, status = "SUPPORTED_BY_REQUIRED_EVIDENCE", "AWAITING_HUMAN"
        reasons.append("human_approval_required")
    else:
        verdict = "HUMAN_ACCEPTED" if needs_human else "SUPPORTED_BY_REQUIRED_EVIDENCE"
        status = "GREEN_WITH_WAIVER" if waiver_map else "GREEN"
    return GateAssessment(binding, policy_digest, verdict, status, tuple(sorted(accepted_obligations)),
                          tuple(sorted(missing)), tuple(sorted({item.evidence.evidence_id for item in valid})),
                          tuple(sorted(item.waiver_id for item in waiver_map.values())), tuple(reasons))
