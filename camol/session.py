"""Durable, non-secret state for the interactive Camol client.

Interactive planning exists before a kernel run has a runbook, so it has its own
small state record.  The record is intentionally outside the repository and is
never a source of lease or completion truth; once execution starts, the kernel
ledger and supervised control socket remain authoritative.
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

from .probes import Redactor
from .schema import canonical_digest, reject_unknown_fields, require_schema_header


class SessionError(RuntimeError):
    """Interactive session state is missing, unsafe, or malformed."""


SESSION_SCHEMA = "camol.interactive_session"
SESSION_VERSION = 1
SESSION_FIELDS = (
    "schema", "schema_version", "session_id", "workspace", "state_dir",
    "status", "model", "effort", "goal", "grill", "plan", "plan_digest",
    "approved_digest", "run_id", "messages", "created_at", "updated_at",
    "selected_box", "event_cursor",
)
SESSION_STATUSES = frozenset({"new", "planning", "plan_ready", "approved", "running", "terminal"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
MESSAGE_ROLES = frozenset({"human", "orchestrator", "system"})
MAX_MESSAGE_CHARS = 256_000
MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_HISTORY_READ_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_ROW_BYTES = 4 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def default_state_root() -> Path:
    override = os.environ.get("CAMOL_STATE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Camol").resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "camol").resolve()
    return (Path.home() / ".local" / "state" / "camol").resolve()


def workspace_key(workspace: Path) -> str:
    resolved = Path(workspace).resolve()
    name = "".join(character if character.isalnum() or character in "-." else "-" for character in resolved.name)
    name = name.strip("-.") or "workspace"
    suffix = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return "{}-{}".format(name[:48], suffix)


def project_state_dir(workspace: Path, root: Optional[Path] = None) -> Path:
    return (Path(root or default_state_root()).resolve() / "projects" / workspace_key(workspace)).resolve()


def _message(value: Mapping[str, Any]) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise SessionError("interactive message must be an object")
    reject_unknown_fields(value, ("message_id", "role", "content", "created_at", "kind"), "interactive message")
    if set(value) != {"message_id", "role", "content", "created_at", "kind"}:
        raise SessionError("interactive message is missing fields")
    if value["role"] not in MESSAGE_ROLES:
        raise SessionError("interactive message role is invalid")
    if value["kind"] not in {"conversation", "command", "notice", "error"}:
        raise SessionError("interactive message kind is invalid")
    for name in ("message_id", "content", "created_at"):
        if not isinstance(value[name], str) or not value[name]:
            raise SessionError("interactive message {} is required".format(name))
    if len(value["content"]) > MAX_MESSAGE_CHARS:
        raise SessionError("interactive message is too large")
    return dict(value)


def _history_tail(path: Path, count: int):
    """Read a bounded recent view, not an integrity scan of the entire archive."""
    from .json_contracts import decode_contract

    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise SessionError("interactive transcript must be an owner-private regular file")
        offset, data, selected = before.st_size, b"", []
        while offset:
            amount = min(offset, 65536, MAX_HISTORY_READ_BYTES - len(data))
            if amount == 0:
                raise SessionError("recent transcript exceeds the 8 MiB inspection ceiling; request fewer entries")
            offset -= amount
            block = os.pread(descriptor, amount, offset)
            if len(block) != amount:
                raise SessionError("interactive transcript changed during inspection")
            data = block + data
            lines = data.split(b"\n")
            # Unless at byte zero, the first row may start before this buffer.
            eligible = lines[1:] if offset else lines
            selected = [line for line in eligible if line.strip()][-count:]
            if len(selected) == count:
                break
        if data and not data.endswith(b"\n"):
            raise SessionError("interactive transcript has an incomplete final record")
        after = os.fstat(descriptor)
        names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_uid", "st_mode")
        if any(getattr(before, name) != getattr(after, name) for name in names):
            raise SessionError("interactive transcript changed during inspection")
        try:
            return [_message(decode_contract(line, max_bytes=MAX_TRANSCRIPT_ROW_BYTES)) for line in selected]
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise SessionError("recent interactive transcript is malformed") from error
    finally:
        os.close(descriptor)


def validate_session(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionError("interactive session must be an object")
    require_schema_header(value, SESSION_SCHEMA, SESSION_VERSION, "interactive session")
    reject_unknown_fields(value, SESSION_FIELDS, "interactive session")
    if set(value) != set(SESSION_FIELDS):
        raise SessionError("interactive session is missing fields")
    for name in ("session_id", "workspace", "state_dir", "model", "effort", "created_at", "updated_at"):
        if not isinstance(value[name], str) or not value[name]:
            raise SessionError("interactive session {} is required".format(name))
    if value["status"] not in SESSION_STATUSES:
        raise SessionError("interactive session status is invalid")
    if value["effort"] not in EFFORTS:
        raise SessionError("interactive effort is invalid")
    workspace = Path(value["workspace"])
    state_dir = Path(value["state_dir"])
    if not workspace.is_absolute() or not state_dir.is_absolute():
        raise SessionError("interactive workspace and state directory must be absolute")
    for name in ("goal", "plan_digest", "approved_digest", "run_id"):
        if value[name] is not None and not isinstance(value[name], str):
            raise SessionError("interactive session {} must be text or null".format(name))
    if value["selected_box"] is not None and not isinstance(value["selected_box"], str):
        raise SessionError("interactive selected_box must be text or null")
    if type(value["event_cursor"]) is not int or value["event_cursor"] < 0:
        raise SessionError("interactive event_cursor must be a non-negative integer")
    if value["plan"] is not None and not isinstance(value["plan"], dict):
        raise SessionError("interactive plan must be an object or null")
    if value["grill"] is not None and not isinstance(value["grill"], dict):
        raise SessionError("interactive grill state must be an object or null")
    if isinstance(value["grill"], dict) and value["grill"].get("schema") == "camol.creation_draft":
        from .draft_creation import validate_state
        try:
            validate_state(value["grill"])
        except (ValueError, RuntimeError, TypeError, KeyError) as error:
            raise SessionError("interactive creation draft is malformed: " + str(error)) from error
    if value["plan"] is None and value["plan_digest"] is not None:
        raise SessionError("interactive plan digest requires a plan")
    if value["plan"] is not None and canonical_digest(value["plan"]) != value["plan_digest"]:
        raise SessionError("interactive plan digest is invalid")
    if value["approved_digest"] is not None and value["approved_digest"] != value["plan_digest"]:
        raise SessionError("approved digest does not match the current plan")
    if not isinstance(value["messages"], list) or len(value["messages"]) > 500:
        raise SessionError("interactive session messages must be an array of at most 500 items")
    normalized = dict(value)
    normalized["messages"] = [_message(item) for item in value["messages"]]
    return normalized


@dataclass
class SessionStore:
    workspace: Path
    root: Optional[Path] = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.project_dir = project_state_dir(self.workspace, self.root)
        self.path = self.project_dir / "session.json"
        self.runs_dir = self.project_dir / "runs"
        self.transcript_path = self.project_dir / "transcript.jsonl"
        self.planning_calls_path = self.project_dir / "planning-calls.jsonl"
        self.proposal_events_path = self.project_dir / "proposal-events.jsonl"

    @contextmanager
    def transaction(self):
        """Serialize clients sharing one project, including separate terminals."""
        import fcntl
        self._ensure_root()
        path = self.project_dir / "session.lock"
        if path.is_symlink():
            raise SessionError("interactive session lock must not be a symlink")
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SessionError("another client is updating this project; retry after its request finishes") from error
            yield
        finally:
            os.close(descriptor)

    def _append_jsonl(self, path: Path, value: Mapping[str, Any]) -> None:
        self._ensure_root()
        if path.is_symlink():
            raise SessionError("interactive log must not be a symlink")
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_planning_call(self, value: Mapping[str, Any]) -> None:
        self._append_jsonl(self.planning_calls_path, Redactor().value(dict(value)))

    def planning_calls(self):
        return self._read_jsonl(self.planning_calls_path)

    def append_proposal_event(self, value: Mapping[str, Any]) -> None:
        self._append_jsonl(self.proposal_events_path, Redactor().value(dict(value)))

    def proposal_events(self):
        return self._read_jsonl(self.proposal_events_path)

    def _read_jsonl(self, path: Path):
        if not path.exists():
            return []
        if path.is_symlink():
            raise SessionError("interactive log must not be a symlink")
        try:
            with path.open(encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except (OSError, ValueError) as error:
            raise SessionError("interactive log is malformed") from error

    def history(self, fallback, count: int = 20):
        # Legacy sessions have no archive. Seed it on the first subsequent write.
        if type(count) is not int or not 1 <= count <= 101:
            raise SessionError("history count must be an integer from 1 to 101")
        try:
            return _history_tail(self.transcript_path, count)
        except FileNotFoundError:
            return [_message(item) for item in list(fallback)[-count:]]
        except OSError as error:
            raise SessionError("interactive transcript is missing, unsafe or unreadable") from error

    def _ensure_root(self) -> None:
        for path in (self.project_dir, self.runs_dir):
            if path.exists() and path.is_symlink():
                raise SessionError("interactive state directories must not be symlinks")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    def create(self) -> Dict[str, Any]:
        self._ensure_root()
        timestamp = _now()
        session_id = "session-" + uuid4().hex
        state_dir = (self.runs_dir / session_id).resolve()
        record = {
            "schema": SESSION_SCHEMA,
            "schema_version": SESSION_VERSION,
            "session_id": session_id,
            "workspace": str(self.workspace),
            "state_dir": str(state_dir),
            "status": "new",
            "model": "manual",
            "effort": "high",
            "goal": None,
            "grill": None,
            "plan": None,
            "plan_digest": None,
            "approved_digest": None,
            "run_id": None,
            "selected_box": None,
            "event_cursor": 0,
            "messages": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.save(record)
        return record

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file() or self.path.is_symlink():
            return self.create()
        if self.path.stat().st_size > MAX_SESSION_BYTES:
            raise SessionError("interactive session file is too large")
        try:
            return validate_session(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            if isinstance(error, SessionError):
                raise
            raise SessionError("interactive session file is malformed") from error

    def save(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        self._ensure_root()
        record = dict(value)
        record["updated_at"] = _now()
        record = validate_session(record)
        serialized = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        while len(serialized) > MAX_SESSION_BYTES and record["messages"]:
            record["messages"] = record["messages"][1:]
            serialized = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(serialized) > MAX_SESSION_BYTES:
            raise SessionError("interactive plan and settings exceed the durable session limit")
        descriptor, temporary = tempfile.mkstemp(prefix="session.", dir=str(self.project_dir))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized.decode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(str(temporary_path), str(self.path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return record

    def update(self, value: Mapping[str, Any], **changes: Any) -> Dict[str, Any]:
        record = dict(value)
        record.update(changes)
        return self.save(record)

    def append_message(self, value: Mapping[str, Any], role: str, content: str, *, kind: str = "conversation") -> Dict[str, Any]:
        safe = Redactor().text(content)
        message = _message({
            "message_id": "message-" + uuid4().hex,
            "role": role,
            "content": safe,
            "created_at": _now(),
            "kind": kind,
        })
        record = dict(value)
        if not self.transcript_path.exists():
            for previous in record["messages"]:
                self._append_jsonl(self.transcript_path, previous)
        self._append_jsonl(self.transcript_path, message)
        record["messages"] = (list(record["messages"]) + [message])[-500:]
        return self.save(record)

    def runbook_path(self, value: Mapping[str, Any]) -> Path:
        run_id = value.get("run_id") or "draft"
        return self.project_dir / ("{}.runbook.json".format(run_id))

    def write_runbook(self, value: Mapping[str, Any], runbook: Mapping[str, Any]) -> Path:
        self._ensure_root()
        path = self.runbook_path(value)
        descriptor, temporary = tempfile.mkstemp(prefix="runbook.", dir=str(self.project_dir))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(runbook, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return path
