"""Read-only task/worker compatibility, not scheduling or admission authority."""

from .overview import _label
from .probes import Redactor
from .schema import canonical_digest


def delegation_snapshot(state, *, task_id=None, offset=0, limit=50, basis="ledger_snapshot"):
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200
            or basis not in {"ledger_snapshot", "plan_only"}):
        raise ValueError("delegation requires nonnegative offset and limit 1..200")
    if not state.get("run_id") or not state.get("runbook"):
        raise ValueError("delegation requires an exact run or an explicit plan-only view")
    if task_id is not None and (not isinstance(task_id, str) or task_id not in state["tasks"]):
        raise ValueError("delegation requires an exact task ID, not a pane number or prefix")
    agents, tasks = state["agents"], state["tasks"]
    rows = []
    if task_id is None:
        names = sorted(tasks)
        for name in names[offset:offset + limit]:
            task = tasks[name]
            required = set(task["capabilities"])
            rows.append(dict(task_id=name, status=task["status"], assigned_box=task.get("agent_id"),
                unmet_dependencies=[dep for dep in task["depends_on"] if tasks[dep]["status"] != "succeeded"],
                capability_matches=sum(required.issubset(agent["capabilities"]) for agent in agents.values())))
    else:
        names = sorted(agents)
        required = set(tasks[task_id]["capabilities"])
        for name in names[offset:offset + limit]:
            agent = agents[name]
            missing = sorted(required - set(agent["capabilities"]))
            rows.append(dict(box_id=name, recorded_status=agent["status"], current_task=agent.get("task_id"),
                capability_match=not missing, missing_capabilities=missing, adapter=agent["adapter"]["kind"]))
    result = dict(schema="camol.delegation_view", schema_version=1, run_id=state["run_id"],
        plan_digest=state["plan_digest"], event_cursor=state.get("last_seq", 0), basis=basis,
        task_id=task_id, run_status=state["status"], declared_box_count=len(agents),
        declared_concurrency_ceiling=state["runbook"]["run"]["max_concurrency"],
        compatibility_basis="declared_capabilities_only", readiness_proven=False,
        allocation_changed=False, automatic_execution=False,
        unchecked_admission=["authority", "workspace", "provider", "resources", "budget", "freshness", "dependencies", "gates"],
        page=dict(offset=offset, limit=limit, total=len(names), more=offset + limit < len(names)), rows=rows)
    if task_id is not None:
        task = tasks[task_id]
        result["task"] = dict(status=task["status"], assigned_box=task.get("agent_id"),
            required_capabilities=list(task["capabilities"]),
            resource_requirements=task.get("resource_requirements"),
            unmet_dependencies=[dep for dep in task["depends_on"] if tasks[dep]["status"] != "succeeded"])
    result = Redactor().value(result)
    return dict(result, snapshot_digest=canonical_digest(result))


def render_delegation(report):
    lines = ["DELEGATION VIEW | run={} | {} | cursor={}".format(
        _label(report["run_id"]), report["basis"], report["event_cursor"]),
        "{} declared boxes; concurrency ceiling {}. Capability matches are NOT readiness or reserved slots.".format(
            report["declared_box_count"], report["declared_concurrency_ceiling"]),
        "No allocation changed, probe, worker or model started. The kernel still checks every admission gate."]
    if report["task_id"] is None:
        lines.append("TASK                     STATE            ASSIGNED BOX             MATCHES / DEPENDENCIES")
        for row in report["rows"]:
            lines.append("{:<24} {:<16} {:<24} {} / {}".format(_label(row["task_id"], 24),
                _label(row["status"], 16), _label(row["assigned_box"] or "unassigned", 24),
                row["capability_matches"], _label(", ".join(row["unmet_dependencies"]) or "none", 80)))
        lines.append("Use /delegate EXACT_TASK_ID to inspect matching and excluded boxes.")
    else:
        lines.append("Task: " + _label(report["task_id"]) + " | " + _label(report["task"]["status"]))
        lines.append("BOX                      STATE            CURRENT TASK             CAPABILITY CHECK")
        for row in report["rows"]:
            lines.append("{:<24} {:<16} {:<24} {}".format(_label(row["box_id"], 24),
                _label(row["recorded_status"], 16), _label(row["current_task"] or "unassigned", 24),
                "matches (not admitted)" if row["capability_match"] else "missing " + _label(", ".join(row["missing_capabilities"]), 80)))
    if report["page"]["more"]:
        lines.append("More rows: --offset {} --limit {}".format(report["page"]["offset"] + report["page"]["limit"], report["page"]["limit"]))
    lines.append("New or redistributed work: /delegate --from RUNBOOK --reason 'WHY' reviews a stopped-run successor. /revise apply DIGEST approves it; /run is separate.")
    return "\n".join(lines)
