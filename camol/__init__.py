"""Camol: a persistent, plan-driven multi-agent orchestration harness."""

from .hillclimb import compare_vectors
from .orchestrator import Orchestrator
from .readiness import (
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
from .runbook import RunbookError, load_runbook, migrate_runbook_v1_to_v2, validate_runbook
from .schema import SchemaError, canonical_digest, canonical_json_bytes
from .store import SQLiteEventStore

__all__ = [
    "AuthorityPolicy",
    "BoxBinding",
    "CapabilityGrant",
    "CapacityReservation",
    "LeaseFence",
    "Orchestrator",
    "ProbePolicy",
    "ProbeRequirement",
    "ProbeResult",
    "ReadinessReceipt",
    "RunbookError",
    "SQLiteEventStore",
    "SchemaError",
    "WaitingReason",
    "WorkspaceReceipt",
    "assess_ready_to_lease",
    "canonical_digest",
    "canonical_json_bytes",
    "compare_vectors",
    "load_runbook",
    "migrate_runbook_v1_to_v2",
    "validate_runbook",
]
