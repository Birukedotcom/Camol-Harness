"""One explicitly approved inference against a Camol-owned local model load.

Prompts/responses live only in the caller and transport, never in this ledger.
An owner string is not remote authentication: this is a trusted local control API.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import select
import socket
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path

from .json_contracts import decode_contract
from .model_host import LlamaCppModelHost, _assert_listener, _private
from .models import ModelError, _exact, _positive
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier


PROMPT_BYTE_LIMIT = 1 << 20
RESPONSE_BYTE_LIMIT = 8 << 20
_TERMINAL = {"completed", "failed", "cancelled", "expired", "unknown"}
_STATES = _TERMINAL | {"prepared", "approved", "in_flight"}
_ALIAS = re.compile(r"camol-[0-9a-f]{48}\Z")
CAPABILITIES = {
    "backend": "llama_cpp_owned_chat_v1",
    "endpoint": "/v1/chat/completions",
    "one_shot": True,
    "tools": False,
    "streaming": False,
    "automatic_retry": False,
    "hard_output_token_cap": False,
    "hard_ram_gpu_cap": False,
    "hard_compute_or_cost_cap": False,
    "usage_source": "provider_reported_or_unknown_not_independently_tokenized",
    "cancellation_scope": "owned_http_transport_closed_not_server_execution_stopped",
    "identity_scope": "owned_listener_and_authenticated_alias_not_cryptographic_weight_attestation",
}


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ledger_errors(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except sqlite3.Error:
            raise ModelError("inference ledger is busy or unavailable; no automatic retry") from None
    return bounded


@dataclass(frozen=True)
class ModelInferencePlan:
    operation_id: str
    host_plan_digest: str
    load_operation_id: str
    owner: str
    model_alias: str
    prompt_digest: str
    prompt_size_bytes: int
    max_output_tokens: int
    timeout_seconds: int
    issued_at: str
    expires_at: str

    def __post_init__(self):
        for name in ("operation_id", "load_operation_id", "owner"):
            require_identifier(getattr(self, name), name)
            if len(getattr(self, name)) > 256:
                raise ModelError(name + " exceeds the inference identity bound")
        for name in ("host_plan_digest", "prompt_digest"):
            require_digest(getattr(self, name), name)
        if not isinstance(self.model_alias, str) or not _ALIAS.fullmatch(self.model_alias):
            raise ModelError("inference requires the exact Camol-owned load alias")
        for name, maximum in (("prompt_size_bytes", PROMPT_BYTE_LIMIT),
                              ("max_output_tokens", 32768), ("timeout_seconds", 600)):
            _positive(getattr(self, name), name)
            if getattr(self, name) > maximum:
                raise ModelError(name + " exceeds the inference bound")
        start = parse_timestamp(self.issued_at, "inference issued_at")
        end = parse_timestamp(self.expires_at, "inference expires_at")
        if not 0 < (end - start).total_seconds() <= 3600:
            raise ModelError("inference authority must have a positive lifetime of at most one hour")

    def to_dict(self):
        return dict(schema="camol.model_inference_plan", schema_version=1, **asdict(self))

    def digest(self):
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        _exact(value, set(cls.__dataclass_fields__) | {"schema", "schema_version"}, "model inference plan")
        if value["schema"] != "camol.model_inference_plan" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ModelError("unsupported model inference schema")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


def _prompt_bytes(path):
    descriptor = os.open(str(Path(path)), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= PROMPT_BYTE_LIMIT:
            raise ModelError("prompt file must be regular, nonempty, and at most 1 MiB")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(PROMPT_BYTE_LIMIT + 1)
        after = os.fstat(descriptor)
        names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (len(data) != before.st_size or len(data) > PROMPT_BYTE_LIMIT
                or tuple(getattr(before, name) for name in names) != tuple(getattr(after, name) for name in names)):
            raise ModelError("prompt file changed during its bounded read")
        try:
            data.decode("utf-8", "strict")
        except UnicodeError:
            raise ModelError("prompt file must contain UTF-8 text") from None
        return data
    finally:
        os.close(descriptor)


def prompt_file_identity(path):
    """Passive bounded fingerprint; never include the path or prompt in metadata."""
    data = _prompt_bytes(path)
    return {"prompt_digest": _digest(data), "prompt_size_bytes": len(data)}


def validate_prompt_file(path, plan):
    if not isinstance(plan, ModelInferencePlan):
        raise ModelError("prompt validation requires a frozen ModelInferencePlan")
    data = _prompt_bytes(path)
    if len(data) != plan.prompt_size_bytes or _digest(data) != plan.prompt_digest:
        raise ModelError("prompt file differs from the exact approved bytes")
    return None


class _Stopped(ModelError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("owned inference " + reason)


def _check_deadline(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise _Stopped("cancelled")
    if time.monotonic() >= deadline:
        raise _Stopped("deadline")


def _wait(sock, deadline, cancel_event, *, writing=False):
    while True:
        _check_deadline(deadline, cancel_event)
        read, write, errors = select.select([] if writing else [sock], [sock] if writing else [], [sock],
                                            min(0.05, max(0, deadline - time.monotonic())))
        if errors:
            raise ModelError("owned inference transport failed")
        if read or write:
            return


def _post(port, payload, deadline, cancel_event, authorize, on_dispatch, observed):
    """One numeric-loopback POST. No redirects, DNS, proxy, tools or retry."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.setblocking(False)
        result = connection.connect_ex(("127.0.0.1", port))
        if result not in {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY}:
            raise ModelError("owned inference connection failed")
        _wait(connection, deadline, cancel_event, writing=True)
        if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            raise ModelError("owned inference connection failed")
        # The credential is resolved inside the owned-host boundary only after
        # an actual connection and a fresh listener/authority check.
        key = authorize()
        _check_deadline(deadline, cancel_event)
        header = ("POST /v1/chat/completions HTTP/1.0\r\nHost: 127.0.0.1:{}\r\n"
                  "Authorization: Bearer {}\r\nContent-Type: application/json\r\n"
                  "Content-Length: {}\r\nConnection: close\r\n\r\n").format(port, key, len(payload)).encode("ascii")
        request = header + payload
        # Conservatively claim uncertainty before the first send can occur.
        on_dispatch()
        observed["dispatch_attempted"] = True
        offset = 0
        while offset < len(request):
            _wait(connection, deadline, cancel_event, writing=True)
            try:
                count = connection.send(request[offset:])
            except BlockingIOError:
                continue
            if not count:
                raise ModelError("owned inference send ended early")
            offset += count
            observed["request_bytes_sent"] = offset
        data, length, header_end = bytearray(), None, None
        while length is None or len(data) < header_end + length:
            _wait(connection, deadline, cancel_event)
            try:
                chunk = connection.recv(min(16384, RESPONSE_BYTE_LIMIT + 16384 - len(data) + 1))
            except BlockingIOError:
                continue
            if not chunk:
                raise ModelError("owned inference response ended before its declared length")
            data.extend(chunk)
            observed["response_bytes_read"] = len(data)
            if header_end is None:
                split = data.find(b"\r\n\r\n")
                if split < 0:
                    if len(data) > 16384:
                        raise ModelError("owned inference response headers exceed their ceiling")
                    continue
                if split > 16384:
                    raise ModelError("owned inference response headers exceed their ceiling")
                header_end = split + 4
                rows = bytes(data[:split]).decode("ascii", "strict").split("\r\n")
                status = rows.pop(0).split(" ")
                if len(status) < 2 or status[0] not in {"HTTP/1.0", "HTTP/1.1"} or status[1] != "200":
                    raise ModelError("owned inference returned a non-success HTTP status")
                headers = {}
                for row in rows:
                    name, separator, content = row.partition(":")
                    name = name.lower()
                    if not separator or name in headers or not name or name.strip() != name:
                        raise ModelError("owned inference returned ambiguous headers")
                    headers[name] = content.strip()
                value = headers.get("content-length", "")
                if (not value.isascii() or not value.isdecimal() or len(value) > 8
                        or "transfer-encoding" in headers or headers.get("content-encoding", "identity") != "identity"):
                    raise ModelError("owned inference requires bounded unencoded content length")
                length = int(value)
                if length > RESPONSE_BYTE_LIMIT:
                    raise ModelError("owned inference response exceeds its byte ceiling")
            if len(data) > RESPONSE_BYTE_LIMIT + 16384:
                raise ModelError("owned inference response exceeds its byte ceiling")
        if len(data) != header_end + length:
            raise ModelError("owned inference response has trailing unframed bytes")
        _check_deadline(deadline, cancel_event)
        return bytes(data[header_end:])


