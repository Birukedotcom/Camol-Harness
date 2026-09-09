"""Owner-side, exact-run box snapshots for detached and embedded clients.

Reading a pane establishes neither live connectivity nor execution authority.
No workspace command, model request, process launch or credential probe occurs.
"""

import io
import os
import sqlite3
import stat
import time
from pathlib import Path

from .artifacts import artifact_refs
from .json_contracts import decode_contract
from .probes import Redactor
from .retention import _RootContext, RetentionError
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest, require_identifier, require_timestamp
from .state import project


class BoxInspectionError(ValueError):
    pass


def _read_preview(root, reference, allowance):
    if reference.stored_bytes > 1 << 20 or reference.stored_bytes > allowance[0]:
        raise BoxInspectionError("artifact preview exceeds the byte allowance")
    hex_digest = reference.digest.split(":", 1)[1]
    destination = io.BytesIO()
    _, digest, size = root._read_file(Path("artifacts") / "sha256" / hex_digest[:2] / hex_digest[2:],
                                     1 << 20, sink=destination)
    if digest != reference.digest or size != reference.stored_bytes:
        raise BoxInspectionError("artifact preview hash or size differs from its reference")
    allowance[0] -= size
    return destination.getvalue()


def read_artifact_preview(state_dir, reference, allowance):
    with _RootContext(Path(state_dir)) as root:
        result = _read_preview(root, reference, allowance)
        root.verify()
        return result


def box_snapshot(run_id, runbook, state, events, box_id, *, after_seq=0, limit=200, tail=False, preview_reader=None):
    """Pure association and pagination over one already-validated event cut."""
    if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000 or type(tail) is not bool:
        raise BoxInspectionError("box cursor must be nonnegative and limit must be 1..1000")
    worker = next((item for item in runbook["agents"] if item["id"] == box_id), None)
    if worker is None:
        raise BoxInspectionError("unknown box id; an exact ID is required")
    owners, historical, relevant, workspaces = {}, set(), [], {}
    for event in events:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        # Actor names alone are not assignment identity (a user or worker may
        # literally be named 'orchestrator'). Explicit subjects are required.
        task_id = payload.get("task_id")
        named = next((payload[name] for name in ("agent_id", "worker_id", "box_id") if isinstance(payload.get(name), str)), None)
        if task_id and named:
            owners[task_id] = named
            if named == box_id:
                historical.add(task_id)
        bundle = payload.get("bundle", {}) if event.get("type") == "ADMISSION_RECORDED" else {}
        binding = bundle.get("binding", {})
        if binding.get("run_id") == run_id and binding.get("box_id") == box_id:
            historical.add(binding["task_id"])
            receipt = bundle["workspace"]
            workspaces[binding["task_id"]] = dict(workspace_id=receipt["workspace_id"], task_id=binding["task_id"],
                box_id=box_id, run_id=run_id, path=receipt["path"], branch=receipt["branch"], integration=False,
                basis="recorded_admission_not_live_probe", recorded_at_seq=event["seq"])
        mentioned = [payload[name] for name in ("task_id", "evaluated_task_id", "from_task_id", "to_task_id")
                     if isinstance(payload.get(name), str)]
        associated = (named == box_id or (binding.get("run_id") == run_id and binding.get("box_id") == box_id)
                      or (named is None and any(owners.get(task) == box_id for task in mentioned)))
        if event.get("type") in {"BOX_MESSAGE_POSTED", "BOX_MESSAGE_DELIVERED", "BOX_MESSAGE_CONSUMED", "BOX_MESSAGE_SEND_REJECTED", "BOX_PEER_READ_RECORDED"}:
            target = payload.get("target", {}).get("subject", payload.get("subject", {}))
            sender = payload.get("sender", {}).get("subject") or {}
            associated = associated or any(item.get("run_id") == run_id and item.get("box_id") == box_id for item in (target, sender))
        if event.get("type") in {"BOX_PEER_CALL_STARTED", "BOX_PEER_CALL_FINISHED"}:
            caller = payload.get("subject") or state.get("peer_tool_calls", {}).get(payload.get("call_id"), {}).get("start", {}).get("subject", {})
            associated = associated or (caller.get("run_id") == run_id and caller.get("box_id") == box_id)
        if event.get("type") in {"WORKER_STREAM_ENROLLED", "WORKER_STREAM_REVOKED", "WORKER_STREAM_IMPORTED"}:
            enrollment = payload.get("proposal") or state.get("worker_streams", {}).get(payload.get("scope"), {}).get("proposal", {})
            subject = enrollment.get("stream", {}).get("fence", {})
            associated = associated or (subject.get("run_id") == run_id and subject.get("box_id") == box_id)
        if associated and event.get("seq", 0) > after_seq:
            relevant.append(event)
    task_ids = sorted(task_id for task_id, task in state["tasks"].items() if task.get("agent_id") == box_id)
    historical.update(task_ids)
    selected = relevant[-limit:] if tail else relevant[:limit]
    previews, references = {}, {}
    for event in selected:
        for reference in artifact_refs(event):
            # Equal content may have distinct producers; retain the last exact
            # reference, never a synthetic merged producer record.
            references[reference.digest] = reference
    for reference in list(references.values())[-16:]:
        entry = dict(reference=reference.to_dict())
        producer = reference.producer
        if (producer.get("run_id") != run_id or producer.get("task_id") not in historical
                or producer.get("agent_id") not in {None, box_id}):
            entry["error"] = "artifact subject is outside this run/box task history"
        elif not reference.redacted:
            entry["error"] = "raw artifact withheld from ordinary pane preview"
        elif preview_reader is not None:
            try:
                content = preview_reader(reference)
                text = Redactor().text(content.decode("utf-8", "replace"))
                text = "".join(char if char.isprintable() or char in "\n\t" else "\\x{:02x}".format(ord(char)) for char in text)
                entry.update(preview=text[:16000], preview_transform="utf8-replace,credential-redaction,control-escaping",
                             preview_truncated=len(content) > 16000 or len(text) > 16000)
            except (OSError, ValueError, RuntimeError) as error:
                entry["error"] = type(error).__name__
        else:
            entry["error"] = "artifact preview not requested"
        previews[reference.digest] = entry
    current_task = state.get("agents", {}).get(box_id, {}).get("task_id")
    ordered_workspaces = sorted(workspaces.values(), key=lambda item: item["recorded_at_seq"])
    primary = workspaces.get(current_task) or (ordered_workspaces[-1] if ordered_workspaces else None)
    result = dict(schema="camol.control_box", schema_version=1, run_id=run_id, plan_digest=state.get("plan_digest"),
        box_id=box_id, role=worker["role"], capabilities=worker["capabilities"], adapter_kind=worker["adapter"]["kind"],
        task_ids=task_ids, eligible_task_ids=sorted(task["id"] for task in runbook["tasks"]
            if set(task["capabilities"]).issubset(worker["capabilities"])),
        task_states={task: state["tasks"][task] for task in sorted(historical) if task in state["tasks"]},
        task_contracts=[task for task in runbook["tasks"] if task["id"] in historical],
        workspace=primary, workspaces=ordered_workspaces, events=selected, artifacts=previews,
        next_seq=selected[-1]["seq"] if selected else after_seq,
        observation=dict(basis="durable_ledger_snapshot", event_cursor=state.get("last_seq", 0),
            live_connection_proven=False, task_readiness_proven=False, historical_task_ids=sorted(historical),
            matched_events=len(relevant), more_events=len(relevant) > len(selected), tail=tail))
    if state.get("box_messages"):
        from .mailbox import inbox
        # A retained cut is not a live connection check. Its message statuses
        # are evaluated at the cut's last recorded time, preserving stable reads.
        result["mailbox"] = dict(inbox(state, box_id, events[-1]["occurred_at"]), basis="retained_event_cut_not_live_delivery_proof")
    if state.get("box_message_failures"):
        result["message_send_failures"] = [item for item in state["box_message_failures"].values() if item["subject"]["box_id"] == box_id][-100:]
    result = Redactor().value(result)
    return dict(result, snapshot_digest=canonical_digest(result))


