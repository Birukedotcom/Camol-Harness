"""Durable lease-bound worker evidence delivery, not worker execution authority.

The owner supplies one random 32-byte key per exact binding over an authenticated,
confidential enrollment channel. This module authenticates messages, not hosts;
it does not create a network listener or transfer execution grants.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .json_contracts import decode_contract
from .leases import effective_expiry
from .probes import Redactor
from .readiness import LeaseFence
from .schema import canonical_digest, parse_timestamp, require_identifier


FRAME_BYTES = 256 << 10
RECORD_BYTES = 32 << 10
MAX_BATCH = 6  # Includes JSON escaping overhead within the frame ceiling.
MAX_RECORDS = 10000
MAX_UNACKNOWLEDGED = 256
KINDS = frozenset({"heartbeat", "checkpoint", "tool", "artifact", "usage", "diagnostic", "turn_result"})
ZERO = "sha256:" + "0" * 64


class DeliveryError(ValueError):
    pass


def _bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as error:
        raise DeliveryError("worker delivery requires bounded finite JSON") from error


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise DeliveryError("worker delivery has missing or unknown fields")


def _mac_equal(value, expected):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None and hmac.compare_digest(value, expected)


def binding(value):
    _fields(value, {"schema", "schema_version", "control_plane_id", "worker_generation", "stream_id", "runtime_id", "fence"})
    if value["schema"] != "camol.worker_stream" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("unsupported worker stream binding")
    for name in ("control_plane_id", "worker_generation", "stream_id", "runtime_id"):
        require_identifier(value[name], name)
        if len(value[name]) > 128:
            raise DeliveryError("worker stream identity exceeds its bound")
    fence = LeaseFence.from_dict(value["fence"])
    if canonical_digest(fence.to_dict()) != canonical_digest(value["fence"]):
        raise DeliveryError("worker stream requires an exact canonical lease fence")
    if len(_bytes(value)) > 8192:
        raise DeliveryError("worker binding exceeds its byte ceiling")
    return deepcopy(value)


def authorize_state(state, stream, *, now):
    """Check a freshly read authoritative state; transport identity is separate.

    The embedding must serialize this check with lease mutation and receipt
    ingestion. An old snapshot or a worker-supplied state is not authorization.
    """
    stream = binding(stream)
    fence = LeaseFence.from_dict(stream["fence"])
    task = state["tasks"].get(fence.task_id)
    if (state["run_id"] != fence.run_id or state["plan_digest"] != fence.plan_digest
            or not state.get("approved_by") or state.get("terminal") is not None
            or task is None or task["status"] not in {"running", "verifying"}
            or task.get("lease_id") != fence.lease_id or task.get("agent_id") != fence.worker_id
            or task.get("fence_digest") != fence.digest()
            or canonical_digest(task.get("lease_fence")) != fence.digest()
            or state["lease_epochs"].get(fence.task_id) != fence.epoch):
        raise DeliveryError("worker stream no longer owns this active lease")
    current = parse_timestamp(now, "worker ingestion time")
    if not parse_timestamp(fence.issued_at, "issued") <= current < parse_timestamp(effective_expiry(state, task), "expiry"):
        raise DeliveryError("worker stream lease is expired or future dated")
    admission = state["admissions"][fence.task_id + ":" + fence.worker_id]
    if admission["receipt"]["runtime_id"] != stream["runtime_id"]:
        raise DeliveryError("worker stream runtime differs from admitted runtime")
    return True


def _private(path, directory=False):
    meta = path.lstat()
    if (not (stat.S_ISDIR(meta.st_mode) if directory else stat.S_ISREG(meta.st_mode))
            or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or (not directory and meta.st_nlink != 1)):
        raise DeliveryError("worker delivery storage must be private, owner-controlled and unlinked")


def read_enrollment_key(path):
    """Read one explicitly selected private raw key, without logging its value."""
    path = Path(path).absolute()
    if path.resolve() != path:
        raise DeliveryError("worker enrollment key cannot traverse symlinks")
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        meta = os.fstat(descriptor)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size != 32 or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
            raise DeliveryError("worker enrollment key must be a private 32-byte regular file")
        result = os.read(descriptor, 33)
        if len(result) != 32:
            raise DeliveryError("worker enrollment key changed while reading")
        return result
    except OSError as error:
        raise DeliveryError("explicit worker enrollment key is missing or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def key_redactor(key):
    forms = {key.hex(), key.hex().upper(), base64.b64encode(key).decode(), base64.urlsafe_b64encode(key).decode()}
    forms.update(value.rstrip("=") for value in tuple(forms))
    try:
        literal = key.decode("utf-8")
        if literal.isprintable():
            forms.add(literal)
    except UnicodeError:
        pass
    return Redactor(env={**os.environ, **{"CAMOL_ENROLLMENT_TOKEN_" + str(i): value for i, value in enumerate(sorted(forms))}})


class WorkerDelivery:
    """One private database for one enrolled stream and one direction.

    Producer and receiver use distinct directories. Exact retries are receipts,
    not repeated kernel events. A durable receipt acknowledges storage only;
    promotion into the kernel must independently validate worker claims.
    """

    def __init__(self, root, stream, key, *, role, create=False):
        self.stream = binding(stream)
        if type(key) is not bytes or len(key) != 32:
            raise DeliveryError("worker enrollment requires a random 32-byte key")
        if role not in {"producer", "receiver"} or type(create) is not bool:
            raise DeliveryError("invalid worker delivery role or creation flag")
        self._key, self.role = key, role
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise DeliveryError("worker delivery directory cannot traverse symlinks")
        if create:
            self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        self.path = self.root / "worker-delivery.sqlite3"
        self.scope = canonical_digest(self.stream)
        self._paths()
        if create:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            with self._db(initializing=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    db.execute("CREATE TABLE configuration (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)")
                    db.execute("CREATE TABLE records (seq INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, value TEXT NOT NULL)")
                    db.execute("CREATE TABLE progress (singleton INTEGER PRIMARY KEY CHECK(singleton=1), acknowledged INTEGER NOT NULL)")
                    db.execute("INSERT INTO configuration VALUES (1,?)", (_bytes(self._configuration()).decode(),))
                    db.execute("INSERT INTO progress VALUES (1,0)")
                    db.execute("PRAGMA user_version=1")
                self._check(db)
            fd = os.open(str(self.root), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            with self._db() as db:
                self._check(db)

    def _configuration(self):
        # A keyed scope verifier detects accidental wrong-key reopening. Never
        # store the enrollment key or a bearer credential in the database.
        return dict(stream=self.stream, role=self.role, key_check=self._mac("configuration", self.scope))

    def _mac(self, domain, value):
        return hmac.new(self._key, b"camol.worker.delivery.v1\0" + domain.encode() + b"\0" + _bytes(value), hashlib.sha256).hexdigest()

    def _redactor(self):
        return key_redactor(self._key)

    def _paths(self):
        _private(self.root, True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists() or path.is_symlink():
                try:
                    _private(path)
                except FileNotFoundError:
                    if suffix:
                        continue
                    raise
                if suffix in {"-wal", "-shm"}:
                    raise DeliveryError("unexpected worker delivery journal mode")

    def _check(self, db):
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise DeliveryError("unsupported worker delivery database")
        row = db.execute("SELECT value FROM configuration WHERE singleton=1").fetchone()
        if row is None or not hmac.compare_digest(row[0], _bytes(self._configuration()).decode()):
            raise DeliveryError("worker delivery scope, role or enrollment key changed")

    @contextmanager
    def _db(self, *, initializing=False, write=False):
        db = None
        try:
            self._paths()
            _private(self.path)
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + ("rw" if write or initializing else "ro"), uri=True, timeout=1, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write or initializing else "BEGIN")
            if not initializing:
                self._check(db)
            yield db
            db.execute("COMMIT")
        except (OSError, sqlite3.Error) as error:
            raise DeliveryError("worker delivery storage is unavailable, unsafe, busy or corrupt") from error
        finally:
            if db is not None:
                db.close()  # Also rolls back any uncommitted batch.

    def _role(self, expected):
        if self.role != expected:
            raise DeliveryError("worker delivery operation is invalid for this role")

    def _record(self, value):
        _fields(value, {"scope", "seq", "previous", "kind", "occurred_at", "data", "digest"})
        if (value["scope"] != self.scope or type(value["seq"]) is not int or not 1 <= value["seq"] <= MAX_RECORDS
                or not isinstance(value["kind"], str) or value["kind"] not in KINDS or not isinstance(value["data"], dict)
                or len(_bytes(value)) > RECORD_BYTES):
            raise DeliveryError("invalid worker delivery record")
        parse_timestamp(value["occurred_at"], "worker reported time")
        expected = canonical_digest({k: v for k, v in value.items() if k != "digest"})
        if value["digest"] != expected:
            raise DeliveryError("worker delivery record digest changed")
        return value

    def queue(self, kind, data, *, occurred_at):
        self._role("producer")
        # Apply capture policy before signing, never reinterpret history against
        # a later credential environment. Receiver rejects new unredacted input.
        data = self._redactor().value(deepcopy(data))
        with self._db(write=True) as db:
            last = db.execute("SELECT seq,digest FROM records ORDER BY seq DESC LIMIT 1").fetchone() or (0, ZERO)
            acknowledged = db.execute("SELECT acknowledged FROM progress WHERE singleton=1").fetchone()[0]
            if last[0] - acknowledged >= MAX_UNACKNOWLEDGED:
                raise DeliveryError("worker evidence backpressure: drain the durable outbox before more work")
            value = dict(scope=self.scope, seq=last[0] + 1, previous=last[1], kind=kind, occurred_at=occurred_at, data=data)
            value["digest"] = canonical_digest(value)
            self._record(value)
            db.execute("INSERT INTO records VALUES (?,?,?)", (value["seq"], value["digest"], _bytes(value).decode()))
        return value

    def batch(self):
        self._role("producer")
        with self._db() as db:
            cursor = db.execute("SELECT acknowledged FROM progress WHERE singleton=1").fetchone()[0]
            values = [self._record(decode_contract(row[0], max_bytes=RECORD_BYTES)) for row in db.execute(
                "SELECT value FROM records WHERE seq>? ORDER BY seq LIMIT ?", (cursor, MAX_BATCH))]
        unsigned = dict(schema="camol.worker_batch", schema_version=1, scope=self.scope, records=values)
        raw = _bytes(dict(unsigned, mac=self._mac("batch", unsigned)))
        if len(raw) > FRAME_BYTES:
            raise DeliveryError("worker batch exceeds its byte ceiling")
        return raw

    def validate_batch(self, raw):
        """Authenticate bounded input without taking a storage or kernel lock."""
        self._role("receiver")
        message = decode_contract(raw, max_bytes=FRAME_BYTES)
        _fields(message, {"schema", "schema_version", "scope", "records", "mac"})
        unsigned = {k: v for k, v in message.items() if k != "mac"}
        if (message["schema"] != "camol.worker_batch" or type(message["schema_version"]) is not int or message["schema_version"] != 1
                or message["scope"] != self.scope or not isinstance(message["records"], list) or len(message["records"]) > MAX_BATCH
                or not _mac_equal(message["mac"], self._mac("batch", unsigned))):
            raise DeliveryError("unauthenticated, foreign or oversized worker batch")
        values = [self._record(value) for value in message["records"]]
        if any(right["seq"] != left["seq"] + 1 or right["previous"] != left["digest"] for left, right in zip(values, values[1:])):
            raise DeliveryError("worker batch must contain a contiguous hash chain")
        return values

    def accept(self, raw, *, authorize):
        self._role("receiver")
        if not callable(authorize):
            raise DeliveryError("worker ingestion requires an authoritative lease guard")
        values = self.validate_batch(raw)
        with self._db(write=True) as db:
            last = db.execute("SELECT seq,digest FROM records ORDER BY seq DESC LIMIT 1").fetchone() or (0, ZERO)
            for value in values:
                if value["seq"] <= last[0]:
                    old = db.execute("SELECT value FROM records WHERE seq=?", (value["seq"],)).fetchone()
                    if old is None or old[0] != _bytes(value).decode():
                        raise DeliveryError("worker sequence was reused with different evidence")
                    continue
                if value["seq"] != last[0] + 1 or value["previous"] != last[1]:
                    raise DeliveryError("worker stream has a gap or changed predecessor")
                if self._redactor().value(value["data"]) != value["data"]:
                    raise DeliveryError("new worker evidence violates capture redaction policy")
                if authorize(deepcopy(self.stream)) is not True:
                    raise DeliveryError("worker stream lease guard denied new evidence")
                db.execute("INSERT INTO records VALUES (?,?,?)", (value["seq"], value["digest"], _bytes(value).decode()))
                last = value["seq"], value["digest"]
            receipt = dict(schema="camol.worker_ack", schema_version=1, scope=self.scope, cursor=last[0], digest=last[1],
                           meaning="durably_received_untrusted_worker_evidence_not_kernel_acceptance")
        # Only produce the acknowledgment after the transaction commits.
        return _bytes(dict(receipt, mac=self._mac("ack", receipt)))

    def acknowledge(self, raw):
        self._role("producer")
        receipt = decode_contract(raw, max_bytes=4096)
        _fields(receipt, {"schema", "schema_version", "scope", "cursor", "digest", "meaning", "mac"})
        unsigned = {k: v for k, v in receipt.items() if k != "mac"}
        if (receipt["schema"] != "camol.worker_ack" or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["scope"] != self.scope or type(receipt["cursor"]) is not int or not 0 <= receipt["cursor"] <= MAX_RECORDS
                or receipt["meaning"] != "durably_received_untrusted_worker_evidence_not_kernel_acceptance"
                or not _mac_equal(receipt["mac"], self._mac("ack", unsigned))):
            raise DeliveryError("invalid worker delivery acknowledgment")
        with self._db(write=True) as db:
            cursor = db.execute("SELECT acknowledged FROM progress WHERE singleton=1").fetchone()[0]
            row = db.execute("SELECT digest FROM records WHERE seq=?", (receipt["cursor"],)).fetchone() if receipt["cursor"] else (ZERO,)
            if row is None or row[0] != receipt["digest"]:
                raise DeliveryError("worker acknowledgment names unqueued or changed evidence")
            cursor = max(cursor, receipt["cursor"])
            db.execute("UPDATE progress SET acknowledged=? WHERE singleton=1", (cursor,))
        return cursor

    def inspect(self, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("invalid worker inspection cursor or limit")
        with self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            cursor = db.execute("SELECT acknowledged FROM progress WHERE singleton=1").fetchone()[0]
            rows = [self._record(decode_contract(row[0], max_bytes=RECORD_BYTES)) for row in db.execute(
                "SELECT value FROM records WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]
        return dict(scope=self.scope, role=self.role, count=count, acknowledged=cursor if self.role == "producer" else None,
                    unacknowledged=count - cursor if self.role == "producer" else None,
                    records=rows, next_seq=rows[-1]["seq"] if rows else after, more=count > (rows[-1]["seq"] if rows else after),
                    execution_authority=False, kernel_promoted=False)


def deliver_once(producer, transport):
    """One owner-initiated exchange; lost replies leave a retryable outbox.

    The transport must provide confidentiality and bounded/cancellable I/O.
    Reopening the producer after failure retains its exact outstanding records.
    """
    if not callable(transport):
        raise DeliveryError("worker delivery requires an explicit transport")
    return producer.acknowledge(transport(producer.batch()))
