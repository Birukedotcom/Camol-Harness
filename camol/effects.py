"""Strict external-effect intent records and replay-safety helpers."""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_bool,
    require_digest,
    require_identifier,
    require_optional_digest,
    require_schema_header,
    require_timestamp,
)


class EffectError(ValueError):
    """An effect record or transition is unsafe or malformed."""


EFFECT_STATES = frozenset({"EFFECT_REQUESTED", "EFFECT_CONFIRMED", "EFFECT_REJECTED", "EFFECT_UNKNOWN"})


@dataclass(frozen=True)
class EffectRequest:
    """Intent persisted before a remote mutation is attempted."""

    SCHEMA = "camol.effect_request"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "effect_id", "run_id", "task_id",
        "lease_id", "fence_digest", "idempotency_key", "provider",
        "operation", "target", "request_digest", "approval_digest",
        "requested_by", "requested_at", "state",
    )

    effect_id: str
    run_id: str
    task_id: str
    lease_id: str
    fence_digest: str
    idempotency_key: str
    provider: str
    operation: str
    target: str
    request_digest: str
    approval_digest: str
    requested_by: str
    requested_at: str
    state: str = "EFFECT_REQUESTED"

    def __post_init__(self) -> None:
        for name in (
            "effect_id", "run_id", "task_id", "lease_id", "provider",
            "operation", "requested_by",
        ):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "effect " + name))
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise EffectError("effect idempotency_key is required")
        if len(self.idempotency_key) > 200:
            raise EffectError("effect idempotency_key is too long")
        if not isinstance(self.target, str) or not self.target.strip():
            raise EffectError("effect target is required")
        for name in ("fence_digest", "request_digest", "approval_digest"):
            object.__setattr__(self, name, require_digest(getattr(self, name), "effect " + name))
        object.__setattr__(self, "requested_at", require_timestamp(self.requested_at, "effect requested_at"))
        if self.state != "EFFECT_REQUESTED":
            raise EffectError("a new effect record must be EFFECT_REQUESTED")

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION, **{
            name: getattr(self, name) for name in self.FIELDS if name not in {"schema", "schema_version"}
        }}

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EffectRequest":
        if not isinstance(payload, dict):
            raise EffectError("effect request must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "effect request")
        reject_unknown_fields(payload, cls.FIELDS, "effect request")
        missing = sorted(set(cls.FIELDS) - set(payload))
        if missing:
            raise EffectError("effect request is missing fields: {}".format(", ".join(missing)))
        return cls(**{key: payload[key] for key in cls.FIELDS if key not in {"schema", "schema_version"}})


@dataclass(frozen=True)
class EffectOutcome:
    """Digest-only outcome for a previously persisted external-effect intent."""

    SCHEMA = "camol.effect_outcome"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "effect_id", "state", "outcome_digest",
        "readback_digest", "reconciled", "resolved_at",
    )

    effect_id: str
    state: str
    outcome_digest: Optional[str]
    readback_digest: Optional[str]
    reconciled: bool
    resolved_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect_id", require_identifier(self.effect_id, "effect outcome id"))
        if self.state not in EFFECT_STATES - {"EFFECT_REQUESTED"}:
            raise EffectError("invalid effect outcome state")
        object.__setattr__(
            self, "outcome_digest", require_optional_digest(self.outcome_digest, "effect outcome digest")
        )
        object.__setattr__(
            self, "readback_digest", require_optional_digest(self.readback_digest, "effect readback digest")
        )
        object.__setattr__(self, "reconciled", require_bool(self.reconciled, "effect reconciled"))
        object.__setattr__(self, "resolved_at", require_timestamp(self.resolved_at, "effect resolved_at"))
        if self.state == "EFFECT_UNKNOWN" and (self.outcome_digest is not None or self.reconciled):
            raise EffectError("unknown effect cannot claim an outcome or reconciliation")
        if self.reconciled and self.readback_digest is None:
            raise EffectError("reconciled effect outcome requires provider readback")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "effect_id": self.effect_id,
            "state": self.state,
            "outcome_digest": self.outcome_digest,
            "readback_digest": self.readback_digest,
            "reconciled": self.reconciled,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EffectOutcome":
        if not isinstance(payload, dict):
            raise EffectError("effect outcome must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "effect outcome")
        reject_unknown_fields(payload, cls.FIELDS, "effect outcome")
        missing = sorted(set(cls.FIELDS) - set(payload))
        if missing:
            raise EffectError("effect outcome is missing fields: {}".format(", ".join(missing)))
        return cls(**{key: payload[key] for key in cls.FIELDS if key not in {"schema", "schema_version"}})


def outcome_payload(
    effect_id: str,
    state: str,
    *,
    resolved_at: str,
    outcome: Optional[Dict[str, Any]] = None,
    readback: Optional[Dict[str, Any]] = None,
    reconciled: bool = False,
) -> Dict[str, Any]:
    return EffectOutcome(
        effect_id=effect_id,
        state=state,
        outcome_digest=canonical_digest(outcome) if outcome is not None else None,
        readback_digest=canonical_digest(readback) if readback is not None else None,
        reconciled=reconciled,
        resolved_at=resolved_at,
    ).to_dict()
