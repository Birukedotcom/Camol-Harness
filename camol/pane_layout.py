"""Metadata-only tiled monitor projection, usable without Textual or tmux."""

from .pane_switcher import switch_snapshot
from .probes import Redactor


def _text(value):
    return "".join(char if char.isprintable() else " " for char in Redactor().text(str(value)))[:160]


def monitor_snapshot(session, state, basis, organization=None):
    result = switch_snapshot(session, state, basis, organization)
    result["rows"] = [row for row in result["rows"] if row["box_id"] is not None]
    for row in result["rows"]:
        agent = state["agents"][row["box_id"]]
        task = state["tasks"].get(agent.get("task_id"), {})
        wait = task.get("runtime_wait") or task.get("waiting") or task.get("blocker") or {}
        gate = task.get("gate_wait") or {}
        row["wait"] = _text(wait.get("code") or wait.get("kind") or "none")
        row["gate"] = _text(gate.get("phase") or ("pending" if gate else "none"))
        stats = agent.get("stats", {})
        row["tokens"] = stats.get("tokens", 0)
        row["turns"] = stats.get("turns", 0)
    return result


def page(snapshot, mode, number, width, height):
    if mode not in {"split", "grid"} or type(number) is not int or number < 1:
        raise ValueError("monitor needs split/grid and a positive page number")
    columns = 2 if width >= 90 else 1
    rows = 1 if mode == "split" or height < 30 else 2 if height < 42 else 3
    size = columns * rows
    pages = max(1, (len(snapshot["rows"]) + size - 1) // size)
    number = min(number, pages)
    start = (number - 1) * size
    return dict(number=number, pages=pages, columns=columns, rows=rows,
                items=snapshot["rows"][start:start + size], total=len(snapshot["rows"]))


def tile_label(row, cursor):
    return "{}{} | {}\ntask: {}\ngate: {} · wait: {}\n{} tokens · {} turns · cursor {}".format(
        "★ " if row.get("pinned") else "", _text(row["box_id"]), _text(row["status"]),
        _text(row["task_id"]), row["gate"], row["wait"], row["tokens"], row["turns"], cursor)
