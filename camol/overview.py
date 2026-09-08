"""Content-free, deterministic fleet/task views shared by terminal and API users.

These projections never discover terminals, start work, or establish readiness.
The durable run/task/box identities are independent of presentation pane numbers.
"""

from collections import Counter
from .probes import Redactor
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest


def _label(value, width=64):
    text = Redactor().text(str(value))
    return "".join(char if char.isprintable() else " " for char in text)[:width]


def planned_state(runbook):
    document = validate_runbook(runbook)
    return dict(run_id=document["run"]["id"], plan_digest=runbook_digest(document),
        runbook=document, status="not_started", last_seq=0,
        agents={item["id"]: dict(item, status="dormant", task_id=None) for item in document["agents"]},
        tasks={item["id"]: dict(item, status="not_started", agent_id=None) for item in document["tasks"]})


def fleet_overview(state, *, attention=False, offset=0, limit=50, basis="ledger_snapshot"):
    if type(attention) is not bool or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("overview requires nonnegative offset and limit 1..200")
    if basis not in {"ledger_snapshot", "plan_only"} or not state.get("run_id") or not state.get("runbook"):
        raise ValueError("overview requires one exact run or a declared plan-only view")
    tasks = []
    for task_id, task in sorted(state["tasks"].items()):
        dependencies = list(task["depends_on"])
        unmet = [name for name in dependencies if state["tasks"][name]["status"] != "succeeded"]
        blocker = task.get("blocker") or {}
        waiting = task.get("waiting") or {}
        gate = task.get("gate_wait") or {}
        needs_attention = bool(blocker or waiting or gate or task["status"] in {"blocked", "waiting"})
        tasks.append(dict(task_id=task_id, status=task["status"], box_id=task.get("agent_id"),
            dependencies=dependencies, unmet_dependencies=unmet,
            attention=needs_attention, blocker_kind=blocker.get("kind"),
            waiting_code=waiting.get("code"), gate_phase=gate.get("phase"),
            turns=task.get("turn_count", 0), attempts=task.get("attempts", 0)))
    boxes = []
    for box_id, agent in sorted(state["agents"].items()):
        profile = agent["adapter"].get("profile_snapshot", {})
        boxes.append(dict(box_id=box_id, status=agent["status"], task_id=agent.get("task_id"),
            adapter=agent["adapter"]["kind"], requested_model=profile.get("requested_model"),
            model_identity="requested_only" if profile.get("requested_model") else "not_observed",
            attention=any(item["attention"] and item["box_id"] == box_id for item in tasks), turns=agent.get("stats", {}).get("turns", 0),
            tokens=agent.get("stats", {}).get("tokens", 0),
            readiness="not_established_by_overview"))
    selected_boxes = [item for item in boxes if not attention or item["attention"]]
    selected_tasks = [item for item in tasks if not attention or item["attention"]]
    result = dict(schema="camol.fleet_overview", schema_version=1, run_id=state["run_id"],
        plan_digest=state["plan_digest"], run_status=state["status"], basis=basis,
        event_cursor=state.get("last_seq", 0), live_connection_proven=False,
        counts=dict(boxes=len(boxes), tasks=len(tasks), task_states=dict(sorted(Counter(item["status"] for item in tasks).items()))),
        page=dict(offset=offset, limit=limit, attention_only=attention,
                  matched_boxes=len(selected_boxes), matched_tasks=len(selected_tasks),
                  more_boxes=offset + limit < len(selected_boxes), more_tasks=offset + limit < len(selected_tasks)),
        boxes=selected_boxes[offset:offset + limit], tasks=selected_tasks[offset:offset + limit])
    # Metadata labels may still come from untrusted runbook/worker identities.
    result = Redactor().value(result)
    return dict(result, snapshot_digest=canonical_digest(result))


def render_overview(report):
    lines = ["CAMOL OVERVIEW | run={} state={} cursor={}".format(
        _label(report["run_id"]), _label(report["run_status"]), report["event_cursor"]),
        "{} | {} boxes / {} tasks | snapshot, not a live-readiness receipt".format(
            report["basis"], report["counts"]["boxes"], report["counts"]["tasks"]),
        "", "BOX                     STATE           TASK                    ADAPTER"]
    for item in report["boxes"]:
        lines.append("{} {:<22} {:<15} {:<23} {}".format("!" if item["attention"] else " ",
            _label(item["box_id"], 22), _label(item["status"], 15),
            _label(item["task_id"] or "unassigned", 23), _label(item["adapter"], 20)))
    lines += ["", "TASK                    STATE           BOX                     WAIT / DEPENDENCIES"]
    for item in report["tasks"]:
        reason = item["blocker_kind"] or item["waiting_code"] or ("gate:" + str(item["gate_phase"]) if item["gate_phase"] else None)
        reason = reason or ("after " + ", ".join(item["unmet_dependencies"]) if item["unmet_dependencies"] else "-")
        lines.append("{} {:<22} {:<15} {:<23} {}".format("!" if item["attention"] else " ",
            _label(item["task_id"], 22), _label(item["status"], 15), _label(item["box_id"] or "unassigned", 23), _label(reason, 80)))
    page = report["page"]
    if page["more_boxes"] or page["more_tasks"]:
        lines.append("More rows: --offset {} --limit {} (offset applies to each table)".format(page["offset"] + page["limit"], page["limit"]))
    lines.append("Use /box ID VIEW for details. Layout changes never start, stop, or approve a task.")
    return "\n".join(lines)
