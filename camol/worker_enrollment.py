"""Owner-reviewed enrollment for exact worker evidence streams, not machines."""

import hashlib
import os
import secrets
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from itertools import islice

from .schema import canonical_digest, parse_timestamp, require_digest
from .worker_delivery import DeliveryError, WorkerDelivery, authorize_state, binding, read_enrollment_key, key_redactor, _bytes, _fields, _private
from .json_contracts import decode_contract
from .probes import Redactor


EVENTS = frozenset({"WORKER_STREAM_ENROLLED", "WORKER_STREAM_REVOKED"})
MAX_STREAMS = 1000


def _owner(state, by):
    if not isinstance(by, str) or not by or by != state.get("approved_by") or by in state["agents"]:
        raise DeliveryError("only the exact approved human owner may enroll or revoke worker streams")


def proposal(state, stream, key_digest, *, by):
    _owner(state, by)
    stream = binding(stream)
    require_digest(key_digest, "worker enrollment key commitment")
    value = dict(schema="camol.worker_enrollment", schema_version=1, stream=stream,
                 scope=canonical_digest(stream), key_digest=key_digest, approved_owner=by,
                 permission="receive_untrusted_worker_evidence_only", execution_authority=False)
    return dict(value, digest=canonical_digest(value))


def _validate_proposal(state, value, by):
    if not isinstance(value, dict):
        raise DeliveryError("worker enrollment requires an exact proposal")
    try:
        expected = proposal(state, value["stream"], value["key_digest"], by=by)
    except KeyError as error:
        raise DeliveryError("worker enrollment proposal is incomplete") from error
    if canonical_digest(expected) != canonical_digest(value):
        raise DeliveryError("worker enrollment proposal or digest changed")
    return expected


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise DeliveryError("worker enrollment event belongs to another run")
    payload = event["payload"]
    records = state.get("worker_streams", {})
    if event["type"] == "WORKER_STREAM_ENROLLED":
        _fields(payload, {"proposal", "review_digest", "enrolled_at"})
        value = _validate_proposal(state, payload["proposal"], event["actor_id"])
        if payload["review_digest"] != value["digest"]:
            raise DeliveryError("worker enrollment requires exact human review")
        authorize_state(state, value["stream"], now=payload["enrolled_at"])
        if value["scope"] in records or len(records) >= MAX_STREAMS:
            raise DeliveryError("duplicate worker stream or enrollment history ceiling")
        lease_id = value["stream"]["fence"]["lease_id"]
        if any(row["status"] == "active" and row["proposal"]["stream"]["fence"]["lease_id"] == lease_id for row in records.values()):
            raise DeliveryError("this lease already has an active worker stream; revoke it before replacing")
        state.setdefault("worker_streams", {})[value["scope"]] = dict(proposal=value, status="active",
            enrolled_at=payload["enrolled_at"], revoked_at=None, reason=None)
    else:
        _fields(payload, {"scope", "proposal_digest", "revoked_at", "reason"})
        record = records.get(payload["scope"])
        if record is None or record["status"] != "active" or record["proposal"]["digest"] != payload["proposal_digest"]:
            raise DeliveryError("worker revocation requires an active exact enrolled stream")
        if parse_timestamp(payload["revoked_at"], "worker revocation") < parse_timestamp(record["enrolled_at"], "worker enrollment"):
            raise DeliveryError("worker revocation predates enrollment")
        if not isinstance(payload["reason"], str) or not 1 <= len(payload["reason"]) <= 2048 or any(not c.isprintable() for c in payload["reason"]):
            raise DeliveryError("invalid worker revocation reason")
        record.update(status="revoked", revoked_at=payload["revoked_at"], reason=payload["reason"])


