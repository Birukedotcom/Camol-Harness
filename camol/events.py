"""Event names and immutable event construction."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


EVENT_TYPES = frozenset(
    {
        "RUN_CREATED",
        "PLAN_APPROVED",
        "RUN_STARTED",
        "RUN_COMPLETED",
        "RUN_BLOCKED",
        "TASK_LEASED",
        "TASK_STARTED",
        "AGENT_TURN_RECORDED",
        "EVIDENCE_RECORDED",
        "MESSAGE_ROUTED",
        "TASK_SUBMITTED",
        "TASK_VERIFICATION_RECORDED",
        "TASK_SUCCEEDED",
        "TASK_RETRY_SCHEDULED",
        "TASK_BLOCKED",
        "DEBUG_CASE_OPENED",
        "DEBUG_CASE_VERIFIED",
        "EVAL_PROMOTED",
        "HILLCLIMB_RECORDED",
        # Readiness and typed non-runnable state.
        "READINESS_RECORDED",
        "TASK_WAITING",
        "TASK_WAIT_CLEARED",
        # M3 admission, capacity, and fenced lease lifecycle.
        "ADMISSION_RECORDED",
        "RESERVATION_RELEASED",
        "TASK_LEASE_REJECTED",
        "LEASE_HEARTBEAT",
        "LEASE_RENEWED",
        "LEASE_REVOKED",
    }
)

# Event types that existed before M0. Replay of a ledger containing only these
# must produce a projection identical to the pre-M0 projection.
LEGACY_EVENT_TYPES = frozenset(
    EVENT_TYPES
    - {
        "READINESS_RECORDED",
        "TASK_WAITING",
        "TASK_WAIT_CLEARED",
        "ADMISSION_RECORDED",
        "RESERVATION_RELEASED",
        "TASK_LEASE_REJECTED",
        "LEASE_HEARTBEAT",
        "LEASE_RENEWED",
        "LEASE_REVOKED",
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
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    if causation_id:
        event["causation_id"] = causation_id
    if correlation_id:
        event["correlation_id"] = correlation_id
    return event
