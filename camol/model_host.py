"""One-shot, owner-approved local llama.cpp lifecycle; never a downloader.

The ledger is trusted local-host state, not an authentication boundary for
untrusted callers who can supply an owner's name or write this directory.
"""

import atexit
import fcntl
import hashlib
import http.client
import json
import os
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import ModelError, ModelStore, _exact, _positive, _text
from .json_contracts import decode_contract
from .probes import Redactor
from .schema import SchemaError, canonical_digest, require_digest, require_identifier


_ACTIVE = {"starting", "loading", "loaded", "stopping"}
_STATES = _ACTIVE | {"unloaded", "failed", "cancelled", "expired", "unknown"}
_CHILDREN = []


class ModelHostReadbackError(ModelError):
    """Content-free readiness diagnostic; safe to retain in the private ledger."""
    def __init__(self, phase, cause):
        if phase not in {"listener", "credential", "health", "models", "properties", "identity"}:
            phase = "unknown"
        self.phase = phase
        self.kind = ("timeout" if isinstance(cause, (TimeoutError, subprocess.TimeoutExpired)) else
                     "os_error" if isinstance(cause, OSError) else "invalid_readback")
        super().__init__("model-host " + self.phase + " readback failed (" + self.kind + ")")


@atexit.register
def _detach_helpers():
    for child in _CHILDREN:
        if child.poll() is None:
            # Deliberate detached supervisor, not a leaked application worker.
            child.returncode = 0


def _absolute(value, label):
    path = Path(_text(value, label))
    if not path.is_absolute() or ".." in path.parts or str(path) != value or path.is_symlink():
        raise ModelError(label + " must be an absolute canonical nonsymlink path")
    return path


def _json(data):
    try:
        return decode_contract(data)
    except SchemaError:
        raise ModelError("model host JSON is ambiguous or malformed") from None


@dataclass(frozen=True)
class ModelHostPlan:
    plan_id: str
    owner: str
    model_store_root: str
    download_plan_digest: str
    logical_path: str
    artifact_digest: str
    artifact_size_bytes: int
    executable: str
    executable_digest: str
    port: int
    context_tokens: int = 2048
    threads: int = 2
    gpu_layers: int = 0
    load_timeout_seconds: int = 120
    stop_timeout_seconds: int = 5
    lifetime_seconds: int = 3600
    backend: str = "llama_cpp"

    def __post_init__(self):
        for name in ("plan_id", "owner"):
            require_identifier(getattr(self, name), name)
        for name in ("download_plan_digest", "artifact_digest", "executable_digest"):
            require_digest(getattr(self, name), name)
        for name in ("model_store_root", "executable"):
            _absolute(getattr(self, name), name)
        logical = Path(_text(self.logical_path, "logical_path"))
        if logical.is_absolute() or ".." in logical.parts or logical.suffix.lower() != ".gguf" or "\\" in self.logical_path:
            raise ModelError("host v1 supports one explicit local GGUF artifact")
        for name, minimum, maximum in (
            ("artifact_size_bytes", 4, 2 ** 63 - 1), ("port", 1024, 65535),
            ("context_tokens", 32, 1048576), ("threads", 1, 1024), ("gpu_layers", 0, 1024),
            ("load_timeout_seconds", 1, 3600), ("stop_timeout_seconds", 1, 60),
            ("lifetime_seconds", 1, 86400),
        ):
            _positive(getattr(self, name), name, minimum)
            if getattr(self, name) > maximum:
                raise ModelError(name + " exceeds the supported bound")
        if self.backend != "llama_cpp":
            raise ModelError("unsupported local model host backend")
        if Redactor().value(self.to_dict()) != self.to_dict():
            raise ModelError("host plans cannot include secret-shaped values")

    def to_dict(self):
        return dict(schema="camol.model_host_plan", schema_version=1, **asdict(self))

    def digest(self):
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        fields = set(cls.__dataclass_fields__) | {"schema", "schema_version"}
        _exact(value, fields, "model host plan")
        if value["schema"] != "camol.model_host_plan" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ModelError("unsupported model host plan schema")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ModelHostUnload:
    operation_id: str
    plan_digest: str
    load_operation_id: str
    owner: str

    def __post_init__(self):
        for name in ("operation_id", "load_operation_id", "owner"):
            require_identifier(getattr(self, name), name)
        require_digest(self.plan_digest, "plan_digest")

    def to_dict(self):
        return dict(schema="camol.model_host_unload", schema_version=1, **asdict(self))

    def digest(self):
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        _exact(value, set(cls.__dataclass_fields__) | {"schema", "schema_version"}, "model host unload")
        if value["schema"] != "camol.model_host_unload" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ModelError("unsupported model host unload schema")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


