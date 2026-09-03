"""Deterministic projections rebuilt from the event ledger."""

from copy import deepcopy
from typing import Any, Dict, Iterable


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
        task.update(status="leased", agent_id=payload["agent_id"], lease_id=payload["lease_id"])
        agent.update(status="leased", task_id=payload["task_id"])
    elif event_type == "TASK_STARTED":
        task = next_state["tasks"][payload["task_id"]]
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
        next_state["evidence"][payload["evidence_id"]] = deepcopy(payload)
        task_id = payload.get("task_id")
        if task_id:
            next_state["tasks"][task_id]["evidence_ids"].append(payload["evidence_id"])
        debug_case_id = payload.get("debug_case_id")
        if debug_case_id:
            next_state["debug_cases"][debug_case_id]["evidence_ids"].append(payload["evidence_id"])
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
        task.update(status="succeeded", lease_id=None)
        agent.update(status="idle", task_id=None)
        agent["stats"]["successful_tasks"] += 1
    elif event_type == "TASK_RETRY_SCHEDULED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"][task["agent_id"]]
        task.update(status="pending", agent_id=None, lease_id=None)
        task["last_retry_reason"] = payload["reason"]
        agent.update(status="idle", task_id=None)
        agent["stats"]["failed_attempts"] += 1
    elif event_type == "TASK_BLOCKED":
        task = next_state["tasks"][payload["task_id"]]
        agent = next_state["agents"].get(task["agent_id"])
        task.update(status="blocked", blocker=deepcopy(payload["blocker"]), lease_id=None)
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
    else:
        raise ValueError("projection does not handle {}".format(event_type))

    next_state["last_seq"] = event.get("seq", next_state["last_seq"] + 1)
    return next_state


def project(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    state = empty_state()
    for event in events:
        state = apply_event(state, event)
    return state
