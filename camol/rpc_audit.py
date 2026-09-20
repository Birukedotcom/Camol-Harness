"""Private content-free RPC measurements, separate from mutation authority."""

import fcntl
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .ssh_protocol import ALL_COMMANDS, MUTATING_COMMANDS, SSHTransportError
from .schema import parse_timestamp, require_digest


MAX_RECORDS = 1000000
STATUSES = {"prepared", "dispatched", "completed", "rejected", "unknown"}
COLUMNS = ("seq", "request_id", "profile_digest", "command", "mutating", "status", "created_at", "finished_at",
           "elapsed_ms", "request_bytes", "stdout_bytes", "stderr_bytes")


def _private(path, *, directory=False):
    metadata = path.lstat()
    if (not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
            or (not directory and metadata.st_nlink != 1)):
        raise OSError("unsafe audit path")


class RPCAudit:
    """Owner-controlled directory, no raw params, replies, banners or credentials.

    Prepared/dispatched rows after interruption stay visibly unfinished. This
    audit cannot clear the independent mutation journal's uncertainty holds.
    """

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.path = self.root / "rpc-audit.sqlite3"

    def _paths(self):
        _private(self.root, directory=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists() or path.is_symlink():
                try:
                    _private(path)
                except FileNotFoundError:
                    if suffix:
                        continue  # A concurrent SQLite commit removed its journal.
                    raise
                if suffix in {"-wal", "-shm"}:
                    raise OSError("unexpected audit journal mode")

    @contextmanager
    def _database(self, *, write=False):
        lock, connection = None, None
        try:
            self._paths()
            if write:
                lock_path = self.root / "rpc-audit.lock"
                lock = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                _private(lock_path)
                deadline = time.monotonic() + 1
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise OSError("audit writer is busy")
                        time.sleep(.01)
                new = False
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    os.close(fd)
                    new = True
                except FileExistsError:
                    pass
                self._paths()
                connection = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
                connection.execute("PRAGMA synchronous=FULL")
                if new:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE calls (seq INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, profile_digest TEXT NOT NULL, command TEXT NOT NULL, mutating INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, request_bytes INTEGER, stdout_bytes INTEGER, stderr_bytes INTEGER)")
                    connection.execute("CREATE INDEX calls_profile ON calls(profile_digest, seq)")
                    connection.execute("PRAGMA user_version=1")
                    connection.execute("COMMIT")
            else:
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise OSError("unsupported audit schema")
            if tuple(row[1] for row in connection.execute("PRAGMA table_info(calls)")) != COLUMNS:
                raise OSError("invalid audit columns")
            yield connection
        except (OSError, sqlite3.Error) as error:
            raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit is unavailable, busy, unsafe or requires inspection") from error
        finally:
            if connection is not None:
                connection.close()
            if lock is not None:
                os.close(lock)

    def start(self, record, *, mutating):
        if (not isinstance(record.get("request_id"), str) or not re.fullmatch(r"ssh-[0-9a-f]{32}", record["request_id"])
                or record.get("command") not in ALL_COMMANDS or type(mutating) is not bool
                or mutating != (record["command"] in MUTATING_COMMANDS)):
            raise SSHTransportError("POLICY_DENIED", "invalid RPC audit identity or command")
        require_digest(record["target_digest"], "RPC profile digest")
        parse_timestamp(record["created_at"], "RPC start")
        with self._database(write=True) as db:
            if db.execute("SELECT COALESCE(MAX(seq),0) FROM calls").fetchone()[0] >= MAX_RECORDS:
                raise SSHTransportError("AUDIT_FULL", "RPC audit reached its retention ceiling; explicitly archive before more calls")
            db.execute("INSERT INTO calls(request_id,profile_digest,command,mutating,status,created_at) VALUES (?,?,?,?,?,?)",
                       (record["request_id"], record["target_digest"], record["command"], int(mutating), "prepared", record["created_at"]))

    def dispatched(self, request_id):
        with self._database(write=True) as db:
            if db.execute("UPDATE calls SET status='dispatched' WHERE request_id=? AND status='prepared'", (request_id,)).rowcount != 1:
                raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit has no exact prepared call")

    def finish(self, record, *, elapsed_ms, request_bytes, stdout_bytes, stderr_bytes):
        if record["status"] not in {"completed", "rejected", "unknown"}:
            raise SSHTransportError("POLICY_DENIED", "RPC audit requires a terminal transport outcome")
        parse_timestamp(record["finished_at"], "RPC finish")
        if any(value is not None and (type(value) is not int or not 0 <= value < 2**63)
               for value in (elapsed_ms, request_bytes, stdout_bytes, stderr_bytes)):
            raise SSHTransportError("POLICY_DENIED", "invalid RPC measurement")
        with self._database(write=True) as db:
            if db.execute("UPDATE calls SET status=?,finished_at=?,elapsed_ms=?,request_bytes=?,stdout_bytes=?,stderr_bytes=? WHERE request_id=? AND status IN ('prepared','dispatched')",
                          (record["status"], record.get("finished_at"), elapsed_ms, request_bytes, stdout_bytes, stderr_bytes, record["request_id"])).rowcount != 1:
                raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit cannot finish this exact pending call")

    def inspect(self, profile_digest, *, after=0, limit=100):
        require_digest(profile_digest, "RPC profile digest")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise SSHTransportError("POLICY_DENIED", "RPC usage cursor/limit is invalid")
        report = dict(schema="camol.rpc_usage", schema_version=1, profile_digest=profile_digest,
                      basis="local_transport_measurement_not_provider_billing", provider_tokens=None, provider_cost=None,
                      coverage="recorded_validated_requests_only; legacy and pre-validation denials are unmeasured",
                      calls=0, by_command=[], entries=[], next_seq=after, more=False)
        if not self.path.exists() and not self.path.is_symlink():
            if self.root.exists() or self.root.is_symlink():
                try:
                    self._paths()
                except OSError as error:
                    raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit directory is unsafe or unavailable") from error
            return report
        with self._database() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT command,status,COUNT(*),COUNT(elapsed_ms),COALESCE(SUM(elapsed_ms),0),MAX(elapsed_ms),COUNT(request_bytes),COALESCE(SUM(request_bytes),0),COUNT(stdout_bytes),COALESCE(SUM(stdout_bytes),0),COUNT(stderr_bytes),COALESCE(SUM(stderr_bytes),0) FROM calls WHERE profile_digest=? GROUP BY command,status ORDER BY command,status", (profile_digest,)).fetchall()
            for row in rows:
                command, status, count, measured, elapsed, maximum, *sizes = row
                if command not in ALL_COMMANDS or status not in STATUSES or any(type(value) is not int or value < 0 for value in (count, measured, elapsed) + tuple(sizes)):
                    raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit measurements are invalid")
                report["calls"] += count
                report["by_command"].append(dict(command=command, status=status, calls=count,
                    elapsed_ms=dict(known_calls=measured, known_total=elapsed, maximum=maximum),
                    **{name: dict(known_calls=sizes[index * 2], known_total=sizes[index * 2 + 1])
                       for index, name in enumerate(("request_bytes", "stdout_bytes", "stderr_bytes"))}))
            selected = db.execute("SELECT * FROM calls WHERE profile_digest=? AND seq>? ORDER BY seq LIMIT ?", (profile_digest, after, limit + 1)).fetchall()
            report["entries"] = [dict(zip(COLUMNS, row)) for row in selected[:limit]]
            for entry in report["entries"]:
                try:
                    if (not isinstance(entry["request_id"], str) or not re.fullmatch(r"ssh-[0-9a-f]{32}", entry["request_id"])
                            or entry["mutating"] not in (0, 1) or entry["status"] not in STATUSES or entry["command"] not in ALL_COMMANDS
                            or bool(entry["mutating"]) != (entry["command"] in MUTATING_COMMANDS)):
                        raise ValueError("invalid entry")
                    require_digest(entry["profile_digest"], "RPC profile digest")
                    parse_timestamp(entry["created_at"], "RPC start")
                    if entry["finished_at"] is not None:
                        parse_timestamp(entry["finished_at"], "RPC finish")
                    for name in ("elapsed_ms", "request_bytes", "stdout_bytes", "stderr_bytes"):
                        if entry[name] is not None and (type(entry[name]) is not int or entry[name] < 0):
                            raise ValueError("invalid measurement")
                except (ValueError, TypeError) as error:
                    raise SSHTransportError("AUDIT_UNAVAILABLE", "RPC audit contains an invalid measurement entry") from error
            report["next_seq"] = report["entries"][-1]["seq"] if report["entries"] else after
            report["more"] = len(selected) > limit
        return report