def _file_digest(path, expected=None, *, executable=False):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022 or (executable and not before.st_mode & 0o111):
            raise ModelError("host input must be a regular non-group/world-writable file")
        while True:
            part = handle.read(1024 * 1024)
            if not part:
                break
            digest.update(part)
            size += len(part)
        after = os.fstat(handle.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ModelError("host input changed during verification")
    actual = "sha256:" + digest.hexdigest()
    if expected is not None and expected != actual:
        raise ModelError("host input differs from its approved digest")
    return actual, size


def _private(path, directory=False):
    info = path.lstat()
    expected = 0o700 if directory else 0o600
    if (stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != expected
            or (not directory and info.st_nlink != 1)):
        raise ModelError("host state must be private owner-only regular files/directories")
    return info


class LlamaCppModelHost:
    name = "llama_cpp"

    def __init__(self, root, read_only=False):
        if os.name != "posix":
            raise ModelError("local model host v1 requires POSIX process ownership")
        candidate = Path(root).absolute()
        if candidate.is_symlink():
            raise ModelError("model host root must not be a symlink")
        self.root, self.read_only = candidate.resolve(), read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = _private(self.root, True)
        self._identity = (info.st_dev, info.st_ino)
        self.database = self.root / "host.sqlite3"
        if not self.database.exists() and not read_only:
            fd = os.open(str(self.database), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self._check()
        uri = self.database.as_uri() + "?mode=ro" if read_only else str(self.database)
        self.connection = sqlite3.connect(uri, uri=read_only, timeout=10)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS plans(digest TEXT PRIMARY KEY, document TEXT NOT NULL, approved_by TEXT);
                CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL,
                    kind TEXT NOT NULL, document TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, detail TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_load_per_plan ON operations(plan_digest) WHERE kind='load';
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_digest TEXT NOT NULL,
                    type TEXT NOT NULL, observed_at REAL NOT NULL, data TEXT NOT NULL);
            """)
            self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    def _check(self, write=False):
        if write and self.read_only:
            raise ModelError("model host ledger is read-only")
        info = _private(self.root, True)
        if (info.st_dev, info.st_ino) != self._identity:
            raise ModelError("model host root identity changed")
        _private(self.database)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                _private(self.root / ("host.sqlite3" + suffix))
            except FileNotFoundError:
                # SQLite creates/removes sidecars during concurrent commits.
                # Only absence is permissible; existing unsafe files still fail.
                pass

    def _event(self, digest, kind, data):
        self.connection.execute("INSERT INTO events(plan_digest,type,observed_at,data) VALUES(?,?,?,?)",
                                (digest, kind, time.time(), json.dumps(data, sort_keys=True)))

    def _plan(self, digest):
        self._check()
        require_digest(digest, "host plan digest")
        row = self.connection.execute("SELECT * FROM plans WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ModelError("unknown model host plan")
        plan = ModelHostPlan.from_dict(_json(row["document"]))
        if plan.digest() != digest or row["approved_by"] not in (None, plan.owner):
            raise ModelError("model host plan/approval integrity failure")
        return row, plan

    def _operation(self, digest):
        row = self.connection.execute("SELECT * FROM operations WHERE plan_digest=? AND kind='load'", (digest,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["detail"] = _json(result["detail"])
        if result["state"] not in _STATES or _json(result["document"]) != {"plan_digest": digest, "operation_id": result["operation_id"]}:
            raise ModelError("model host operation integrity failure")
        return result

    def _directory(self, digest):
        path = self.root / ("load-" + digest.split(":", 1)[1])
        if path.exists() or path.is_symlink():
            _private(path, True)
        return path

    def _helper_alive(self, digest):
        path = self._directory(digest) / "helper.lock"
        if not path.exists():
            return False
        _private(path)
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def prepare(self, plan):
        self._check(write=True)
        if not isinstance(plan, ModelHostPlan):
            raise ModelError("prepare requires a frozen ModelHostPlan")
        with self.connection:
            cursor = self.connection.execute("INSERT OR IGNORE INTO plans(digest,document) VALUES(?,?)",
                                             (plan.digest(), json.dumps(plan.to_dict(), sort_keys=True)))
            if cursor.rowcount:
                self._event(plan.digest(), "MODEL_HOST_PREPARED", {"plan_digest": plan.digest()})
        return self.status(plan.digest())

    def approve(self, plan_digest, by):
        self._check(write=True)
        _, plan = self._plan(plan_digest)
        if by != plan.owner:
            raise ModelError("only the exact plan owner may approve model hosting")
        with self.connection:
            self.connection.execute("UPDATE plans SET approved_by=? WHERE digest=?", (by, plan_digest))
            self._event(plan_digest, "MODEL_HOST_APPROVED", {"approved_by": by, "plan_digest": plan_digest})
        return self.status(plan_digest)

    def inventory(self):
        self._check()
        return [self.status(row[0]) for row in self.connection.execute("SELECT digest FROM plans ORDER BY digest")]

    def events(self, plan_digest):
        self._plan(plan_digest)
        return [dict(seq=row["seq"], type=row["type"], observed_at=row["observed_at"], data=_json(row["data"]))
                for row in self.connection.execute("SELECT * FROM events WHERE plan_digest=? ORDER BY seq", (plan_digest,))]

    def status(self, plan_digest, *, live=False):
        row, plan = self._plan(plan_digest)
        operation = self._operation(plan_digest)
        state = operation["state"] if operation else "approved" if row["approved_by"] else "prepared"
        detail = operation["detail"] if operation else {}
        helper_alive = self._helper_alive(plan_digest) if operation and state in _ACTIVE else False
        fresh = operation and 0 <= time.time() - operation["updated_at"] <= 10
        if state in _ACTIVE and not helper_alive:
            state = "unknown"
        observed = None
        if live and state == "loaded" and helper_alive and fresh:
            try:
                observed = self._readback(plan, operation)
            except (ModelError, OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
                state = "unknown"
        return dict(schema="camol.model_host_status", schema_version=1, backend=self.name,
                    plan_digest=plan_digest, plan=plan.to_dict(), approved_by=row["approved_by"], status=state,
                    load_operation_id=operation["operation_id"] if operation else None,
                    last_recorded_status=operation["state"] if operation else None,
                    helper_alive=helper_alive, heartbeat_fresh=bool(fresh),
                    downloaded="verified_at_load" if detail.get("artifact_verified") else "unverified",
                    loaded="observed" if state == "loaded" and fresh else "no" if state in {"unloaded", "cancelled", "expired", "failed"} else "unverified",
                    inference_ready="unverified", task_ready="unverified", endpoint="http://127.0.0.1:" + str(plan.port),
                    readback=observed or detail.get("readback"), operation=operation,
                    identity_scope="prelaunch_file_hashes_and_authenticated_runtime_readback_not_hardware_attestation",
                    limits_scope="runtime_settings_and_helper_deadlines_not_hard_ram_gpu_or_wire_caps",
                    reconciliation_required=state == "unknown")

    def _verify_inputs(self, plan):
        with ModelStore(plan.model_store_root, read_only=True) as models:
            artifacts = models.verified_artifacts(plan.download_plan_digest)
        if len(artifacts) != 1 or artifacts[0]["logical_path"] != plan.logical_path:
            raise ModelError("host v1 requires exactly one pinned GGUF without extra model files")
        artifact = artifacts[0]
        if artifact["digest"] != plan.artifact_digest or artifact["size_bytes"] != plan.artifact_size_bytes:
            raise ModelError("downloaded artifact differs from host authority")
        with open(artifact["path"], "rb") as handle:
            if handle.read(4) != b"GGUF":
                raise ModelError("host artifact must have a GGUF header")
        _file_digest(Path(plan.executable), plan.executable_digest, executable=True)
        return artifact

    def load(self, plan_digest, by, *, operation_id, cancel_event=None):
        self._check(write=True)
        require_identifier(operation_id, "load operation_id")
        row, plan = self._plan(plan_digest)
        if row["approved_by"] != plan.owner or by != plan.owner:
            raise ModelError("approve the exact host plan before process or allocation work")
        existing = self._operation(plan_digest)
        if existing:
            if existing["operation_id"] != operation_id:
                raise ModelError("one-shot host plan already consumed; never retry an uncertain load")
            return self.status(plan_digest)
        for prior in self.connection.execute("SELECT plan_digest FROM operations WHERE kind='load'"):
            if self.status(prior[0])["reconciliation_required"]:
                raise ModelError("an uncertain host operation requires reconciliation before another load")
        if cancel_event is not None and cancel_event.is_set():
            raise ModelError("load cancelled before execution")
        self._verify_inputs(plan)
        # A pre-existing listener is not ours: do not send it API keys or kill it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            try:
                check.bind(("127.0.0.1", plan.port))
            except OSError:
                raise ModelError("approved loopback port is already occupied") from None
        with self.connection:
            now = time.time()
            try:
                self.connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                    (operation_id, plan_digest, "load", json.dumps(dict(plan_digest=plan_digest, operation_id=operation_id)),
                     "starting", now, now, "{}"))
            except sqlite3.IntegrityError:
                raise ModelError("load operation or one-shot plan already consumed") from None
            self._event(plan_digest, "MODEL_HOST_LOAD_INTENT", {"operation_id": operation_id, "approved_by": by})
        try:
            directory = self._directory(plan_digest)
            directory.mkdir(mode=0o700)
            # A random alias authenticates this particular launch, rather than
            # mistaking any unrelated listener on the same port for our model.
            self._new_private(directory / "api.key", secrets.token_urlsafe(48).encode("ascii"))
            self._new_private(directory / "helper.lock", b"")
            script = "import sys;sys.path.insert(0,{!r});from camol.model_host_worker import main;main()".format(str(Path(__file__).resolve().parents[1]))
            child = subprocess.Popen([sys.executable, "-I", "-c", script, str(self.root), plan_digest, operation_id],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=str(directory), env={"PATH": "/usr/bin:/bin", "HOME": str(directory), "LANG": "C"},
                                     close_fds=True, start_new_session=True)
            _CHILDREN[:] = [item for item in _CHILDREN if item.poll() is None]
            _CHILDREN.append(child)
        except BaseException:
            # Even Popen can be interrupted after fork. Never guess "not run".
            self._update(plan_digest, "unknown", {"error": "helper_launch_uncertain"})
            raise ModelError("model host launch uncertain; inspect ledger, do not retry") from None
        deadline = time.monotonic() + plan.load_timeout_seconds + plan.stop_timeout_seconds + 5
        try:
            while time.monotonic() < deadline:
                current = self._operation(plan_digest)
                if current["state"] not in {"starting", "loading", "stopping"}:
                    return self.status(plan_digest)
                if cancel_event is not None and cancel_event.is_set():
                    return self._cancel_load(plan_digest, plan, helper=child)
                if child.poll() is not None:
                    return self.status(plan_digest)
                time.sleep(0.05)
        except KeyboardInterrupt:
            return self._cancel_load(plan_digest, plan, helper=child)
        return self._cancel_load(plan_digest, plan, reason="load_deadline", helper=child)

    def _new_private(self, path, data):
        self._check(write=True)
        if path.parent.parent != self.root or not path.parent.name.startswith("load-"):
            raise ModelError("host private material must remain in its owned load directory")
        _private(path.parent, True)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            expected, actual = path.parent.lstat(), os.fstat(directory_fd)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise ModelError("model host load directory changed before publication")
            fd = os.open(path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _update(self, digest, state=None, detail=None):
        self._check(write=True)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            operation = self._operation(digest)
            merged = dict(operation["detail"], **(detail or {}))
            self.connection.execute("UPDATE operations SET state=?,updated_at=?,detail=? WHERE operation_id=?",
                                    (state or operation["state"], time.time(), json.dumps(merged, sort_keys=True), operation["operation_id"]))
            if state and state not in _ACTIVE and merged.get("unload_operation_id"):
                self.connection.execute("UPDATE operations SET state=?,updated_at=?,detail=? WHERE operation_id=? AND kind='unload'",
                    ("completed" if state == "unloaded" else state, time.time(), json.dumps({"load_outcome": state}), merged["unload_operation_id"]))
            if state and state != operation["state"]:
                self._event(digest, "MODEL_HOST_" + state.upper(), {"operation_id": operation["operation_id"],
                                                                   "state": state, "detail": detail or {}})

    def _cancel_load(self, digest, plan, reason="cancelled", helper=None):
        # Cancellation is part of the original exact load authority, not an
        # inferred permission to touch another model or unrelated process.
        self._update(digest, detail={"stop_requested": reason})
        return self._await_stop(digest, plan.stop_timeout_seconds + 5, helper=helper)

    def propose_unload(self, plan_digest, operation_id):
        _, plan = self._plan(plan_digest)
        operation = self._operation(plan_digest)
        if operation is None:
            raise ModelError("there is no owned load operation to unload")
        return ModelHostUnload(operation_id, plan_digest, operation["operation_id"], plan.owner)

    def unload(self, request, by, *, approve_digest):
        self._check(write=True)
        if not isinstance(request, ModelHostUnload) or approve_digest != request.digest():
            raise ModelError("approve the exact frozen unload request")
        _, plan = self._plan(request.plan_digest)
        operation = self._operation(request.plan_digest)
        if by != plan.owner or request.owner != plan.owner:
            raise ModelError("only the exact owner may request unload")
        if operation is None or operation["operation_id"] != request.load_operation_id:
            raise ModelError("unload targets a different load operation")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.connection.execute("SELECT document FROM operations WHERE operation_id=?", (request.operation_id,)).fetchone()
            if old and _json(old["document"]) != request.to_dict():
                raise ModelError("unload operation identity conflict")
            if not old:
                now = time.time()
                self.connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                    (request.operation_id, request.plan_digest, "unload", json.dumps(request.to_dict(), sort_keys=True), "requested", now, now, "{}"))
                self._event(request.plan_digest, "MODEL_HOST_UNLOAD_APPROVED", {"request": request.to_dict(), "approval_digest": approve_digest})
        self._update(request.plan_digest, detail={"stop_requested": "owner_unload", "unload_operation_id": request.operation_id})
        return self._await_stop(request.plan_digest, plan.stop_timeout_seconds + 5)

    def _await_stop(self, digest, timeout, helper=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.status(digest)
            if result["last_recorded_status"] not in _ACTIVE:
                if helper is not None:
                    try:
                        helper.wait(timeout=max(0.1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        return dict(result, status="unknown", loaded="unverified", reconciliation_required=True)
                return result
            if not result["helper_alive"] and (helper is None or helper.poll() is not None):
                return result
            time.sleep(0.05)
        return dict(self.status(digest), status="unknown", loaded="unverified", reconciliation_required=True)

    def _credential(self, plan_digest):
        """Internal private credential reference; never include it in API results."""
        self._check()
        directory = self._directory(plan_digest)
        key_path = directory / "api.key"
        expected = _private(key_path)
        directory_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            actual = os.fstat(directory_fd)
            parent = directory.lstat()
            if (actual.st_dev, actual.st_ino) != (parent.st_dev, parent.st_ino):
                raise ModelError("model-host credential directory changed")
            descriptor = os.open("api.key", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino, info.st_size) != (expected.st_dev, expected.st_ino, 64):
                    raise ModelError("model-host credential identity changed")
                raw = handle.read(65)
        finally:
            os.close(directory_fd)
        try:
            key = raw.decode("ascii", "strict")
        except UnicodeError:
            raise ModelError("invalid private model-host API credential") from None
        if len(key) != 64 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key):
            raise ModelError("invalid private model-host API credential")
        return key

    def _readback(self, plan, operation, *, deadline=None, cancel_event=None):
        phase = "listener"
        try:
            alias = operation["detail"].get("alias")
            _assert_listener(operation["detail"].get("child_pid"), plan.port, deadline=deadline, cancel_event=cancel_event)
            phase = "credential"
            key = self._credential(plan.digest())
            def authorize_peer():
                _assert_listener(operation["detail"].get("child_pid"), plan.port, deadline=deadline, cancel_event=cancel_event)
            phase = "health"
            health = _http_json(plan.port, "/health", deadline=deadline, cancel_event=cancel_event)
            phase = "models"
            models = _http_json(plan.port, "/v1/models", key, deadline=deadline, cancel_event=cancel_event, authorize=authorize_peer)
            phase = "properties"
            props = _http_json(plan.port, "/props", key, deadline=deadline, cancel_event=cancel_event, authorize=authorize_peer)
            phase = "identity"
            if (health.get("status") != "ok" or not isinstance(models.get("data"), list) or len(models["data"]) != 1
                    or not isinstance(models["data"][0], dict) or models["data"][0].get("id") != alias
                    or props.get("model_path") != operation["detail"].get("model_path")
                    or props.get("is_sleeping") is not False or type(props.get("total_slots")) is not int or props["total_slots"] != 1):
                raise ModelError("model runtime readback differs from the owned load")
            return {"observed_at": time.time(), "health": "ok", "model_alias": alias, "model_path_matches": True,
                    "runtime_reported_loaded": True, "provider_weight_digest": None, "inference_executed": False}
        except (ModelError, OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
            raise ModelHostReadbackError(phase, error) from None


def _check_io_deadline(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ModelError("model-host inspection cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ModelError("model-host inspection deadline exceeded")


def _assert_listener(pid, port, *, deadline=None, cancel_event=None):
    """Prove the expected child owns the loopback listener before sending a key.

This is an OS observation, not endpoint TLS/hardware attestation. A changed or
unobservable listener fails closed. Never probes arbitrary ports/processes.
"""
    if type(pid) is not int or pid <= 1:
        raise ModelError("model host child identity is unavailable")
    _check_io_deadline(deadline, cancel_event)
    if sys.platform.startswith("linux"):
        expected = "0100007F:{:04X}".format(port)
        with open("/proc/net/tcp", "r", encoding="ascii") as handle:
            inodes = {row[9] for row in (line.split() for line in handle.readlines()[1:])
                      if row[1] == expected and row[3] == "0A"}
        if inodes:
            for path in (Path("/proc") / str(pid) / "fd").iterdir():
                try:
                    if os.readlink(path) in {"socket:[" + inode + "]" for inode in inodes}:
                        _check_io_deadline(deadline, cancel_event)
                        return
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP@127.0.0.1:" + str(port),
                                 "-sTCP:LISTEN", "-F", "pn"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"},
                                timeout=2 if deadline is None else min(2, max(0.001, deadline - time.monotonic())), check=False)
        _check_io_deadline(deadline, cancel_event)
        rows = result.stdout.decode("ascii", "replace").splitlines()
        if result.returncode == 0 and "p" + str(pid) in rows and "n127.0.0.1:" + str(port) in rows:
            return
    raise ModelError("approved model process does not own the expected loopback listener")


def _http_json(port, path, key=None, *, deadline=None, cancel_event=None, authorize=None):
    # Direct numeric loopback socket: no DNS, proxy, redirects, cookies, or
    # ambient user/provider credentials. Only read-only lifecycle endpoints.
    if path not in {"/health", "/v1/models", "/props"}:
        raise ModelError("unsupported model-host inspection endpoint")
    if key is not None and (len(key) != 64 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key)):
        raise ModelError("invalid model-host credential shape")
    # A socket timeout alone is not an operation deadline: a slow local peer
    # could drip headers/bytes forever and prevent cancellation/TTL handling.
    # Read a small fixed-length HTTP/1 response with an absolute deadline.
    deadline = min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1
    _check_io_deadline(deadline, cancel_event)
    with socket.create_connection(("127.0.0.1", port), timeout=max(0.001, deadline - time.monotonic())) as connection:
        if key is not None and authorize is not None:
            authorize()
        _check_io_deadline(deadline, cancel_event)
        request = "GET {} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n".format(path, port)
        if key:
            request += "Authorization: Bearer " + key + "\r\n"
        connection.sendall((request + "\r\n").encode("ascii"))
        data, length, header_end = bytearray(), None, None
        while length is None or len(data) < header_end + length:
            _check_io_deadline(deadline, cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError("model-host readback deadline exceeded")
            connection.settimeout(min(remaining, 0.05))
            try:
                chunk = connection.recv(min(16384, 256 * 1024 + 16384 - len(data) + 1))
            except socket.timeout:
                continue
            if not chunk:
                raise ModelError("model-host readback ended before its declared length")
            data.extend(chunk)
            if header_end is None:
                split = data.find(b"\r\n\r\n")
                if split < 0:
                    if len(data) > 16384:
                        raise ModelError("model-host headers exceed their byte ceiling")
                    continue
                if split > 16384:
                    raise ModelError("model-host headers exceed their byte ceiling")
                header_end = split + 4
                rows = bytes(data[:split]).decode("ascii").split("\r\n")
                status = rows.pop(0).split(" ")
                if len(status) < 2 or status[0] not in {"HTTP/1.0", "HTTP/1.1"} or status[1] != "200":
                    raise ModelError("model-host readback is not ready")
                headers = {}
                for row in rows:
                    name, separator, content = row.partition(":")
                    name = name.lower()
                    if not separator or name in headers or not name or name.strip() != name:
                        raise ModelError("ambiguous model-host response headers")
                    headers[name] = content.strip()
                value = headers.get("content-length", "")
                if (not value.isascii() or not value.isdecimal() or len(value) > 7
                        or "transfer-encoding" in headers or headers.get("content-encoding", "identity") != "identity"):
                    raise ModelError("model-host readback requires a bounded unencoded content length")
                length = int(value)
                if length > 256 * 1024:
                    raise ModelError("model-host response exceeds readback limit")
            if len(data) > 256 * 1024 + 16384:
                raise ModelError("model-host response exceeds readback limit")
        value = _json(bytes(data[header_end:header_end + length]))
        if not isinstance(value, dict):
            raise ModelError("model-host readback must be an object")
        return value
