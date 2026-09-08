"""Event names and immutable event construction."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4
from .schema import require_timestamp


EVENT_TYPES = frozenset(
    {
        "TARGET_ADOPTED", "TARGET_RETIRED", "TARGET_RUNTIME_OBSERVED", "TARGET_SSH_RUNTIME_OBSERVED",
        "RUN_CREATED",
        "SOURCE_BASELINE_BOUND",
        "PLAN_APPROVED",
        "RUN_STARTED",
        "RUN_COMPLETED",
        "RUN_BLOCKED",
        "TASK_LEASED",
        "TASK_STARTED",
        "AGENT_TURN_RECORDED",
        "EVIDENCE_RECORDED",
        "MESSAGE_ROUTED",
        "BOX_PEER_READ_RECORDED",
        "BOX_PEER_CALL_STARTED", "BOX_PEER_CALL_FINISHED",
        "WORKER_STREAM_ENROLLED", "WORKER_STREAM_REVOKED", "WORKER_STREAM_IMPORTED", "WORKER_GATEWAY_CONFIGURED", "WORKER_GATEWAY_STOPPED",
        "BOX_MESSAGE_POSTED", "BOX_MESSAGE_DELIVERED", "BOX_MESSAGE_CONSUMED", "BOX_MESSAGE_SEND_REJECTED",
        "TASK_SUBMITTED",
        "TASK_VERIFICATION_RECORDED",
        "TASK_SUCCEEDED",
        "TASK_RETRY_SCHEDULED",
        "TASK_BLOCKED",
        "DEBUG_CASE_OPENED",
        "DEBUG_CASE_CREATED",
        "DEBUG_REPRODUCED",
        "DEBUG_LOCALIZED",
        "DEBUG_EXPERIMENT_STARTED",
        "DEBUG_EXPERIMENT_FINISHED",
        "DEBUG_VERIFIED",
        "DEBUG_CONTRADICTED",
        "DEBUG_BLOCKED",
        "DEBUG_RESUMED",
        "DEBUG_EVAL_PROMOTED",
        "DEBUG_REPRODUCTION_FROZEN", "DEBUG_EXECUTION_AUTHORIZED", "DEBUG_EXECUTION_STARTED",
        "DEBUG_EXECUTION_FINISHED", "DEBUG_EXECUTION_INTERRUPTED", "DEBUG_COUNTEREXAMPLE_LINKED",
        "WATCHER_CREATED", "WATCHER_BATCH_RECORDED", "WATCHER_WAITING", "WATCHER_CONFLICT", "WATCHER_REOPENED",
        "WATCHER_SCHEDULED", "WATCHER_POLL_STARTED", "WATCHER_POLL_FINISHED", "WATCHER_STOPPED",
        "DEBUG_CASE_VERIFIED",
        "EVAL_PROMOTED",
        "HILLCLIMB_RECORDED",
        # Readiness and typed non-runnable state.
        "READINESS_RECORDED",
        "TASK_WAITING",
        "TASK_WAIT_CLEARED",
        "PROVIDER_BUDGET_WAITING", "PROVIDER_BUDGET_WAIT_CLEARED",
        # M3 admission, capacity, and fenced lease lifecycle.
        "ADMISSION_RECORDED",
        "RESERVATION_RELEASED",
        "TASK_LEASE_REJECTED",
        "LEASE_HEARTBEAT",
        "LEASE_RENEWED",
        "LEASE_AUTHORIZATION_REFRESHED",
        "LEASE_REVOKED",
        # External mutation intent and reconciled outcome.
        "EFFECT_REQUESTED",
        "EFFECT_CONFIRMED",
        "EFFECT_REJECTED",
        "EFFECT_UNKNOWN",
        "WORKSPACE_SALVAGED",
        # Frozen evaluator, candidate, counterexample, and integration lineage.
        "CANDIDATE_CAPTURED",
        "VCS_RELATION_CHANGED",
        "VCS_OBSERVATION_STARTED", "VCS_OBSERVATION_FINISHED",
        "COUNTEREXAMPLE_RECORDED",
        "INTEGRATION_ACCEPTED",
        "GATE_ASSESSED",
        "GATE_APPROVED",
        "RUN_AWAITING_ACCEPTANCE",
        "RUN_ACCEPTED",
        "REVISION_PROPOSED",
        "RUN_SUPERSEDED",
        "REVISION_LINKED",
        "REVISION_EFFECT_REUSED",
        "GLOBAL_CAPACITY_RESERVED", "GLOBAL_CAPACITY_RENEWED", "GLOBAL_CAPACITY_RELEASED", "GLOBAL_CAPACITY_WAITING", "CAPACITY_CALL_RESERVED", "CAPACITY_CALL_DEFERRED",
    }
)

# Event types that existed before M0. Replay of a ledger containing only these
# must produce a projection identical to the pre-M0 projection.
LEGACY_EVENT_TYPES = frozenset(
    EVENT_TYPES
    - {
        "TARGET_ADOPTED", "TARGET_RETIRED", "TARGET_RUNTIME_OBSERVED", "TARGET_SSH_RUNTIME_OBSERVED",
        "WORKER_STREAM_ENROLLED", "WORKER_STREAM_REVOKED", "WORKER_STREAM_IMPORTED", "WORKER_GATEWAY_CONFIGURED", "WORKER_GATEWAY_STOPPED",
        "BOX_PEER_CALL_STARTED", "BOX_PEER_CALL_FINISHED",
        "BOX_PEER_READ_RECORDED",
        "BOX_MESSAGE_POSTED", "BOX_MESSAGE_DELIVERED", "BOX_MESSAGE_CONSUMED", "BOX_MESSAGE_SEND_REJECTED",
        "SOURCE_BASELINE_BOUND",
        "WATCHER_CREATED", "WATCHER_BATCH_RECORDED", "WATCHER_WAITING", "WATCHER_CONFLICT", "WATCHER_REOPENED",
        "WATCHER_SCHEDULED", "WATCHER_POLL_STARTED", "WATCHER_POLL_FINISHED", "WATCHER_STOPPED",
        "DEBUG_CASE_CREATED", "DEBUG_REPRODUCED", "DEBUG_LOCALIZED",
        "DEBUG_EXPERIMENT_STARTED", "DEBUG_EXPERIMENT_FINISHED", "DEBUG_VERIFIED",
        "DEBUG_CONTRADICTED", "DEBUG_BLOCKED", "DEBUG_RESUMED", "DEBUG_EVAL_PROMOTED",
        "DEBUG_REPRODUCTION_FROZEN", "DEBUG_EXECUTION_AUTHORIZED", "DEBUG_EXECUTION_STARTED",
        "DEBUG_EXECUTION_FINISHED", "DEBUG_EXECUTION_INTERRUPTED", "DEBUG_COUNTEREXAMPLE_LINKED",
        "READINESS_RECORDED",
        "TASK_WAITING",
        "TASK_WAIT_CLEARED",
        "PROVIDER_BUDGET_WAITING", "PROVIDER_BUDGET_WAIT_CLEARED",
        "ADMISSION_RECORDED",
        "RESERVATION_RELEASED",
        "TASK_LEASE_REJECTED",
        "LEASE_HEARTBEAT",
        "LEASE_RENEWED",
        "LEASE_AUTHORIZATION_REFRESHED",
        "LEASE_REVOKED",
        "EFFECT_REQUESTED",
        "EFFECT_CONFIRMED",
        "EFFECT_REJECTED",
        "EFFECT_UNKNOWN",
        "WORKSPACE_SALVAGED",
        "CANDIDATE_CAPTURED",
        "VCS_RELATION_CHANGED",
        "VCS_OBSERVATION_STARTED", "VCS_OBSERVATION_FINISHED",
        "COUNTEREXAMPLE_RECORDED",
        "INTEGRATION_ACCEPTED",
        "GATE_ASSESSED",
        "GATE_APPROVED",
        "RUN_AWAITING_ACCEPTANCE",
        "RUN_ACCEPTED",
        "REVISION_PROPOSED",
        "RUN_SUPERSEDED",
        "REVISION_LINKED",
        "REVISION_EFFECT_REUSED",
        "GLOBAL_CAPACITY_RESERVED", "GLOBAL_CAPACITY_RENEWED", "GLOBAL_CAPACITY_RELEASED", "GLOBAL_CAPACITY_WAITING", "CAPACITY_CALL_RESERVED", "CAPACITY_CALL_DEFERRED",
    }
)

EVIDENCE_KINDS = frozenset(
    {
        "command",
        "tool_call",
        "model_request",
        "model_usage",
        "transcript",
        "environment",
        "artifact",
        "diff",
        "test_result",
        "claim",
    }
)


def new_event(
    run_id: str,
    event_type: str,
    actor_id: str,
    payload: Dict[str, Any],
    *,
    causation_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError("unknown event type: {}".format(event_type))
    if not run_id or not actor_id:
        raise ValueError("run_id and actor_id are required")
    event = {
        "event_id": str(uuid4()),
        "run_id": run_id,
        "type": event_type,
        "actor_id": actor_id,
        "occurred_at": require_timestamp(occurred_at, "event occurred_at") if occurred_at is not None else datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    if causation_id:
        event["causation_id"] = causation_id
    if correlation_id:
        event["correlation_id"] = correlation_id
    return event
