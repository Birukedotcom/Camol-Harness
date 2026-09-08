"""Metadata-only pane navigation; selection never grants execution authority."""

from .probes import Redactor
from .schema import canonical_digest


def switch_snapshot(session, state, basis):
    scope = canonical_digest(dict(session_id=session["session_id"], workspace=session["workspace"],
        state_dir=session["state_dir"], run_id=session["run_id"], product_plan_digest=session["plan_digest"],
        run_plan_digest=state["plan_digest"], boxes=sorted(state["agents"])))
    rows = [dict(key="orchestrator", box_id=None, label="ORCHESTRATOR", group="orchestrator",
                 status=state["status"], task_id="planning / coordination", adapter="human + planner")]
    for box_id, agent in state["agents"].items():
        task_id = agent.get("task_id")
        task = state["tasks"].get(task_id, {})
        attention = bool(task.get("waiting") or task.get("blocker") or task.get("gate_wait")
                         or task.get("status") in {"blocked", "waiting"})
        rows.append(dict(key="box:" + box_id, box_id=box_id, label=box_id,
            group="attention" if attention else "workers", status=task.get("status", agent["status"]),
            task_id=task_id or "unassigned", adapter=agent["adapter"]["kind"]))
    # Routing retains exact identities; labels alone are sanitized for display.
    redactor = Redactor()
    for row in rows:
        for name in ("label", "status", "task_id", "adapter"):
            row[name] = "".join(c if c.isprintable() else " " for c in redactor.text(row[name]))
    rows.sort(key=lambda row: (0 if row["group"] == "orchestrator" else 1 if row["group"] == "attention" else 2, row["key"]))
    return dict(scope=scope, run_id=state["run_id"], basis=basis, event_cursor=state.get("last_seq", 0), rows=rows)


def filter_rows(rows, query):
    if len(query) > 256:
        raise ValueError("box search is limited to 256 characters")
    words = query.casefold().split()
    return [row for row in rows if all(word in " ".join(str(row[name]) for name in
            ("label", "group", "status", "task_id", "adapter")).casefold() for word in words)]


def row_label(row):
    return "{} | {} | {} | {} | {}".format(row["group"], row["label"], row["status"], row["task_id"], row["adapter"])
