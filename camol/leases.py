"""Lease extension proof that preserves the original packet's fenced identity."""

from typing import Any, Dict

from .admission import AdmissionBundle
from .readiness import LeaseFence
from .schema import parse_timestamp, reject_unknown_fields


def effective_expiry(state: Dict[str, Any], task: Dict[str, Any]) -> str:
    extension = state.get("lease_authorizations", {}).get(task["lease_id"])
    if extension is not None and extension["fence_digest"] == task["fence_digest"]:
        return extension["expires_at"]
    return task["lease_fence"]["expires_at"]


def validate_refresh_identity(original: AdmissionBundle, refreshed: AdmissionBundle) -> None:
    """Permit fresh observations, never a new execution subject or authority."""
    for name in ("authority_policy", "probe_policy", "sandbox_policy"):
        if getattr(original, name).digest() != getattr(refreshed, name).digest():
            raise ValueError("lease refresh changed {}".format(name))
    if original.binding.subject() != refreshed.binding.subject():
        raise ValueError("lease refresh changed its execution subject")
    if original.binding.plan_digest != refreshed.binding.plan_digest:
        raise ValueError("lease refresh changed its plan")
    for field, value in original.workspace.to_dict().items():
        if field != "dirty_digest" and refreshed.workspace.to_dict().get(field) != value:
            raise ValueError("lease refresh changed workspace identity")
    if original.evaluator_digest != refreshed.evaluator_digest:
        raise ValueError("lease refresh changed its evaluator")
    for name in ("runtime_id", "adapter_kind", "requested_model", "transport_id", "credential_scopes"):
        if getattr(original.receipt, name) != getattr(refreshed.receipt, name):
            raise ValueError("lease refresh changed runtime or provider identity")
    if original.grant.granted_by != refreshed.grant.granted_by:
        raise ValueError("lease refresh changed approval identity")
    for name in ("concurrency_slots", "max_tokens", "max_usd_cents"):
        if getattr(original.reservation, name) != getattr(refreshed.reservation, name):
            raise ValueError("lease refresh changed its reserved budget")


def validate_authorization(state: Dict[str, Any], payload: Dict[str, Any]) -> AdmissionBundle:
    """Validate at creation and replay; expired/reassigned leases cannot revive."""
    fields = {"task_id", "lease_id", "fence_digest", "bundle", "bundle_digest", "refreshed_at", "expires_at", "scope"}
    reject_unknown_fields(payload, fields, "lease authorization")
    if set(payload) not in (fields, fields - {"scope"}):
        raise ValueError("lease authorization is missing fields")
    task = state["tasks"].get(payload["task_id"])
    if task is None or task["status"] not in {"running", "verifying"}:
        raise ValueError("lease refresh needs active running or verifying work")
    if task["lease_id"] != payload["lease_id"] or task["fence_digest"] != payload["fence_digest"]:
        raise ValueError("lease refresh does not match the active fenced lease")
    fence = LeaseFence.from_dict(task["lease_fence"])
    if fence.epoch != state["lease_epochs"][task["id"]]:
        raise ValueError("lease refresh has a superseded epoch")
    now = parse_timestamp(payload["refreshed_at"], "lease refresh now")
    scope = payload.get("scope", "active")
    if scope not in {"active", "verification_resume"}:
        raise ValueError("lease authorization has an unknown scope")
    if scope == "verification_resume" and (
        task["status"] != "verifying" or task.get("gate_wait")
        or not any(item["lease_id"] == task["lease_id"] and item["fence_digest"] == task["fence_digest"] for item in state["candidates"].values())
    ):
        raise ValueError("verification resumption requires a captured candidate and no pending human gate")
    if now < parse_timestamp(fence.issued_at, "lease issued") or (
        scope == "active" and now >= parse_timestamp(effective_expiry(state, task), "lease expiry")
    ):
        raise ValueError("an expired lease cannot be refreshed")
    original = AdmissionBundle.from_dict(state["admissions"]["{}:{}".format(task["id"], task["agent_id"])])
    previous = state.get("lease_authorizations", {}).get(task["lease_id"])
    current = AdmissionBundle.from_dict(previous["bundle"]) if previous else original
    if current.reservation.reservation_id in state["released_reservation_ids"]:
        raise ValueError("a released reservation cannot be refreshed")
    refreshed = AdmissionBundle.from_dict(payload["bundle"])
    from .source_binding import require_source_admission
    require_source_admission(state, refreshed)
    if refreshed.digest() != payload["bundle_digest"]:
        raise ValueError("lease authorization bundle digest is invalid")
    validate_refresh_identity(original, refreshed)
    if refreshed.reservation.reservation_id in state["reservations"]:
        raise ValueError("lease authorization reservation is already recorded")
    decision = refreshed.decision(
        now=payload["refreshed_at"], plan_digest=state["plan_digest"],
        plan_frozen=state["approved_by"] is not None,
        dependencies_green=all(state["tasks"][key]["status"] == "succeeded" for key in task["depends_on"]),
    )
    if not decision.ready:
        raise ValueError("lease authorization is not ready: " + decision.reasons[0].detail)
    expiry = parse_timestamp(payload["expires_at"], "lease authorization expiry")
    if state["runbook"].get("schema_version", 0) >= 6:
        from .capacity_runtime import capacity_for_task
        capacity = capacity_for_task(state, task["id"], task["agent_id"], now=payload["refreshed_at"])
        if expiry > parse_timestamp(capacity["expires_at"], "global capacity expiry"):
            raise ValueError("lease authorization outlives its shared capacity proof")
    if not now < expiry <= min(
        parse_timestamp(refreshed.receipt.expires_at, "readiness expiry"),
        parse_timestamp(refreshed.grant.expires_at, "grant expiry"),
        parse_timestamp(refreshed.reservation.expires_at, "reservation expiry"),
    ):
        raise ValueError("lease authorization outlives its admission proof")
    return refreshed
