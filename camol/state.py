"""Deterministic projections rebuilt from the event ledger."""

from copy import deepcopy
from typing import Any, Dict, Iterable

from .admission import AdmissionBundle
from .effects import EffectOutcome, EffectRequest
from .evidence import EvidenceRecord
from .readiness import LeaseFence, ReadinessReceipt, WaitingReason
from .schema import reject_unknown_fields, require_digest, require_string
from .workspace import SalvageReceipt


def empty_state() -> Dict[str, Any]:
    return {
        "run_id": None,
        "status": "missing",
        "runbook": None,
        "plan_digest": None,
        "approved_by": None,
        "agents": {},
        "tasks": {},
        "evidence": {},
        "messages": [],
        "debug_cases": {},
        "evals": {},
        "hillclimbs": [],
        "readiness_receipts": {},
        "admissions": {},
        "reservations": {},
        "released_reservation_ids": [],
        "lease_epochs": {},
        "heartbeats": {},
        "effects": {},
        "salvages": [],
        "total_tokens": 0,
        "last_seq": 0,
    }


def apply_event(state: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    next_state = deepcopy(state)
    payload = event["payload"]
    event_type = event["type"]

    if event_type == "RUN_CREATED":
        runbook = deepcopy(payload["runbook"])
        next_state["run_id"] = event["run_id"]
        next_state["status"] = "draft"
        next_state["runbook"] = runbook
        next_state["plan_digest"] = payload["plan_digest"]
        next_state["agents"] = {
            agent["id"]: dict(deepcopy(agent), status="idle", task_id=None, stats={
                "turns": 0,
                "tokens": 0,
                "successful_tasks": 0,
                "failed_attempts": 0,
                "verified_steps": 0,
            })
            for agent in runbook["agents"]
        }
        next_state["tasks"] = {
            task["id"]: dict(
                deepcopy(task),
                status="pending",
                agent_id=None,
                lease_id=None,
                lease_fence=None,
                fence_digest=None,
                attempts=0,
                turn_count=0,
                completed_step_ids=[],
                checkpoints=[],
                evidence_ids=[],
                verification_history=[],
                blocker=None,
            )
            for task in runbook["tasks"]
        }
    elif event_type == "PLAN_APPROVED":
        next_state["status"] = "ready"
        next_state["approved_by"] = payload["approved_by"]
    elif event_type == "RUN_STARTED":
        next_state["status"] = "running"
    elif event_type in {"RUN_COMPLETED", "RUN_BLOCKED"}:
        next_state["status"] = "completed" if event_type == "RUN_COMPLETED" else "blocked"
        next_state["terminal"] = deepcopy(payload)
    elif event_type == "TASK_LEASED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"][payload["agent_id"]]
        fence = LeaseFence.from_dict(payload["fence"])
        if task["status"] != "pending" or agent["status"] != "idle":
            raise ValueError("TASK_LEASED requires a pending task and idle worker")
        admission_payload = next_state["admissions"].get(
            "{}:{}".format(payload["task_id"], payload["agent_id"])
        )
        if admission_payload is None:
            raise ValueError("TASK_LEASED has no persisted admission bundle")
        admission = AdmissionBundle.from_dict(admission_payload)
        if admission.digest() != payload["admission_digest"]:
            raise ValueError("TASK_LEASED admission digest is invalid")
        if admission.reservation.reservation_id in next_state["released_reservation_ids"]:
            raise ValueError("TASK_LEASED uses a released reservation")
        if (
            fence.lease_id != payload["lease_id"]
            or fence.task_id != payload["task_id"]
            or fence.worker_id != payload["agent_id"]
            or fence.box_id != payload["box_id"]
            or fence.run_id != event["run_id"]
            or fence.target_id != admission.binding.target_id
        ):
            raise ValueError("TASK_LEASED fence does not match its event subject")
        if fence.digest() != payload["fence_digest"]:
            raise ValueError("TASK_LEASED fence digest is invalid")
        if fence.epoch != next_state["lease_epochs"].get(payload["task_id"], 0) + 1:
            raise ValueError("TASK_LEASED fence epoch is not monotonic")
        expected_digests = {
            "plan_digest": next_state["plan_digest"],
            "box_binding_digest": admission.binding.digest(),
            "evaluator_digest": admission.evaluator_digest,
            "workspace_digest": admission.workspace.digest(),
            "authority_digest": admission.authority_policy.digest(),
            "probe_policy_digest": admission.probe_policy.digest(),
            "readiness_digest": admission.receipt.digest(),
            "grant_digest": admission.grant.digest(),
            "reservation_digest": admission.reservation.digest(),
        }
        if fence.bound_digests() != expected_digests:
            raise ValueError("TASK_LEASED fence does not bind its admission bundle")
        task.update(
            status="leased",
            agent_id=payload["agent_id"],
            lease_id=payload["lease_id"],
            lease_fence=fence.to_dict(),
            fence_digest=fence.digest(),
            admission_digest=payload["admission_digest"],
        )
        next_state["lease_epochs"][payload["task_id"]] = fence.epoch
        agent.update(status="leased", task_id=payload["task_id"])
    elif event_type == "TASK_STARTED":
        task = next_state["tasks"][payload["task_id"]]
        if (
            task["status"] != "leased"
            or task["agent_id"] != payload["agent_id"]
            or task["lease_id"] != payload["lease_id"]
            or task["fence_digest"] != payload.get("fence_digest")
        ):
            raise ValueError("TASK_STARTED does not match the active fenced lease")
        task["status"] = "running"
        task["attempts"] += 1
    elif event_type == "AGENT_TURN_RECORDED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"][payload["agent_id"]]
        used_tokens = payload["input_tokens"] + payload["output_tokens"]
        task["turn_count"] += 1
        task["completed_step_ids"] = list(
            dict.fromkeys(task["completed_step_ids"] + payload["completed_step_ids"])
        )
        if payload.get("checkpoint"):
            task["checkpoints"].append(payload["checkpoint"])
        agent["stats"]["turns"] += 1
        agent["stats"]["tokens"] += used_tokens
        agent["stats"]["verified_steps"] += payload["newly_completed_steps"]
        next_state["total_tokens"] += used_tokens
    elif event_type == "EVIDENCE_RECORDED":
        if payload.get("schema") == EvidenceRecord.SCHEMA:
            record = EvidenceRecord.from_dict(payload)
            if record.run_id != event["run_id"]:
                raise ValueError("EVIDENCE_RECORDED belongs to another run")
            if record.task_id is not None:
                task = next_state["tasks"].get(record.task_id)
                if (
                    task is None
                    or task["agent_id"] != record.agent_id
                    or task["lease_id"] != record.lease_id
                    or task["fence_digest"] != record.fence_digest
                    or task["status"] not in {"running", "verifying"}
                ):
                    raise ValueError("EVIDENCE_RECORDED does not match the active fenced lease")
            elif record.debug_case_id not in next_state["debug_cases"]:
                raise ValueError("EVIDENCE_RECORDED names an unknown debug case")
            normalized_payload = record.to_dict()
        else:
            # Historical v0 ledgers used an unversioned payload. They remain
            # replayable but cannot satisfy the stricter epistemic gate below.
            normalized_payload = deepcopy(payload)
        if normalized_payload["evidence_id"] in next_state["evidence"]:
            raise ValueError("EVIDENCE_RECORDED evidence id already exists")
        next_state["evidence"][normalized_payload["evidence_id"]] = normalized_payload
        task_id = normalized_payload.get("task_id")
        if task_id:
            next_state["tasks"][task_id]["evidence_ids"].append(normalized_payload["evidence_id"])
        debug_case_id = normalized_payload.get("debug_case_id")
        if debug_case_id:
            next_state["debug_cases"][debug_case_id]["evidence_ids"].append(normalized_payload["evidence_id"])
    elif event_type == "MESSAGE_ROUTED":
        next_state["messages"].append(deepcopy(payload))
    elif event_type == "TASK_SUBMITTED":
        task = next_state["tasks"][payload["task_id"]]
        task["status"] = "verifying"
        task["submission"] = deepcopy(payload)
    elif event_type == "TASK_VERIFICATION_RECORDED":
        task = next_state["tasks"][payload["task_id"]]
        task["verification_history"].append(deepcopy(payload))
    elif event_type == "TASK_SUCCEEDED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"][task["agent_id"]]
        task.update(
            status="succeeded", lease_id=None, lease_fence=None, fence_digest=None,
            admission_digest=None,
        )
        agent.update(status="idle", task_id=None)
        agent["stats"]["successful_tasks"] += 1
    elif event_type == "TASK_RETRY_SCHEDULED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"][task["agent_id"]]
        task.update(
            status="pending", agent_id=None, lease_id=None, lease_fence=None,
            fence_digest=None, admission_digest=None,
        )
        task["last_retry_reason"] = payload["reason"]
        agent.update(status="idle", task_id=None)
        agent["stats"]["failed_attempts"] += 1
    elif event_type == "TASK_BLOCKED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"].get(task["agent_id"])
        task.update(
            status="blocked", blocker=deepcopy(payload["blocker"]), lease_id=None,
            lease_fence=None, fence_digest=None, admission_digest=None,
        )
        if agent:
            agent.update(status="idle", task_id=None)
    elif event_type == "DEBUG_CASE_OPENED":
        next_state["debug_cases"][payload["case_id"]] = dict(
            deepcopy(payload), status="open", evidence_ids=[]
        )
    elif event_type == "DEBUG_CASE_VERIFIED":
        next_state["debug_cases"][payload["case_id"]].update(
            status="verified", verification=deepcopy(payload)
        )
    elif event_type == "EVAL_PROMOTED":
        next_state["debug_cases"][payload["case_id"]].update(
            status="eval_promoted", eval_id=payload["eval_id"]
        )
        next_state["evals"][payload["eval_id"]] = deepcopy(payload)
    elif event_type == "HILLCLIMB_RECORDED":
        next_state["hillclimbs"].append(deepcopy(payload))
    elif event_type == "READINESS_RECORDED":
        # Validate on replay so a corrupted or hand-edited ledger cannot project
        # an unparseable or foreign receipt as if it were proof.
        receipt = ReadinessReceipt.from_dict(payload["receipt"])
        if receipt.run_id != event["run_id"]:
            raise ValueError(
                "READINESS_RECORDED receipt run_id {!r} does not match event run_id {!r}".format(
                    receipt.run_id, event["run_id"]
                )
            )
        if next_state["plan_digest"] is not None and receipt.plan_digest != next_state["plan_digest"]:
            raise ValueError("READINESS_RECORDED receipt is bound to a different plan digest")
        if receipt.task_id not in next_state["tasks"]:
            raise ValueError("READINESS_RECORDED receipt names unknown task {!r}".format(receipt.task_id))
        if receipt.worker_id not in next_state["agents"]:
            raise ValueError("READINESS_RECORDED receipt names unknown worker {!r}".format(receipt.worker_id))
        if receipt.receipt_id in next_state["readiness_receipts"]:
            raise ValueError("READINESS_RECORDED receipt id already exists: {}".format(receipt.receipt_id))
        next_state["readiness_receipts"][receipt.receipt_id] = receipt.to_dict()
    elif event_type == "TASK_WAITING":
        task_id = payload["task_id"]
        if task_id not in next_state["tasks"]:
            raise ValueError("TASK_WAITING names unknown task {!r}".format(task_id))
        task = next_state["tasks"][task_id]
        reason = WaitingReason.from_dict(payload["reason"])
        if reason.task_id != task_id:
            raise ValueError(
                "TASK_WAITING reason task_id {!r} does not match payload task_id {!r}".format(reason.task_id, task_id)
            )
        if task["status"] not in {"pending", "waiting"}:
            raise ValueError("only a pending or waiting task can enter a typed wait")
        task.update(status="waiting", waiting=reason.to_dict())
    elif event_type == "TASK_WAIT_CLEARED":
        task_id = payload["task_id"]
        if task_id not in next_state["tasks"]:
            raise ValueError("TASK_WAIT_CLEARED names unknown task {!r}".format(task_id))
        task = next_state["tasks"][task_id]
        if task["status"] != "waiting":
            raise ValueError("task is not waiting")
        task.update(status="pending", waiting=None)
    elif event_type == "ADMISSION_RECORDED":
        bundle = AdmissionBundle.from_dict(payload["bundle"])
        if bundle.digest() != payload.get("bundle_digest"):
            raise ValueError("ADMISSION_RECORDED bundle digest is invalid")
        if bundle.binding.run_id != event["run_id"] or bundle.binding.plan_digest != next_state["plan_digest"]:
            raise ValueError("ADMISSION_RECORDED belongs to another run or plan")
        if bundle.binding.task_id not in next_state["tasks"] or bundle.binding.worker_id not in next_state["agents"]:
            raise ValueError("ADMISSION_RECORDED names an unknown task or worker")
        if bundle.binding.box_id != bundle.binding.worker_id:
            raise ValueError("ADMISSION_RECORDED box id does not match its registered worker")
        recorded_receipt = next_state["readiness_receipts"].get(bundle.receipt.receipt_id)
        if recorded_receipt != bundle.receipt.to_dict():
            raise ValueError("ADMISSION_RECORDED readiness receipt was not recorded exactly")
        existing_reservation = next_state["reservations"].get(bundle.reservation.reservation_id)
        if existing_reservation is not None and existing_reservation != bundle.reservation.to_dict():
            raise ValueError("ADMISSION_RECORDED reservation id is already bound to another record")
        key = "{}:{}".format(bundle.binding.task_id, bundle.binding.box_id)
        next_state["admissions"][key] = bundle.to_dict()
        next_state["reservations"][bundle.reservation.reservation_id] = bundle.reservation.to_dict()
        next_state["readiness_receipts"][bundle.receipt.receipt_id] = bundle.receipt.to_dict()
    elif event_type == "RESERVATION_RELEASED":
        reservation_id = payload["reservation_id"]
        if reservation_id not in next_state["reservations"]:
            raise ValueError("RESERVATION_RELEASED names an unknown reservation")
        if reservation_id not in next_state["released_reservation_ids"]:
            next_state["released_reservation_ids"].append(reservation_id)
    elif event_type in {"TASK_LEASE_REJECTED", "LEASE_REVOKED"}:
        task = next_state["tasks"][payload["task_id"]]
        if task["lease_id"] != payload["lease_id"] or task["status"] not in {"leased", "running", "verifying"}:
            raise ValueError("{} does not match the active lease".format(event_type))
        agent = next_state["agents"].get(task.get("agent_id"))
        reason = WaitingReason.from_dict(payload["reason"])
        if reason.task_id != task["id"] or reason.box_id not in (None, task["agent_id"]):
            raise ValueError("{} reason does not match the active lease".format(event_type))
        task.update(
            status="waiting",
            waiting=reason.to_dict(),
            agent_id=None,
            lease_id=None,
            lease_fence=None,
            fence_digest=None,
            admission_digest=None,
        )
        if agent:
            agent.update(status="idle", task_id=None)
    elif event_type == "LEASE_HEARTBEAT":
        task = next_state["tasks"][payload["task_id"]]
        if (
            task["lease_id"] != payload["lease_id"]
            or task["fence_digest"] != payload["fence_digest"]
            or task["status"] not in {"leased", "running", "verifying"}
        ):
            raise ValueError("LEASE_HEARTBEAT does not match the active fenced lease")
        next_state["heartbeats"][payload["lease_id"]] = deepcopy(payload)
    elif event_type == "LEASE_RENEWED":
        task = next_state["tasks"][payload["task_id"]]
        fence = LeaseFence.from_dict(payload["fence"])
        if (
            fence.lease_id != task["lease_id"]
            or fence.task_id != task["id"]
            or fence.worker_id != task["agent_id"]
            or fence.epoch != next_state["lease_epochs"][task["id"]]
            or fence.digest() != payload.get("fence_digest")
        ):
            raise ValueError("LEASE_RENEWED does not match the active fence")
        task["lease_fence"] = fence.to_dict()
        task["fence_digest"] = fence.digest()
    elif event_type == "EFFECT_REQUESTED":
        record = EffectRequest.from_dict(payload)
        if record.run_id != event["run_id"]:
            raise ValueError("EFFECT_REQUESTED belongs to another run")
        task = next_state["tasks"].get(record.task_id)
        if (
            task is None
            or task["status"] != "running"
            or task["lease_id"] != record.lease_id
            or task["fence_digest"] != record.fence_digest
        ):
            raise ValueError("EFFECT_REQUESTED does not match the active fenced lease")
        normalized = record.to_dict()
        effect_id = record.effect_id
        if effect_id in next_state["effects"]:
            raise ValueError("EFFECT_REQUESTED effect id already exists")
        if any(
            item["idempotency_key"] == record.idempotency_key
            for item in next_state["effects"].values()
        ):
            raise ValueError("EFFECT_REQUESTED idempotency key already exists")
        next_state["effects"][effect_id] = normalized
    elif event_type in {"EFFECT_CONFIRMED", "EFFECT_REJECTED", "EFFECT_UNKNOWN"}:
        outcome = EffectOutcome.from_dict(payload)
        if outcome.state != event_type:
            raise ValueError("effect outcome state does not match its event type")
        effect = next_state["effects"].get(outcome.effect_id)
        if effect is None:
            raise ValueError("{} names an unknown effect".format(event_type))
        if effect["state"] not in {"EFFECT_REQUESTED", "EFFECT_UNKNOWN"}:
            raise ValueError("{} cannot transition a terminal effect".format(event_type))
        if effect["state"] == "EFFECT_UNKNOWN" and event_type != "EFFECT_UNKNOWN" and not outcome.reconciled:
            raise ValueError("an unknown effect requires provider readback reconciliation")
        effect.update(
            state=event_type,
            outcome_digest=outcome.outcome_digest,
            readback_digest=outcome.readback_digest,
            reconciled=outcome.reconciled,
            resolved_at=outcome.resolved_at,
        )
    elif event_type == "WORKSPACE_SALVAGED":
        fields = (
            "run_id", "task_id", "agent_id", "lease_id", "fence_digest",
            "reason", "salvage", "salvage_digest",
        )
        if not isinstance(payload, dict):
            raise ValueError("WORKSPACE_SALVAGED payload must be an object")
        reject_unknown_fields(payload, fields, "workspace salvaged event")
        missing = sorted(set(fields) - set(payload))
        if missing:
            raise ValueError("WORKSPACE_SALVAGED is missing fields: {}".format(", ".join(missing)))
        if payload["run_id"] != event["run_id"]:
            raise ValueError("WORKSPACE_SALVAGED belongs to another run")
        task = next_state["tasks"].get(payload["task_id"])
        if (
            task is None
            or task["status"] not in {"leased", "running", "verifying"}
            or task["agent_id"] != payload["agent_id"]
            or task["lease_id"] != payload["lease_id"]
            or task["fence_digest"] != require_digest(payload["fence_digest"], "salvage fence digest")
        ):
            raise ValueError("WORKSPACE_SALVAGED does not match the active fenced lease")
        require_string(payload["reason"], "salvage reason")
        salvage = SalvageReceipt.from_dict(payload["salvage"])
        if salvage.digest() != require_digest(payload["salvage_digest"], "salvage digest"):
            raise ValueError("WORKSPACE_SALVAGED salvage digest is invalid")
        admission = AdmissionBundle.from_dict(
            next_state["admissions"]["{}:{}".format(payload["task_id"], payload["agent_id"])]
        )
        if salvage.workspace_digest != admission.workspace.digest():
            raise ValueError("WORKSPACE_SALVAGED does not bind the admitted workspace")
        normalized = dict(payload)
        normalized["salvage"] = salvage.to_dict()
        next_state["salvages"].append(normalized)
    else:
        raise ValueError("projection does not handle {}".format(event_type))

    next_state["last_seq"] = event.get("seq", next_state["last_seq"] + 1)
    return next_state


def project(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    state = empty_state()
    for event in events:
        state = apply_event(state, event)
    return state
