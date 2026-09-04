"""Restart-safe SQLite append-only event storage."""

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConcurrentAppendError(RuntimeError):
    """The stream advanced after a caller made its state-based decision."""


class SQLiteEventStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_file(self.path, create=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """
        )
        self._harden_database_files()
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def append(self, event: Dict[str, Any], *, expected_seq: Optional[int] = None) -> Dict[str, Any]:
        return self.append_many([event], expected_seq=expected_seq)[0]

    def append_many(
        self, events: List[Dict[str, Any]], *, expected_seq: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if not events:
            return []
        run_id = events[0]["run_id"]
        if any(event["run_id"] != run_id for event in events):
            raise ValueError("one append transaction cannot span runs")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current_seq = self.connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            if expected_seq is not None and current_seq != expected_seq:
                raise ConcurrentAppendError(
                    "event stream advanced from expected seq {} to {}".format(expected_seq, current_seq)
                )
            appended = []
            for offset, event in enumerate(events, 1):
                sequence = current_seq + offset
                payload_json = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))
                self.connection.execute(
                    """
                    INSERT INTO events (
                        run_id, seq, event_id, event_type, actor_id, occurred_at,
                        causation_id, correlation_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["run_id"],
                        sequence,
                        event["event_id"],
                        event["type"],
                        event["actor_id"],
                        event["occurred_at"],
                        event.get("causation_id"),
                        event.get("correlation_id"),
                        payload_json,
                    ),
                )
                appended.append(dict(event, seq=sequence))
            self._harden_database_files()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return appended

    @staticmethod
    def _secure_file(path: Path, *, create: bool) -> None:
        """Require an owner-controlled regular file and force owner-only access."""

        if path.is_symlink():
            raise OSError("event-store files must not be symlinks: {}".format(path))
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(path), flags, 0o600)
        except FileNotFoundError:
            if create:
                raise
            return
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("event-store path is not a regular file: {}".format(path))
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise OSError("event-store file is not owned by the current user: {}".format(path))
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _harden_database_files(self) -> None:
        self._secure_file(self.path, create=False)
        self._secure_file(Path(str(self.path) + "-wal"), create=False)
        self._secure_file(Path(str(self.path) + "-shm"), create=False)

    def read(self, run_id: str, after_seq: int = 0) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def latest_run_id(self) -> Optional[str]:
        row = self.connection.execute(
            "SELECT run_id FROM events ORDER BY occurred_at DESC, seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def has_run(self, run_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM events WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
        event = {
            "run_id": row["run_id"],
            "seq": row["seq"],
            "event_id": row["event_id"],
            "type": row["event_type"],
            "actor_id": row["actor_id"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload_json"]),
        }
        if row["causation_id"]:
            event["causation_id"] = row["causation_id"]
        if row["correlation_id"]:
            event["correlation_id"] = row["correlation_id"]
        return event
