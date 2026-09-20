"""Provider-neutral discovery that never persists provider credentials."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .probes import Redactor
from .schema import reject_unknown_fields, require_schema_header


class ConnectionError(RuntimeError):
    """A connection probe or persisted connection record is unsafe."""


CONNECTION_SCHEMA = "camol.connection"
CONNECTION_VERSION = 1
CONNECTION_FIELDS = (
    "schema", "schema_version", "connection_id", "provider", "kind",
    "auth_method", "credential_ref", "status", "capabilities", "runtime",
    "runtime_version", "account_fingerprint", "requested_model", "resolved_model",
    "observed_at", "detail",
)
CONNECTION_STATUSES = frozenset({"ready", "auth_required", "unavailable", "unknown", "error"})
LOCAL_PROBE_TIMEOUT_SECONDS = 2.0
LOCAL_PROBE_MAX_BYTES = 1 << 20
CLI_PROBE_MAX_BYTES = 256 << 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_loopback_endpoint(endpoint: str) -> str:
    """Return a normalized HTTP(S) numeric-loopback endpoint or deny it."""
    try:
        parsed = urllib.parse.urlparse(endpoint)
        host = ipaddress.ip_address(parsed.hostname or "")
        parsed.port
    except (ValueError, UnicodeError) as error:
        raise ConnectionError("local model endpoint must use a valid numeric loopback address") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not host.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectionError("local model endpoint must use credential-free HTTP(S) numeric loopback")
    return endpoint.rstrip("/")


def observation_age(record: Mapping[str, Any], *, now=None) -> str:
    """Label age without converting a cached observation into current readiness."""
    try:
        observed = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return "age unknown"
        seconds = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    except (ValueError, TypeError, KeyError, AttributeError):
        return "age unknown"
    if seconds < 0:
        return "future-dated; unverified"
    if seconds < 60:
        return "{}s ago".format(int(seconds))
    if seconds < 3600:
        return "{}m ago".format(int(seconds // 60))
    if seconds < 86400:
        return "{}h ago".format(int(seconds // 3600))
    return "{}d ago".format(int(seconds // 86400))


def observation_label(record: Mapping[str, Any], *, now=None) -> str:
    """Interpret legacy ready by its observation type, never as task authority."""
    if record["status"] == "ready":
        label = {"claude-cli": "auth-observed", "codex-cli": "auth-observed",
                 "openai-api-env": "key-presence-observed", "local-openai": "catalog-observed"}.get(
                     record["connection_id"], "status-observed")
    else:
        label = record["status"] + "-observed"
    return label + " " + observation_age(record, now=now)


def read_local_catalog(endpoint: str, *, timeout=None, maximum=None, cancel_event=None) -> bytes:
    """One GET with owned socket shutdown, absolute deadline and no redirects.

    Numeric loopback avoids DNS and http.client never discovers proxies or follows
    redirects. A deadline watcher shuts down an owned duplicate socket, interrupting slow
    headers/body reads; this function owns closing every response/connection.
    """
    endpoint = validate_loopback_endpoint(endpoint)
    timeout = LOCAL_PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    maximum = LOCAL_PROBE_MAX_BYTES if maximum is None else maximum
    if type(timeout) not in (int, float) or not 0 < timeout <= 5 or type(maximum) is not int or not 1 <= maximum <= LOCAL_PROBE_MAX_BYTES:
        raise ConnectionError("local catalog probe bounds are invalid")
    target = urllib.parse.urlsplit(endpoint)
    port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
    # We explicitly own TCP before connecting and perform TLS as a separate
    # deadline-bound phase. HTTPSConnection.connect() hides the socket until its
    # handshake returns, which would prevent cancellation during that handshake.
    connection = http.client.HTTPConnection(target.hostname, port, timeout=min(timeout, 1.0))
    lock, expired, finished = threading.Lock(), threading.Event(), threading.Event()
    owned = [None]
    response = None
    def abort():
        expired.set()
        with lock:
            if owned[0] is not None:
                try:
                    owned[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    deadline = time.monotonic() + timeout
    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if expired.is_set() or remaining <= 0:
            raise ConnectionError("local catalog deadline exceeded")
        if cancel_event is not None and cancel_event.is_set():
            raise ConnectionError("local catalog request cancelled")
        return remaining
    def watch_deadline():
        while not finished.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
                abort()
                return
            finished.wait(min(.025, remaining))
    timer = threading.Thread(target=watch_deadline, name="camol-local-catalog-deadline", daemon=True)
    timer.start()
    try:
        remaining_timeout()
        family = socket.AF_INET6 if ":" in target.hostname else socket.AF_INET
        connection.sock = socket.socket(family, socket.SOCK_STREAM)
        with lock:
            owned[0] = socket.socket(fileno=os.dup(connection.sock.fileno()))
        connection.sock.settimeout(min(1.0, remaining_timeout()))
        connection.sock.connect((target.hostname, port))
        if target.scheme == "https":
            context = ssl.create_default_context()
            connection.sock.settimeout(remaining_timeout())
            connection.sock = context.wrap_socket(
                connection.sock, server_hostname=target.hostname,
                do_handshake_on_connect=False,
            )
            connection.sock.do_handshake()
        connection.sock.settimeout(remaining_timeout())
        path = (target.path.rstrip("/") if target.path else "") + "/models"
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ConnectionError("local catalog returned non-success status; redirects are refused")
        declared = response.getheader("Content-Length")
        if declared is not None and (len(declared) > 20 or not declared.isdecimal() or int(declared) > maximum):
            raise ConnectionError("local catalog exceeds the bounded response size")
        value = response.read(maximum + 1)
        if len(value) > maximum:
            raise ConnectionError("local catalog exceeds the bounded response size")
        if expired.is_set() or time.monotonic() >= deadline:
            raise ConnectionError("local catalog deadline exceeded")
        return value
    finally:
        finished.set()
        if response is not None:
            response.close()
        connection.close()
        with lock:
            if owned[0] is not None:
                owned[0].close()
                owned[0] = None
        timer.join(timeout=.2)


def validate_connection(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectionError("connection must be an object")
    require_schema_header(value, CONNECTION_SCHEMA, CONNECTION_VERSION, "connection")
    reject_unknown_fields(value, CONNECTION_FIELDS, "connection")
    if set(value) != set(CONNECTION_FIELDS):
        raise ConnectionError("connection is missing fields")
    for name in ("connection_id", "provider", "kind", "auth_method", "credential_ref", "status", "runtime", "observed_at", "detail"):
        if not isinstance(value[name], str):
            raise ConnectionError("connection {} must be text".format(name))
    if value["status"] not in CONNECTION_STATUSES:
        raise ConnectionError("connection status is invalid")
    for name in ("runtime_version", "account_fingerprint", "requested_model", "resolved_model"):
        if value[name] is not None and not isinstance(value[name], str):
            raise ConnectionError("connection {} must be text or null".format(name))
    if not isinstance(value["capabilities"], list) or any(not isinstance(item, str) or not item for item in value["capabilities"]):
        raise ConnectionError("connection capabilities must be strings")
    if len(value["detail"]) > 1_000:
        raise ConnectionError("connection detail is too large")
    normalized = dict(value)
    normalized["capabilities"] = sorted(set(value["capabilities"]))
    return normalized


def _record(
    connection_id: str, provider: str, kind: str, *, status: str,
    auth_method: str = "none", credential_ref: str = "none", capabilities: Sequence[str] = (),
    runtime: str = "", runtime_version: Optional[str] = None,
    account_fingerprint: Optional[str] = None, requested_model: Optional[str] = None,
    resolved_model: Optional[str] = None, detail: str = "",
) -> Dict[str, Any]:
    return validate_connection({
        "schema": CONNECTION_SCHEMA,
        "schema_version": CONNECTION_VERSION,
        "connection_id": connection_id,
        "provider": provider,
        "kind": kind,
        "auth_method": auth_method,
        "credential_ref": credential_ref,
        "status": status,
        "capabilities": list(capabilities),
        "runtime": runtime,
        "runtime_version": runtime_version,
        "account_fingerprint": account_fingerprint,
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "observed_at": _now(),
        "detail": Redactor().text(detail),
    })


@dataclass
class ConnectionRegistry:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.path = self.root / "connections.json"
        self.key_path = self.root / "connection-fingerprint.key"
        self._probe_context = threading.local()

    def _check_cancelled(self):
        event = getattr(self._probe_context, "cancel_event", None)
        if event is not None and event.is_set():
            raise ConnectionError("connection refresh cancelled")

    def _ensure_root(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise ConnectionError("connection state root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _key(self) -> bytes:
        self._ensure_root()
        if self.key_path.exists():
            if self.key_path.is_symlink() or not self.key_path.is_file() or (self.key_path.stat().st_mode & 0o777) != 0o600:
                raise ConnectionError("connection fingerprint key is unsafe")
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise ConnectionError("connection fingerprint key is malformed")
            return key
        descriptor = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        key = os.urandom(32)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        return key

    def fingerprint(self, value: str) -> str:
        return "hmac-sha256:" + hmac.new(self._key(), value.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _environment() -> Dict[str, str]:
        return {
            name: os.environ[name]
            for name in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
            if name in os.environ
        }

    def _run(self, argv: Sequence[str], timeout: int = 10) -> subprocess.CompletedProcess:
        process = None
        finished = False
        try:
            self._check_cancelled()
            process = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._environment(), start_new_session=True,
            )
            output = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                for name in output:
                    selector.register(getattr(process, name), selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    self._check_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConnectionError("connection probe exceeded its deadline")
                    for key, _ in selector.select(min(.1, remaining)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output[key.data].extend(chunk)
                            if sum(len(value) for value in output.values()) > CLI_PROBE_MAX_BYTES:
                                raise ConnectionError("connection probe exceeded its output bound")
            result = subprocess.CompletedProcess(list(argv), process.wait(timeout=1), bytes(output["stdout"]), bytes(output["stderr"]))
            finished = True
            return result
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConnectionError("connection probe failed: {}".format(error.__class__.__name__)) from error
        finally:
            if process is not None:
                if not finished or process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.wait(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def probe_claude(self) -> Dict[str, Any]:
        executable = shutil.which("claude")
        if not executable:
            return _record("claude-cli", "anthropic", "cli", status="unavailable", runtime="claude", detail="Claude CLI is not installed")
        version_result = self._run([executable, "--version"])
        version = version_result.stdout.decode("utf-8", "replace").strip()[:120] or None
        status_result = self._run([executable, "auth", "status"])
        if status_result.returncode != 0:
            return _record(
                "claude-cli", "anthropic", "cli", status="auth_required", runtime="claude",
                runtime_version=version, credential_ref="cli:claude", detail="Run /login claude to authenticate",
            )
        try:
            payload = json.loads(status_result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _record(
                "claude-cli", "anthropic", "cli", status="error", runtime="claude",
                runtime_version=version, credential_ref="cli:claude", detail="Claude returned an unreadable authentication status",
            )
        if not isinstance(payload, dict):
            raise ConnectionError("Claude authentication status must be an object")
        logged_in = payload.get("loggedIn") is True
        identity = "{}:{}".format(payload.get("orgId", ""), payload.get("email", ""))
        return _record(
            "claude-cli", "anthropic", "cli", status="ready" if logged_in else "auth_required",
            auth_method=str(payload.get("authMethod") or "unknown"), credential_ref="cli:claude",
            capabilities=("conversation", "worker", "tools") if logged_in else (), runtime="claude",
            runtime_version=version, account_fingerprint=self.fingerprint(identity) if logged_in and identity != ":" else None,
            detail="Authenticated Claude CLI" if logged_in else "Run /login claude to authenticate",
        )

    def probe_codex(self) -> Dict[str, Any]:
        executable = shutil.which("codex")
        if not executable:
            return _record("codex-cli", "openai", "cli", status="unavailable", runtime="codex", detail="Codex CLI is not installed")
        version_result = self._run([executable, "--version"])
        version = version_result.stdout.decode("utf-8", "replace").strip()[:120] or None
        status_result = self._run([executable, "login", "status"])
        output = (status_result.stdout + status_result.stderr).decode("utf-8", "replace").strip()
        ready = status_result.returncode == 0 and "logged in" in output.lower()
        method = "chatgpt" if "chatgpt" in output.lower() else "api_key" if "api" in output.lower() else "unknown"
        return _record(
            "codex-cli", "openai", "cli", status="ready" if ready else "auth_required",
            auth_method=method, credential_ref="cli:codex", capabilities=("conversation", "worker", "tools") if ready else (),
            runtime="codex", runtime_version=version,
            account_fingerprint=self.fingerprint("codex:" + method) if ready else None,
            detail="Authenticated Codex CLI" if ready else "Run /login codex to authenticate",
        )

    def probe_openai_environment(self) -> Dict[str, Any]:
        configured = bool(os.environ.get("OPENAI_API_KEY"))
        return _record(
            "openai-api-env", "openai", "api", status="ready" if configured else "auth_required",
            auth_method="api_key", credential_ref="env:OPENAI_API_KEY",
            capabilities=("responses",) if configured else (), runtime="https",
            detail="OPENAI_API_KEY reference is present" if configured else "OPENAI_API_KEY is not configured",
        )

    def probe_local(self, endpoint: str = "http://127.0.0.1:11434/v1") -> Dict[str, Any]:
        endpoint = validate_loopback_endpoint(endpoint)
        try:
            from .json_contracts import decode_contract
            payload = decode_contract(read_local_catalog(endpoint, cancel_event=getattr(self._probe_context, "cancel_event", None)), max_bytes=LOCAL_PROBE_MAX_BYTES)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ConnectionError("local endpoint did not return a model catalog")
            count = len(payload["data"])
            return _record(
                "local-openai", "local", "openai_compatible", status="ready", auth_method="none",
                credential_ref="none", capabilities=(), runtime=endpoint,
                detail="Catalog reported {} model(s); load, inference and task readiness are unverified".format(count),
            )
        except (OSError, http.client.HTTPException, ValueError, ConnectionError):
            return _record(
                "local-openai", "local", "openai_compatible", status="unavailable", auth_method="none",
                credential_ref="none", runtime=endpoint, detail="Local OpenAI-compatible endpoint is unavailable",
            )

    def probe_all(self) -> List[Dict[str, Any]]:
        probes = (
            (self.probe_claude, ("claude-cli", "anthropic", "cli", "claude")),
            (self.probe_codex, ("codex-cli", "openai", "cli", "codex")),
            (self.probe_openai_environment, ("openai-api-env", "openai", "api", "https")),
            (self.probe_local, ("local-openai", "local", "openai_compatible", "http://127.0.0.1:11434/v1")),
        )
        records = []
        for probe, (connection_id, provider, kind, runtime) in probes:
            self._check_cancelled()
            try:
                records.append(probe())
            except (ConnectionError, OSError, ValueError) as error:
                records.append(_record(
                    connection_id, provider, kind, status="error", runtime=runtime,
                    detail="{} probe failed safely: {}".format(connection_id, error.__class__.__name__),
                ))
        self._check_cancelled()
        self.save(records)
        return records

    def refresh(self, target: str = "all", *, cancel_event=None) -> List[Dict[str, Any]]:
        """Explicit refresh; a login refresh never probes unrelated providers."""
        previous = getattr(self._probe_context, "cancel_event", None)
        self._probe_context.cancel_event = cancel_event
        try:
            self._check_cancelled()
            return self._refresh(target)
        finally:
            self._probe_context.cancel_event = previous

    def _refresh(self, target):
        if target == "all":
            return self.probe_all()
        choices = {"claude": self.probe_claude, "codex": self.probe_codex, "local": self.probe_local,
                   "openai": self.probe_openai_environment}
        if target not in choices:
            raise ConnectionError("refresh target must be all, claude, codex, local or openai")
        record = choices[target]()
        self._check_cancelled()
        records = {item["connection_id"]: item for item in self.load()}
        records[record["connection_id"]] = record
        result = [records[key] for key in sorted(records)]
        self.save(result)
        return result

    def save(self, records: Iterable[Mapping[str, Any]]) -> None:
        self._ensure_root()
        normalized = [validate_connection(record) for record in records]
        if len({record["connection_id"] for record in normalized}) != len(normalized):
            raise ConnectionError("connection ids must be unique")
        descriptor, temporary = tempfile.mkstemp(prefix="connections.", dir=str(self.root))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"schema": "camol.connection_registry", "schema_version": 1, "connections": normalized}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(str(temporary_path), str(self.path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def load(self) -> List[Dict[str, Any]]:
        if not self.path.is_file() or self.path.is_symlink():
            return []
        if self.path.stat().st_size > 1 << 20:
            raise ConnectionError("connection registry is too large")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectionError("connection registry is malformed") from error
        if not isinstance(payload, dict) or set(payload) != {"schema", "schema_version", "connections"}:
            raise ConnectionError("connection registry has the wrong fields")
        if payload["schema"] != "camol.connection_registry" or payload["schema_version"] != 1:
            raise ConnectionError("connection registry schema is unsupported")
        if not isinstance(payload["connections"], list):
            raise ConnectionError("connection registry connections must be an array")
        return [validate_connection(record) for record in payload["connections"]]

    @staticmethod
    def login_argv(provider: str) -> List[str]:
        if provider == "claude":
            executable = shutil.which("claude")
            if not executable:
                raise ConnectionError("Claude CLI is not installed")
            return [executable, "auth", "login"]
        if provider == "codex":
            executable = shutil.which("codex")
            if not executable:
                raise ConnectionError("Codex CLI is not installed")
            return [executable, "login"]
        raise ConnectionError("/login supports claude or codex; API keys stay in environment/keychain references")
