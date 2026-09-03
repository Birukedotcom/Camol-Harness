"""Provider-neutral discovery that never persists provider credentials."""

import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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


def open_without_proxy(request: Any, *, timeout: int) -> Any:
    """Open a loopback request without consulting proxy environment state."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)


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
        try:
            return subprocess.run(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._environment(), timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConnectionError("connection probe failed: {}".format(error.__class__.__name__)) from error

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
        url = validate_loopback_endpoint(endpoint) + "/models"
        try:
            with open_without_proxy(url, timeout=2) as response:
                if response.status != 200:
                    raise ConnectionError("local model endpoint returned status {}".format(response.status))
                payload = json.loads(response.read(1 << 20).decode("utf-8"))
            models = payload.get("data", []) if isinstance(payload, dict) else []
            count = len(models) if isinstance(models, list) else 0
            return _record(
                "local-openai", "local", "openai_compatible", status="ready", auth_method="none",
                credential_ref="none", capabilities=("conversation", "worker"), runtime=endpoint,
                detail="Local OpenAI-compatible endpoint reported {} model(s)".format(count),
            )
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError, ConnectionError):
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
            try:
                records.append(probe())
            except (ConnectionError, OSError, ValueError) as error:
                records.append(_record(
                    connection_id, provider, kind, status="error", runtime=runtime,
                    detail="{} probe failed safely: {}".format(connection_id, error.__class__.__name__),
                ))
        self.save(records)
        return records

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
