"""Metadata-only pane navigation; selection never grants execution authority."""

from .probes import Redactor
from .schema import canonical_digest


def pane_scope(session, state):
    return canonical_digest(dict(session_id=session["session_id"], workspace=session["workspace"],
        state_dir=session["state_dir"], run_id=session["run_id"], product_plan_digest=session["plan_digest"],
        run_plan_digest=state["plan_digest"], boxes=sorted(state["agents"])))


def switch_snapshot(session, state, basis, organization=None):
    scope = pane_scope(session, state)
    organization = organization or dict(scope=scope, pins=[], groups={})
    if organization["scope"] != scope:
        raise ValueError("pane organization differs from the selected scope")
    pin_order = {box: index for index, box in enumerate(organization["pins"])}
    rows = [dict(key="orchestrator", box_id=None, label="ORCHESTRATOR", group="orchestrator",
                 status=state["status"], task_id="planning / coordination", adapter="human + planner")]
    for box_id, agent in state["agents"].items():
        task_id = agent.get("task_id")
        task = state["tasks"].get(task_id, {})
        attention = bool(task.get("waiting") or task.get("runtime_wait") or task.get("blocker") or task.get("gate_wait")
                         or task.get("status") in {"blocked", "waiting"})
        rows.append(dict(key="box:" + box_id, box_id=box_id, label=box_id,
            group="attention" if attention else "workers", status=task.get("status", agent["status"]),
            pinned=box_id in pin_order, custom_group=organization["groups"].get(box_id, ""),
            runtime_wait=task.get("runtime_wait", {}).get("code", ""),
            task_id=task_id or "unassigned", adapter=agent["adapter"]["kind"]))
    # Routing retains exact identities; labels alone are sanitized for display.
    redactor = Redactor()
    for row in rows:
        for name in ("label", "status", "task_id", "adapter", "custom_group", "runtime_wait"):
            if name not in row:
                continue
            row[name] = "".join(c if c.isprintable() else " " for c in redactor.text(row[name]))
    rows.sort(key=lambda row: (0 if row["group"] == "orchestrator" else 1 if row["group"] == "attention" else 2 if row.get("pinned") else 3,
        pin_order.get(row["box_id"], MAX_ORDER), row.get("custom_group", "").casefold(), row["key"]))
    return dict(scope=scope, run_id=state["run_id"], basis=basis, event_cursor=state.get("last_seq", 0), rows=rows)


def filter_rows(rows, query):
    if len(query) > 256:
        raise ValueError("box search is limited to 256 characters")
    words = query.casefold().split()
    return [row for row in rows if all(word in " ".join(str(row.get(name, "")) for name in
            ("label", "group", "custom_group", "status", "runtime_wait", "task_id", "adapter")).casefold() for word in words)]


def row_label(row):
    return "{}{}{} | {} | {} | {} | {}".format("★ " if row.get("pinned") else "", row["group"],
        " / " + row["custom_group"] if row.get("custom_group") else "", row["label"],
        row["status"] + (" (" + row["runtime_wait"] + ")" if row.get("runtime_wait") else ""), row["task_id"], row["adapter"])


MAX_ORDER = 1001