class WorkerEnrollment:
    """Use inside the controller owner context. No network transport is implicit.

    Preparing private material never enrolls a worker. Exact approval records the
    public commitment; the raw key is kept in separate private control storage.
    A revoked or expired stream cannot receive even duplicate batches through
    this service. Its old spool remains inspectable with explicit owner access.
    """

    def __init__(self, orchestrator, run_id, state_dir):
        self.orchestrator, self.run_id = orchestrator, run_id
        self.state_dir = Path(state_dir).absolute()
        if self.state_dir.resolve() != self.state_dir:
            raise DeliveryError("worker enrollment storage cannot traverse symlinks")
        _private(self.state_dir, True)
        try:
            orchestrator.store.path.resolve().relative_to(self.state_dir)
        except ValueError as error:
            raise DeliveryError("worker enrollment must use the kernel's own state directory") from error
        self.root = self.state_dir / "worker-enrollments"

    def _directory(self, scope):
        require_digest(scope, "worker scope")
        return self.root / scope.split(":")[1]

    def _material(self, value):
        _private(self.root, True)
        directory = self._directory(value["scope"])
        _private(directory, True)
        path = directory / "proposal.json"
        # This control file never contains the key. Open only a private regular
        # file with a fixed byte ceiling; do not follow a replacement symlink.
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            _private(path)
            metadata = os.fstat(descriptor)
            if metadata.st_size > 16384 or metadata.st_ino != path.lstat().st_ino:
                raise DeliveryError("worker proposal changed or exceeds its bound")
            retained = decode_contract(os.read(descriptor, 16385), max_bytes=16384)
        finally:
            os.close(descriptor)
        if canonical_digest(retained) != canonical_digest(value):
            raise DeliveryError("private worker material differs from the reviewed proposal")
        key = read_enrollment_key(directory / "key")
        if "sha256:" + hashlib.sha256(key).hexdigest() != value["key_digest"]:
            raise DeliveryError("worker enrollment key differs from its reviewed commitment")
        return directory, key

    def prepare(self, stream, *, by):
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        authorize_state(state, stream, now=self.orchestrator._now())
        if Redactor().value(stream) != stream:
            raise DeliveryError("worker enrollment identities cannot contain protected credential material")
        key = secrets.token_bytes(32)
        value = proposal(state, stream, "sha256:" + hashlib.sha256(key).hexdigest(), by=by)
        self.root.mkdir(mode=0o700, exist_ok=True)
        _private(self.root, True)
        if len(list(islice(self.root.iterdir(), MAX_STREAMS))) >= MAX_STREAMS:
            raise DeliveryError("worker preparation history reached its retention ceiling")
        directory = self._directory(value["scope"])
        # Exclusive publication prevents replacing a prepared key after review.
        # A partial directory from interruption stays unavailable, not repaired
        # by generating a different key under the same stream identity.
        directory.mkdir(mode=0o700)
        for name, content in (("key", key), ("proposal.json", _bytes(value))):
            fd = os.open(str(directory / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        for path in (directory, self.root, self.state_dir):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return value  # Never return the raw key in an API/CLI response.

    def approve(self, value, *, by, review_digest):
        state = self.orchestrator.state(self.run_id)
        value = _validate_proposal(state, value, by)
        if review_digest != value["digest"]:
            raise DeliveryError("exact human worker enrollment review is required")
        old = state.get("worker_streams", {}).get(value["scope"])
        if old is not None:
            if old["proposal"] != value or old["status"] != "active":
                raise DeliveryError("worker enrollment retry cannot alter or revive a revoked stream")
            self._material(value)
            return deepcopy(old)
        directory, key = self._material(value)
        # Receiver preparation grants no authority before the ledger append.
        WorkerDelivery(directory / "receiver", value["stream"], key, role="receiver", create=True)
        payload = dict(proposal=value, review_digest=review_digest, enrolled_at=self.orchestrator._now())
        apply(deepcopy(state), dict(type="WORKER_STREAM_ENROLLED", payload=payload, actor_id=by, run_id=self.run_id))
        self.orchestrator._emit(self.run_id, "WORKER_STREAM_ENROLLED", payload, actor_id=by, expected_seq=state["last_seq"])
        return self.inspect(value["scope"])

    def revoke(self, scope, *, by, reason):
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        record = state.get("worker_streams", {}).get(scope)
        if record is None:
            raise DeliveryError("unknown enrolled worker stream")
        if record["status"] == "revoked":
            return deepcopy(record)
        if not isinstance(reason, str):
            raise DeliveryError("worker revocation requires a reason")
        try:
            _, key = self._material(record["proposal"])
            reason = key_redactor(key).text(reason)
        except (OSError, DeliveryError):
            # Lost/unsafe key material must not prevent revocation. Withhold the
            # free-text note when its key-specific redaction cannot be checked.
            reason = "Reason withheld: enrollment key unavailable for redaction"
        payload = dict(scope=scope, proposal_digest=record["proposal"]["digest"], reason=reason, revoked_at=self.orchestrator._now())
        apply(deepcopy(state), dict(type="WORKER_STREAM_REVOKED", payload=payload, actor_id=by, run_id=self.run_id))
        self.orchestrator._emit(self.run_id, "WORKER_STREAM_REVOKED", payload, actor_id=by, expected_seq=state["last_seq"])
        return self.inspect(scope)

    def inspect(self, scope):
        require_digest(scope, "worker scope")
        record = self.orchestrator.state(self.run_id).get("worker_streams", {}).get(scope)
        if record is None:
            raise DeliveryError("unknown enrolled worker stream")
        return deepcopy(record)

    @contextmanager
    def _lease_lock(self):
        # A SQLite write reservation serializes this receipt with *all* kernel
        # lease mutations, including other connections/processes. No kernel event
        # is appended here, so the two stores are not presented as one transaction.
        connection = self.orchestrator.store.connection
        if connection.in_transaction:
            raise DeliveryError("worker ingestion cannot borrow an existing kernel transaction")
        previous = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        try:
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("BEGIN IMMEDIATE")
            yield
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("PRAGMA busy_timeout={}".format(previous))

    def receive(self, scope, raw):
        require_digest(scope, "worker scope")
        record = self.inspect(scope)
        if record["status"] != "active":
            raise DeliveryError("worker stream was revoked")
        directory, key = self._material(record["proposal"])
        receiver = WorkerDelivery(directory / "receiver", record["proposal"]["stream"], key, role="receiver")
        # Reject unauthenticated requests before contending with the scheduler.
        # accept() authenticates again inside the critical section as well.
        receiver.validate_batch(raw)
        with self._lease_lock():
            record = self.inspect(scope)
            if record["status"] != "active":
                raise DeliveryError("worker stream was revoked")
            value = record["proposal"]
            def guard(stream):
                return authorize_state(self.orchestrator.state(self.run_id), stream, now=self.orchestrator._now())
            # Unlike the standalone spool, the enrolled service refuses expired
            # or revoked clients even when they only ask for an old receipt.
            guard(value["stream"])
            _, current_key = self._material(value)
            if current_key != key:
                raise DeliveryError("worker enrollment material changed before ingestion")
            return receiver.accept(raw, authorize=guard)

    def producer(self, scope, root, *, by):
        """Explicit owner embedding helper, not remote key transfer or adoption."""
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        record = self.inspect(scope)
        if record["status"] != "active":
            raise DeliveryError("cannot prepare producer for a revoked worker stream")
        authorize_state(state, record["proposal"]["stream"], now=self.orchestrator._now())
        _, key = self._material(record["proposal"])
        return WorkerDelivery(root, record["proposal"]["stream"], key, role="producer", create=True)

    def import_received(self, scope, *, by, request_id, limit=6):
        from .worker_import import import_received
        return import_received(self, scope, by=by, request_id=request_id, limit=limit)

    def records(self, scope, *, after=0, limit=100):
        from .worker_import import snapshot
        return snapshot(self.orchestrator.state(self.run_id), scope, after=after, limit=limit)
