"""Replayable within-lease budget waits; observations confer no spending grant."""

from copy import deepcopy

from .schema import canonical_digest, require_identifier


EVENTS = {"PROVIDER_BUDGET_WAITING", "PROVIDER_BUDGET_WAIT_CLEARED"}


def apply(state, event):
    payload = event["payload"]
    fields = {"task_id", "agent_id", "lease_id", "wait_digest"}
    fields |= {"pending", "code"} if event["type"] == "PROVIDER_BUDGET_WAITING" else {"reason"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("budget wait requires exact fields")
    for name in ("task_id", "agent_id", "lease_id"):
        require_identifier(payload[name], "budget wait " + name)
    task = state["tasks"].get(payload["task_id"])
    if (event["run_id"] != state["run_id"] or task is None or task["status"] != "running"
            or task["agent_id"] != payload["agent_id"] or task["lease_id"] != payload["lease_id"]):
        raise ValueError("budget wait does not match the running lease")
    if event["type"] == "PROVIDER_BUDGET_WAIT_CLEARED":
        if (not isinstance(payload["reason"], str) or payload["reason"] not in {"settlement_recheck", "cancelled", "restart_recheck"}
                or task.get("runtime_wait", {}).get("wait_digest") != payload["wait_digest"]):
            raise ValueError("budget wait clear does not match the retained wait")
        task.pop("runtime_wait")
        return
    if task.get("runtime_wait"):
        raise ValueError("a budget wait is already active")
    if payload["code"] != "BUDGET_RESERVED" or canonical_digest({key: value for key, value in payload.items() if key != "wait_digest"}) != payload["wait_digest"]:
        raise ValueError("budget wait digest or code is invalid")
    pending = payload["pending"]
    if not isinstance(pending, list) or not 1 <= len(pending) <= 1000:
        raise ValueError("budget wait needs a bounded pending invocation set")
    identities = set()
    for item in pending:
        if not isinstance(item, dict) or set(item) != {"invocation_id", "run_id", "task_id", "agent_id", "lease_id", "turn_number"}:
            raise ValueError("budget wait pending subject is malformed")
        for name in ("invocation_id", "run_id", "task_id", "agent_id", "lease_id"):
            require_identifier(item[name], "budget wait " + name)
        other = state["tasks"].get(item["task_id"])
        if (item["run_id"] != state["run_id"] or item["task_id"] == task["id"] or other is None
                or other["status"] != "running" or other["agent_id"] != item["agent_id"]
                or other["lease_id"] != item["lease_id"] or type(item["turn_number"]) is not int
                or item["turn_number"] != other["turn_count"] + 1 or item["invocation_id"] in identities):
            raise ValueError("budget wait names a stale or duplicate invocation subject")
        identities.add(item["invocation_id"])
    task["runtime_wait"] = deepcopy(payload)