class BoxInspector:
    """Read-only API. The database and CAS must belong to one explicit owner state.

    Cold ledgers use immutable SQLite reads. Live WAL reads may maintain SQLite
    shared-memory coordination, but never append events, initialize or repair data.
    """

    def __init__(self, state_dir, *, database=None, max_events=20000, max_event_bytes=2 << 20, max_total_bytes=64 << 20):
        self.state_dir = Path(state_dir)
        self.database = Path(database) if database is not None else self.state_dir / "camol.sqlite3"
        if not self.state_dir.is_absolute() or not self.database.is_absolute():
            raise BoxInspectionError("box inspection requires absolute state and database paths")
        if self.state_dir.is_symlink() or self.database.is_symlink():
            raise BoxInspectionError("selected box state and database cannot be symlinks")
        # Permit OS aliases such as /var and /tmp without following the selected
        # leaf itself. The descriptor reader rechecks canonical traversal.
        self.state_dir = self.state_dir.parent.resolve() / self.state_dir.name
        self.database = self.database.parent.resolve() / self.database.name
        try:
            self.database.relative_to(self.state_dir)
        except ValueError as error:
            raise BoxInspectionError("box database must be inside its exact state directory") from error
        for value, ceiling in ((max_events, 100000), (max_event_bytes, 8 << 20), (max_total_bytes, 256 << 20)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise BoxInspectionError("box snapshot limits exceed their supported bounds")
        self.max_events, self.max_event_bytes, self.max_total_bytes = max_events, max_event_bytes, max_total_bytes

    def _cut(self, run_id):
        store = None
        try:
            require_identifier(run_id, "box run id")
            # Descriptor traversal rejects linked state ancestry and special files.
            with _RootContext(self.state_dir) as root:
                parent = self.database.parent
                while parent != self.state_dir:
                    if parent.is_symlink() or not parent.is_dir():
                        raise BoxInspectionError("linked box database ancestry")
                    parent = parent.parent
                info = self.database.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise BoxInspectionError("box database must be a single-link regular file")
                wal = Path(str(self.database) + "-wal")
                for sidecar in (wal, Path(str(self.database) + "-shm")):
                    if sidecar.exists() or sidecar.is_symlink():
                        side = sidecar.lstat()
                        if not stat.S_ISREG(side.st_mode) or side.st_nlink != 1 or side.st_uid != os.getuid() or side.st_mode & 0o022:
                            raise BoxInspectionError("linked or unsafe SQLite sidecars are not inspected")
                if not wal.exists() or wal.stat().st_size == 0:
                    connection = sqlite3.connect(self.database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
                else:
                    connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)
                store = connection
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                deadline = time.monotonic() + 5
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
                connection.execute("BEGIN")
                measure = "+".join("COALESCE(LENGTH(CAST({} AS BLOB)),0)".format(name) for name in
                    ("payload_json", "run_id", "event_id", "event_type", "actor_id", "occurred_at", "causation_id", "correlation_id"))
                count, largest, total = connection.execute(
                    "SELECT COUNT(*), COALESCE(MAX({0}),0), COALESCE(SUM({0}),0) FROM events WHERE run_id=?".format(measure), (run_id,)).fetchone()
                if not count or count > self.max_events or largest > self.max_event_bytes or total > self.max_total_bytes:
                    raise BoxInspectionError("exact run is missing or exceeds the bounded box snapshot limits")
                events = []
                for row in connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)):
                    event = dict(run_id=row["run_id"], seq=row["seq"], event_id=row["event_id"], type=row["event_type"],
                                 actor_id=row["actor_id"], occurred_at=row["occurred_at"],
                                 payload=decode_contract(row["payload_json"], max_bytes=self.max_event_bytes))
                    for name in ("causation_id", "correlation_id"):
                        if row[name]:
                            event[name] = require_identifier(row[name], "box event " + name)
                    for name in ("event_id", "actor_id"):
                        require_identifier(event[name], "box event " + name)
                    require_timestamp(event["occurred_at"], "box event timestamp")
                    if event["seq"] != len(events) + 1:
                        raise BoxInspectionError("box ledger has a noncontiguous sequence")
                    events.append(event)
                if events[0]["type"] != "RUN_CREATED" or sum(event["type"] == "RUN_CREATED" for event in events) != 1:
                    raise BoxInspectionError("box ledger requires exactly one initial run creation")
                state = project(events)
                document = validate_runbook(state["runbook"])
                if state["run_id"] != run_id or runbook_digest(document) != state["plan_digest"]:
                    raise BoxInspectionError("box ledger does not bind its exact run and plan")
                root.verify()
                after = self.database.lstat()
                if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                    raise BoxInspectionError("box database changed identity during inspection")
                return state, events, root.identity
        except BoxInspectionError:
            raise
        except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError) as error:
            raise BoxInspectionError("exact box ledger is unavailable or invalid; no state was initialized") from error
        finally:
            if store is not None:
                store.close()

    def list(self, run_id):
        state, events, identity = self._cut(run_id)
        rows = [dict(box_id=key, role=agent["role"], status=agent["status"], task_id=agent.get("task_id"),
                     task_states={task_id: task["status"] for task_id, task in state["tasks"].items() if task.get("agent_id") == key},
                     adapter_kind=agent["adapter"]["kind"]) for key, agent in sorted(state["agents"].items())]
        report = Redactor().value(dict(schema="camol.box_list", schema_version=1, run_id=run_id,
            plan_digest=state["plan_digest"], event_cursor=state["last_seq"], live_connection_proven=False, boxes=rows))
        return dict(report, snapshot_digest=canonical_digest(report))

    def resolve(self, run_id, box_id):
        report = self.list(run_id)
        exact = [row for row in report["boxes"] if row["box_id"] == box_id]
        if len(exact) != 1:
            raise BoxInspectionError("box resolution requires one exact ID, never a prefix, label or pane number")
        report.pop("snapshot_digest")
        report["boxes"] = exact
        return dict(report, snapshot_digest=canonical_digest(report))

    def read(self, run_id, box_id, *, after_seq=0, limit=200, tail=False, previews=True):
        if type(previews) is not bool:
            raise BoxInspectionError("previews must be a boolean")
        state, events, identity = self._cut(run_id)
        allowance = [8 << 20]
        try:
            with _RootContext(self.state_dir) as root:
                if root.identity != identity:
                    raise BoxInspectionError("box state root changed between ledger and artifact inspection")
                def preview(reference):
                    return _read_preview(root, reference, allowance)
                result = box_snapshot(run_id, state["runbook"], state, events, box_id, after_seq=after_seq,
                                      limit=limit, tail=tail, preview_reader=preview if previews else None)
                root.verify()
        except (OSError, RetentionError) as error:
            raise BoxInspectionError("box artifact state is unavailable or changed") from error
        return result
