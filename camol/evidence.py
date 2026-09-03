"""Strict evidence envelopes for untrusted worker and trusted observer output."""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .artifacts import ArtifactRef
from .events import EVIDENCE_KINDS
from .probes import Redactor
from .schema import (
    canonical_json_bytes,
    reject_unknown_fields,
    require_choice,
    require_digest,
    require_identifier,
    require_object,
    require_schema_header,
    require_timestamp,
)


class EvidenceError(ValueError):
    """An evidence envelope is malformed, oversized, or ambiguously bound."""


EPISTEMIC_STATUSES = frozenset(
    {"OBSERVED", "EXECUTED", "DERIVED", "INFERRED", "HUMAN_REPORTED", "UNVERIFIED", "CONTRADICTED"}
)

GATE_STATUSES = {
    "command": frozenset({"EXECUTED"}),
    "tool_call": frozenset({"EXECUTED"}),
    "transcript": frozenset({"OBSERVED"}),
    "environment": frozenset({"OBSERVED"}),
    "artifact": frozenset({"OBSERVED", "DERIVED"}),
    "diff": frozenset({"OBSERVED", "DERIVED"}),
    "test_result": frozenset({"EXECUTED"}),
    # A claim is an assertion awaiting evaluation; requiring it to say
    # UNVERIFIED prevents the claim itself from masquerading as proof.
    "claim": frozenset({"UNVERIFIED", "CONTRADICTED"}),
}


def _validate_json(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise EvidenceError("evidence data exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int, float, str)):
        canonical_json_bytes(value)
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise EvidenceError("evidence array is too large")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 4096 or any(not isinstance(key, str) or not key for key in value):
            raise EvidenceError("evidence object has invalid or excessive keys")
        for item in value.values():
            _validate_json(item, depth=depth + 1)
        return
    raise EvidenceError("evidence data contains an unsupported value type")


@dataclass(frozen=True)
class EvidenceRecord:
    """One immutable, subject-bound evidence item stored in the event ledger."""

    SCHEMA = "camol.evidence"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "evidence_id", "run_id", "task_id",
        "debug_case_id", "agent_id", "lease_id", "fence_digest", "kind",
        "epistemic_status", "producer", "observed_at", "data", "artifact_refs",
    )

    evidence_id: str
    run_id: str
    task_id: Optional[str]
    debug_case_id: Optional[str]
    agent_id: Optional[str]
    lease_id: Optional[str]
    fence_digest: Optional[str]
    kind: str
    epistemic_status: str
    producer: str
    observed_at: str
    data: Dict[str, Any]
    artifact_refs: Tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("evidence_id", "run_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "evidence " + name))
        for name in ("task_id", "debug_case_id", "agent_id", "lease_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_identifier(value, "evidence " + name))
        if (self.task_id is None) == (self.debug_case_id is None):
            raise EvidenceError("evidence must bind exactly one task or debug case")
        if self.task_id is not None and (self.agent_id is None or self.lease_id is None or self.fence_digest is None):
            raise EvidenceError("task evidence must bind agent, lease, and fence")
        if self.fence_digest is not None:
            object.__setattr__(self, "fence_digest", require_digest(self.fence_digest, "evidence fence_digest"))
        object.__setattr__(self, "kind", require_choice(self.kind, "evidence kind", EVIDENCE_KINDS))
        object.__setattr__(
            self,
            "epistemic_status",
            require_choice(self.epistemic_status, "evidence epistemic_status", EPISTEMIC_STATUSES),
        )
        object.__setattr__(self, "producer", require_identifier(self.producer, "evidence producer"))
        object.__setattr__(self, "observed_at", require_timestamp(self.observed_at, "evidence observed_at"))
        data = require_object(self.data, "evidence data")
        _validate_json(data)
        if len(canonical_json_bytes(data)) > 262144:
            raise EvidenceError("evidence data exceeds 256 KiB; store content as an artifact")
        object.__setattr__(self, "data", data)
        references = tuple(self.artifact_refs)
        if any(not isinstance(item, ArtifactRef) for item in references):
            raise EvidenceError("evidence artifact_refs must contain ArtifactRef records")
        if len({item.digest for item in references}) != len(references):
            raise EvidenceError("evidence artifact_refs must not contain duplicate digests")
        object.__setattr__(self, "artifact_refs", tuple(sorted(references, key=lambda item: item.digest)))

    def satisfies_gate(self) -> bool:
        return self.epistemic_status in GATE_STATUSES[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "debug_case_id": self.debug_case_id,
            "agent_id": self.agent_id,
            "lease_id": self.lease_id,
            "fence_digest": self.fence_digest,
            "kind": self.kind,
            "epistemic_status": self.epistemic_status,
            "producer": self.producer,
            "observed_at": self.observed_at,
            "data": self.data,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceRecord":
        data = require_object(payload, "evidence record")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "evidence record")
        reject_unknown_fields(data, cls.FIELDS, "evidence record")
        missing = sorted(set(cls.FIELDS) - set(data))
        if missing:
            raise EvidenceError("evidence record is missing fields: {}".format(", ".join(missing)))
        raw_refs = data["artifact_refs"]
        if not isinstance(raw_refs, list):
            raise EvidenceError("evidence artifact_refs must be an array")
        values = {
            name: data[name]
            for name in cls.FIELDS
            if name not in {"schema", "schema_version", "artifact_refs"}
        }
        return cls(artifact_refs=tuple(ArtifactRef.from_dict(item) for item in raw_refs), **values)

    @classmethod
    def task(
        cls,
        *,
        evidence_id: str,
        run_id: str,
        task_id: str,
        agent_id: str,
        lease_id: str,
        fence_digest: str,
        kind: str,
        epistemic_status: str,
        producer: str,
        observed_at: str,
        data: Dict[str, Any],
        artifact_refs: Sequence[ArtifactRef] = (),
        redactor: Optional[Redactor] = None,
    ) -> "EvidenceRecord":
        clean = (redactor or Redactor()).value(data)
        return cls(
            evidence_id=evidence_id,
            run_id=run_id,
            task_id=task_id,
            debug_case_id=None,
            agent_id=agent_id,
            lease_id=lease_id,
            fence_digest=fence_digest,
            kind=kind,
            epistemic_status=epistemic_status,
            producer=producer,
            observed_at=observed_at,
            data=clean,
            artifact_refs=tuple(artifact_refs),
        )
