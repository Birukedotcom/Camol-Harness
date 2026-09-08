"""Exclusive durable provider intent; uncertainty never authorizes a duplicate."""

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from .adapter import AdapterError
from .schema import canonical_digest


def _publish_once(path, payload):
    """Publish fully fsynced bytes atomically, without replacing an old intent."""
    path = Path(path)
    raw = (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = path.parent / (".invocation-" + uuid4().hex + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class InvocationJournal:
    """Reservation and terminal observations are separate immutable records."""

    def __init__(self, path, *, packet_sha256, profile_digest, assignment, run_id, turn_number, workspace):
        self.path = Path(path)
        self.outcome_path = self.path.with_suffix(".observed.json")
        self.binding = dict(run_id=run_id, task_id=assignment["task_id"], agent_id=assignment["agent_id"],
                            lease_id=assignment["lease_id"], fence_digest=assignment.get("fence_digest"),
                            packet_sha256=packet_sha256, profile_digest=profile_digest,
                            turn_number=turn_number, workspace=str(Path(workspace).resolve()))

    def _payload(self, evidence):
        return {"schema": "camol.provider_invocation", "schema_version": 1,
                "binding": self.binding, "binding_digest": canonical_digest(self.binding), "observed_evidence": evidence}

    def _read(self, path):
        try:
            descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invocation is not a regular file")
                raw = stream.read((32 << 20) + 1)
            if len(raw) > 32 << 20:
                raise ValueError("oversized record")
            payload = json.loads(raw)
            expected = self._payload(payload["observed_evidence"])
            if payload != expected or not isinstance(payload["observed_evidence"], list):
                raise ValueError("different invocation identity")
            return payload
        except (ValueError, KeyError, OSError, TypeError) as error:
            raise AdapterError("provider invocation record is invalid or belongs to another subject; inspect before any retry") from error

    def reserve(self, evidence):
        try:
            _publish_once(self.path, self._payload(evidence))
        except FileExistsError:
            original = self._read(self.path)
            previous = self._read(self.outcome_path) if self.outcome_path.exists() else original
            raise AdapterError("provider invocation was already launched without a recoverable successful result; reconcile before retrying",
                               observed_evidence=previous["observed_evidence"])

    def record_outcome(self, evidence):
        payload = self._payload(evidence)
        try:
            _publish_once(self.outcome_path, payload)
        except FileExistsError:
            if self._read(self.outcome_path) != payload:
                raise AdapterError("provider invocation already has a different terminal observation")