def _usage(value):
    result = {"input_tokens": None, "output_tokens": None, "total_tokens": None,
              "source": "unknown", "invalid_fields": False, "total_cost_usd": None,
              "cost_source": "unknown_not_measured"}
    if not isinstance(value, dict):
        return result
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        if source in value:
            count = value[source]
            if type(count) is int and 0 <= count <= 2 ** 63 - 1:
                result[target] = count
            else:
                result["invalid_fields"] = True
    if (all(result[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens"))
            and result["total_tokens"] != result["input_tokens"] + result["output_tokens"]):
        result["total_tokens"] = None
        result["invalid_fields"] = True
    if any(result[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens")):
        result["source"] = "provider_reported"
    return result


class ModelInference:
    """Private one-shot inference ledger scoped to an existing model-host root."""

    @_ledger_errors
    def __init__(self, root, read_only=False):
        self.host = LlamaCppModelHost(root, read_only=True)
        # A default SQLite ten-second busy wait must not defeat a one-second
        # operation deadline or delay cancellation. Contention fails closed;
        # it is never permission to repeat a dispatched inference.
        self.host.connection.execute("PRAGMA busy_timeout=50")
        self.root, self.read_only = self.host.root, read_only
        self.database = self.root / "inference.sqlite3"
        try:
            if not read_only and not self.database.exists():
                descriptor = os.open(str(self.database), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
            info = _private(self.database)
            self._identity = (info.st_dev, info.st_ino)
            target = self.database.as_uri() + "?mode=ro" if read_only else str(self.database)
            self.connection = sqlite3.connect(target, uri=read_only, timeout=0.05)
            self.connection.row_factory = sqlite3.Row
            if not read_only:
                self.connection.execute("PRAGMA journal_mode=DELETE")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.executescript("""
                    CREATE TABLE IF NOT EXISTS requests(digest TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                        document TEXT NOT NULL, approved_by TEXT, state TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL, receipt TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        digest TEXT NOT NULL, type TEXT NOT NULL, observed_at REAL NOT NULL, data TEXT NOT NULL);
                """)
                self.connection.commit()
        except BaseException:
            self.host.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()
        self.host.close()

    def _check(self, write=False):
        self.host._check()
        if write and self.read_only:
            raise ModelError("inference ledger is read-only")
        info = _private(self.database)
        if (info.st_dev, info.st_ino) != self._identity:
            raise ModelError("inference ledger identity changed")
        for suffix in ("-journal", "-wal", "-shm"):
            path = self.root / ("inference.sqlite3" + suffix)
            if path.exists() or path.is_symlink():
                _private(path)

    def _row(self, digest):
        self._check()
        require_digest(digest, "inference plan digest")
        row = self.connection.execute("SELECT * FROM requests WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ModelError("unknown inference plan")
        plan = ModelInferencePlan.from_dict(decode_contract(row["document"]))
        if (plan.digest() != digest or plan.operation_id != row["operation_id"] or row["state"] not in _STATES
                or row["approved_by"] not in (None, plan.owner)):
            raise ModelError("inference plan/approval integrity failure")
        return row, plan

    def _event(self, digest, kind, data):
        self.connection.execute("INSERT INTO events(digest,type,observed_at,data) VALUES(?,?,?,?)",
                                (digest, kind, time.time(), json.dumps(data, sort_keys=True)))

    def _fresh(self, plan):
        now = time.time()
        if not parse_timestamp(plan.issued_at, "issued_at").timestamp() <= now < parse_timestamp(plan.expires_at, "expires_at").timestamp():
            raise ModelError("inference authority is future-issued or expired")

    def _host_subject(self, plan):
        row, host_plan = self.host._plan(plan.host_plan_digest)
        operation = self.host._operation(plan.host_plan_digest)
        if (host_plan.owner != plan.owner or row["approved_by"] != plan.owner or operation is None
                or operation["operation_id"] != plan.load_operation_id
                or operation["detail"].get("alias") != plan.model_alias):
            raise ModelError("inference does not bind the exact approved host/load/owner/alias")
        if (operation["state"] != "loaded" or operation["detail"].get("stop_requested")
                or not self.host._helper_alive(plan.host_plan_digest)
                or not 0 <= time.time() - operation["updated_at"] <= 10
                or time.time() >= operation["created_at"] + host_plan.lifetime_seconds):
            raise ModelError("owned model load is not freshly active; inference is denied")
        return host_plan, operation

    @_ledger_errors
    def prepare(self, plan):
        self._check(write=True)
        if not isinstance(plan, ModelInferencePlan):
            raise ModelError("prepare requires a frozen ModelInferencePlan")
        self._fresh(plan)
        self._host_subject(plan)  # ledger/lock inspection only; no HTTP or key read
        with self.connection:
            try:
                cursor = self.connection.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?)",
                    (plan.digest(), plan.operation_id, json.dumps(plan.to_dict(), sort_keys=True), None, "prepared", time.time(), time.time(), "{}"))
                if not cursor.rowcount:
                    existing = self.connection.execute("SELECT digest FROM requests WHERE operation_id=?", (plan.operation_id,)).fetchone()
                    if existing is None or existing[0] != plan.digest():
                        raise ModelError("inference operation ID already binds a different contract")
                else:
                    self._event(plan.digest(), "MODEL_INFERENCE_PREPARED", {"plan_digest": plan.digest()})
            except sqlite3.IntegrityError:
                raise ModelError("inference operation identity conflict") from None
        return self.status(plan.digest())

    @_ledger_errors
    def approve(self, plan_digest, by):
        self._check(write=True)
        row, plan = self._row(plan_digest)
        self._fresh(plan)
        self._host_subject(plan)
        if by != plan.owner:
            raise ModelError("only the exact inference owner may approve")
        if row["state"] not in {"prepared", "approved"}:
            raise ModelError("one-shot inference already consumed; approval cannot reset it")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute("UPDATE requests SET approved_by=?,state='approved',updated_at=? WHERE digest=? AND state IN ('prepared','approved')",
                                             (by, time.time(), plan_digest))
            if cursor.rowcount != 1:
                raise ModelError("inference state changed before approval")
            self._event(plan_digest, "MODEL_INFERENCE_APPROVED", {"plan_digest": plan_digest, "approved_by": by})
        return self.status(plan_digest)

    def _lock_path(self, digest):
        return self.root / ("inference-" + digest.split(":", 1)[1] + ".lock")

    def _active(self, digest):
        path = self._lock_path(digest)
        if not path.exists():
            return False
        _private(path)
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    @_ledger_errors
    def status(self, plan_digest):
        row, plan = self._row(plan_digest)
        state = row["state"]
        if state == "in_flight" and not self._active(plan_digest):
            state = "unknown"
        receipt = decode_contract(row["receipt"])
        return self._report(plan, row["approved_by"], state, row["state"], receipt)

    def _report(self, plan, approved_by, state, recorded_state, receipt):
        usage = receipt.get("usage", {})
        coverage = {name: "provider_reported" if usage.get(name) is not None else "unknown"
                    for name in ("input_tokens", "output_tokens", "total_tokens")}
        coverage["total_cost_usd"] = "unknown_not_measured"
        return dict(schema="camol.model_inference_status", schema_version=1,
                    plan_digest=plan.digest(), plan=plan.to_dict(), approved_by=approved_by, status=state,
                    last_recorded_status=recorded_state, receipt=receipt, usage_coverage=coverage,
                    reconciliation_required=state == "unknown", reservation_held=state in {"in_flight", "unknown"},
                    reserved_output_token_request=plan.max_output_tokens if state in {"in_flight", "unknown"} else 0,
                    response_text=None, response_retained=False, capabilities=dict(CAPABILITIES))

    @_ledger_errors
    def inventory(self):
        self._check()
        return [self.status(row[0]) for row in self.connection.execute("SELECT digest FROM requests ORDER BY created_at,digest")]

    @_ledger_errors
    def events(self, plan_digest):
        self._row(plan_digest)
        return [dict(seq=row["seq"], type=row["type"], observed_at=row["observed_at"], data=decode_contract(row["data"]))
                for row in self.connection.execute("SELECT * FROM events WHERE digest=? ORDER BY seq", (plan_digest,))]

    def _busy_bound(self, deadline):
        milliseconds = min(50, max(0, int((deadline - time.monotonic()) * 1000)))
        self.connection.execute("PRAGMA busy_timeout=" + str(milliseconds))
        self.host.connection.execute("PRAGMA busy_timeout=" + str(milliseconds))

    def _update(self, plan, state, receipt, event, *, deadline=None):
        self._check(write=True)
        if deadline is not None:
            self._busy_bound(deadline)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute("UPDATE requests SET state=?,updated_at=?,receipt=? WHERE digest=? AND state='in_flight'",
                (state, time.time(), json.dumps(receipt, sort_keys=True), plan.digest()))
            if cursor.rowcount != 1:
                raise ModelError("inference invocation state changed unexpectedly")
            self._event(plan.digest(), event, receipt)

    @_ledger_errors
    def infer(self, plan_digest, by, *, prompt_path, cancel_event=None):
        self._check(write=True)
        row, plan = self._row(plan_digest)
        if by != plan.owner or row["approved_by"] != plan.owner:
            raise ModelError("approve the exact inference plan before sending a prompt")
        if row["state"] not in {"prepared", "approved"}:
            return self.status(plan_digest)  # includes unknown; never resume or repeat
        if row["state"] != "approved":
            raise ModelError("inference plan is not approved")
        self._fresh(plan)
        host_plan, operation = self._host_subject(plan)
        prompt = _prompt_bytes(prompt_path)
        if len(prompt) != plan.prompt_size_bytes or _digest(prompt) != plan.prompt_digest:
            raise ModelError("prompt file differs from the exact approved bytes")
        payload = json.dumps({"model": plan.model_alias, "messages": [{"role": "user", "content": prompt.decode("utf-8")}],
                              "max_tokens": plan.max_output_tokens, "stream": False, "n": 1},
                             separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        started = time.time()
        monotonic_start = time.monotonic()
        available = min(plan.timeout_seconds, parse_timestamp(plan.expires_at, "expires_at").timestamp() - started,
                        operation["created_at"] + host_plan.lifetime_seconds - started)
        deadline = monotonic_start + available
        _check_deadline(deadline, cancel_event)
        lock_path = self._lock_path(plan_digest)
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        observed = {"dispatch_attempted": False, "request_bytes_sent": 0, "response_bytes_read": 0}
        receipt = dict(schema="camol.model_inference_receipt", schema_version=1, plan_digest=plan_digest,
                       operation_id=plan.operation_id, host_plan_digest=plan.host_plan_digest,
                       load_operation_id=plan.load_operation_id, model_alias=plan.model_alias,
                       prompt_digest=plan.prompt_digest, prompt_size_bytes=plan.prompt_size_bytes,
                       request_body_digest=_digest(payload), request_body_size_bytes=len(payload),
                       requested_output_tokens=plan.max_output_tokens, started_at=started, finished_at=None,
                       duration_ms=None, status="in_flight", usage=_usage(None), observed=dict(observed),
                       response_digest=None, response_size_bytes=None, output_text_digest=None,
                       output_text_size_bytes=None, limit_violation=False, error=None,
                       transport_closed=False, server_execution_stopped="unverified", outcome_persisted=False)
        try:
            _private(lock_path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ModelError("this inference operation is already active") from None
            with self.connection:
                _check_deadline(deadline, cancel_event)
                self._busy_bound(deadline)
                self.connection.execute("BEGIN IMMEDIATE")
                current, _ = self._row(plan_digest)
                if current["state"] != "approved":
                    return self.status(plan_digest)
                # A lost result must not be silently retried under a new ID. The
                # exact load is one-shot itself; cached terminal claims do not
                # release this guard or grant authority to another load.
                for other in self.connection.execute("SELECT document FROM requests WHERE state IN ('in_flight','unknown')"):
                    _check_deadline(deadline, cancel_event)
                    pending = ModelInferencePlan.from_dict(decode_contract(other["document"]))
                    if pending.host_plan_digest == plan.host_plan_digest and pending.load_operation_id == plan.load_operation_id:
                        raise ModelError("active or uncertain inference retains this load's request reservation")
                self.connection.execute("UPDATE requests SET state='in_flight',updated_at=?,receipt=? WHERE digest=?",
                                        (started, json.dumps(receipt, sort_keys=True), plan_digest))
                self._event(plan_digest, "MODEL_INFERENCE_INTENT", receipt)
            response_text = None
            try:
                # No prompt POST until the owned load's read-only authenticated
                # readiness checks agree; keep the whole operation deadline.
                self.host._readback(host_plan, operation, deadline=deadline, cancel_event=cancel_event)
                private_key = [None]

                def authorize():
                    self._fresh(plan)
                    current_host, current_load = self._host_subject(plan)
                    _assert_listener(current_load["detail"].get("child_pid"), current_host.port,
                                     deadline=deadline, cancel_event=cancel_event)
                    private_key[0] = self.host._credential(plan.host_plan_digest)
                    return private_key[0]

                def dispatched():
                    _check_deadline(deadline, cancel_event)
                    receipt["observed"] = dict(observed, dispatch_attempted=True)
                    self._update(plan, "in_flight", receipt, "MODEL_INFERENCE_DISPATCH_INTENT", deadline=deadline)

                raw = _post(host_plan.port, payload, deadline, cancel_event, authorize, dispatched, observed)
                receipt.update(response_digest=_digest(raw), response_size_bytes=len(raw))
                value = decode_contract(raw, max_bytes=RESPONSE_BYTE_LIMIT)
                if not isinstance(value, dict):
                    raise ModelError("inference response must be a JSON object")
                receipt["usage"] = _usage(value.get("usage"))
                output_tokens = receipt["usage"]["output_tokens"]
                receipt["limit_violation"] = output_tokens is not None and output_tokens > plan.max_output_tokens
                choices = value.get("choices")
                if (value.get("model") != plan.model_alias or not isinstance(choices, list) or len(choices) != 1
                        or not isinstance(choices[0], dict) or type(choices[0].get("index")) is not int or choices[0]["index"] != 0
                        or choices[0].get("finish_reason") not in {"stop", "length"}):
                    raise ModelError("inference response differs from the approved alias or single text completion")
                message = choices[0].get("message")
                if (not isinstance(message, dict) or message.get("role") != "assistant"
                        or not isinstance(message.get("content"), str)
                        or message.get("tool_calls") or message.get("function_call")):
                    raise ModelError("inference response is not the supported tool-free assistant text")
                if private_key[0] in message["content"]:
                    raise ModelError("inference response echoed its private transport credential")
                text_bytes = message["content"].encode("utf-8", "strict")
                receipt.update(output_text_digest=_digest(text_bytes), output_text_size_bytes=len(text_bytes))
                if receipt["limit_violation"]:
                    receipt.update(status="failed", error="provider_output_request_exceeded")
                else:
                    response_text = message["content"]
                    receipt["status"] = "completed"
            except BaseException as error:
                reason = (error.reason if isinstance(error, _Stopped) else "cancelled"
                          if isinstance(error, KeyboardInterrupt) or (cancel_event is not None and cancel_event.is_set()) else "deadline"
                          if time.monotonic() >= deadline else "request_failed")
                receipt["status"] = "unknown" if observed["dispatch_attempted"] else "cancelled" if reason == "cancelled" else "expired" if reason == "deadline" else "failed"
                receipt["error"] = reason  # never preserve raw exceptions/provider text
            receipt.update(finished_at=time.time(), duration_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
                           observed=dict(observed), transport_closed=True, outcome_persisted=True)
            try:
                self._update(plan, receipt["status"], receipt, "MODEL_INFERENCE_OUTCOME", deadline=deadline)
            except (sqlite3.Error, OSError, ModelError):
                # The durable intent still consumes this operation and holds
                # its reservation. Never claim success when the outcome could
                # not be committed; do not wait indefinitely for a writer.
                receipt.update(status="unknown", error="outcome_persistence_unavailable", outcome_persisted=False)
                return self._report(plan, plan.owner, "unknown", "in_flight", receipt)
            result = self.status(plan_digest)
            result["response_text"] = response_text
            return result
        finally:
            os.close(descriptor)
