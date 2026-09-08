"""Versioned provider profiles and short-lived capability receipts.

The orchestration kernel deals only in these records.  A provider adapter may
create a capability receipt, but a profile never proves authentication, quota,
or model entitlement by declaring them.
"""

import json
import math
import os
import re
import shutil
import subprocess
from importlib import resources
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .probes import Redactor, sanitize_identifier
from .schema import canonical_digest, reject_unknown_fields, require_schema_header


class ProviderError(RuntimeError):
    """A provider profile, receipt, or invocation is invalid."""


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProviderError("{} must be an integer >= {}".format(label, minimum))
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("{} must be a non-empty string".format(label))
    return value


def _strings(value: Any, label: str, *, empty: bool = True) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ProviderError("{} must be an array of non-empty strings".format(label))
    if not empty and not value:
        raise ProviderError("{} must not be empty".format(label))
    if len(value) != len(set(value)):
        raise ProviderError("{} must not contain duplicates".format(label))
    return tuple(value)


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ProviderError("{} must be an ISO-8601 timestamp".format(label)) from error
    if parsed.tzinfo is None:
        raise ProviderError("{} must include a timezone".format(label))
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ModelProfile:
    """Frozen model and budget policy; not evidence that the model exists."""

    SCHEMA = "camol.model_profile"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "profile_id", "provider", "adapter_kind",
        "runtime_binary", "requested_model", "allowed_resolved_models", "effort",
        "permission_mode", "allowed_tools", "max_agent_turns", "max_turn_tokens",
        "max_turn_usd_cents", "max_task_usd_cents", "max_run_usd_cents",
        "capability_ttl_seconds", "network_destinations", "credential_refs",
        "credential_read_paths", "maturity",
    )

    profile_id: str
    provider: str
    adapter_kind: str
    runtime_binary: str
    requested_model: str
    allowed_resolved_models: Tuple[str, ...]
    effort: str
    permission_mode: str
    allowed_tools: Tuple[str, ...]
    max_agent_turns: int
    max_turn_tokens: int
    max_turn_usd_cents: int
    max_task_usd_cents: int
    max_run_usd_cents: int
    capability_ttl_seconds: int
    network_destinations: Tuple[str, ...]
    credential_refs: Tuple[str, ...]
    credential_read_paths: Tuple[str, ...]
    maturity: str
    execution_policy: Optional[Dict[str, str]] = None
    local_provider: Optional[str] = None
    local_endpoint: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("profile_id", "provider", "adapter_kind"):
            value = _string(getattr(self, name), "model profile " + name)
            if sanitize_identifier(value, "") != value:
                raise ProviderError("model profile {} must be an identifier".format(name))
        _string(self.runtime_binary, "model profile runtime_binary")
        if "/" in self.runtime_binary:
            raise ProviderError("model profile runtime_binary must be a PATH-resolved name")
        _string(self.requested_model, "model profile requested_model")
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ProviderError("model profile effort is unsupported")
        if self.permission_mode not in {"plan", "acceptEdits", "dontAsk", "default"}:
            raise ProviderError("model profile permission_mode is unsupported")
        if self.maturity not in {"SPECULATIVE", "PROVISIONAL", "BACKED"}:
            raise ProviderError("model profile maturity is unsupported")
        for name in (
            "max_agent_turns", "max_turn_tokens", "max_turn_usd_cents",
            "max_task_usd_cents", "max_run_usd_cents", "capability_ttl_seconds",
        ):
            _integer(getattr(self, name), "model profile " + name, minimum=1)
        if self.max_turn_usd_cents > self.max_task_usd_cents or self.max_task_usd_cents > self.max_run_usd_cents:
            raise ProviderError("model profile cost ceilings must satisfy turn <= task <= run")
        if tuple(sorted(set(self.network_destinations))) not in ((), ("*",)):
            raise ProviderError("model profile network_destinations must be [] or ['*']")
        if any(item.startswith("-") or any(ord(character) < 32 for character in item) for item in self.allowed_tools):
            raise ProviderError("model profile allowed_tools contains an invalid tool name")
        for item in self.credential_read_paths:
            path = Path(item.replace("{home}/", ""))
            if not item.startswith("{home}/") or ".." in path.parts:
                raise ProviderError("credential_read_paths must be descendants of {home}")
        if self.credential_read_paths and not self.credential_refs:
            raise ProviderError("credential_read_paths require an opaque credential reference")
        if self.execution_policy is not None:
            from .codex_policy import validate_codex_policy
            validate_codex_policy(self)
        elif self.adapter_kind in {"codex_cli", "codex_oss"} or self.local_provider is not None or self.local_endpoint is not None:
            raise ProviderError("Codex workers require a schema2 explicit execution_policy")

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "provider": self.provider,
            "adapter_kind": self.adapter_kind,
            "runtime_binary": self.runtime_binary,
            "requested_model": self.requested_model,
            "allowed_resolved_models": list(self.allowed_resolved_models),
            "effort": self.effort,
            "permission_mode": self.permission_mode,
            "allowed_tools": list(self.allowed_tools),
            "max_agent_turns": self.max_agent_turns,
            "max_turn_tokens": self.max_turn_tokens,
            "max_turn_usd_cents": self.max_turn_usd_cents,
            "max_task_usd_cents": self.max_task_usd_cents,
            "max_run_usd_cents": self.max_run_usd_cents,
            "capability_ttl_seconds": self.capability_ttl_seconds,
            "network_destinations": list(self.network_destinations),
            "credential_refs": list(self.credential_refs),
            "credential_read_paths": list(self.credential_read_paths),
            "maturity": self.maturity,
        }
        if self.execution_policy is not None:
            result.update(schema_version=2, execution_policy=dict(self.execution_policy),
                          local_provider=self.local_provider, local_endpoint=self.local_endpoint)
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModelProfile":
        if not isinstance(payload, dict):
            raise ProviderError("model profile must be an object")
        version = payload.get("schema_version")
        if type(version) is not int or version not in {1, 2}:
            raise ProviderError("model profile schema_version must be 1 or 2")
        require_schema_header(payload, cls.SCHEMA, version, "model profile")
        fields = set(cls.FIELDS) | ({"execution_policy", "local_provider", "local_endpoint"} if version == 2 else set())
        reject_unknown_fields(payload, fields, "model profile")
        missing = sorted(fields - set(payload))
        if missing:
            raise ProviderError("model profile is missing fields: {}".format(", ".join(missing)))
        values = {key: payload[key] for key in fields if key not in {"schema", "schema_version"}}
        if version == 2 and payload["execution_policy"] is None:
            raise ProviderError("schema2 execution_policy cannot be null")
        for name in (
            "allowed_resolved_models", "allowed_tools", "network_destinations",
            "credential_refs", "credential_read_paths",
        ):
            values[name] = _strings(values[name], "model profile " + name, empty=name != "allowed_resolved_models")
        return cls(**values)


def load_model_profile(workspace: Path, relative: str) -> ModelProfile:
    from .json_contracts import decode_contract, load_contract
    from .schema import SchemaError
    if relative.startswith("@camol/"):
        profile_id = relative[len("@camol/"):]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile_id):
            raise ProviderError("built-in model profile id is invalid")
        try:
            payload = resources.files("camol").joinpath(
                "assets", "profiles", profile_id + ".json"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as error:
            raise ProviderError("built-in model profile is unavailable") from error
        try:
            return ModelProfile.from_dict(decode_contract(payload))
        except SchemaError as error:
            raise ProviderError("built-in model profile is malformed") from error
    root = Path(workspace).resolve()
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ProviderError("model profile path must stay inside the workspace")
    lexical = root / raw
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProviderError("model profile path must not traverse a symlink")
    path = lexical.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ProviderError("model profile path escapes the workspace") from error
    if not path.is_file():
        raise ProviderError("model profile must be a regular non-symlink file")
    try:
        return ModelProfile.from_dict(load_contract(path))
    except (OSError, SchemaError) as error:
        raise ProviderError("model profile is not valid JSON-compatible YAML") from error


def model_profile_for_adapter(workspace: Path, adapter: Mapping[str, Any]) -> ModelProfile:
    """Resolve the exact effective policy frozen into a hosted adapter."""
    snapshot = adapter.get("profile_snapshot")
    if snapshot is not None:
        profile = ModelProfile.from_dict(snapshot)
        if profile.adapter_kind != adapter.get("kind"):
            raise ProviderError("frozen model profile kind does not match the adapter")
        return profile
    return load_model_profile(workspace, adapter["profile"])


@dataclass(frozen=True)
class ProviderCapabilityReceipt:
    """Short-lived proof from an explicit, spend-capped provider preflight."""

    SCHEMA = "camol.provider_capability"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "receipt_id", "profile_digest", "target_id",
        "runtime_version", "requested_model", "resolved_model", "authenticated",
        "model_available", "quota_available", "policy_allowed", "max_usd_cents",
        "cost_usd_micros", "input_tokens", "output_tokens", "request_digest",
        "observed_at", "expires_at",
    )

    receipt_id: str
    profile_digest: str
    target_id: str
    runtime_version: str
    requested_model: str
    resolved_model: str
    authenticated: bool
    model_available: bool
    quota_available: bool
    policy_allowed: bool
    max_usd_cents: int
    cost_usd_micros: int
    input_tokens: int
    output_tokens: int
    request_digest: str
    observed_at: str
    expires_at: str

    def __post_init__(self) -> None:
        for name in ("receipt_id", "target_id"):
            _string(getattr(self, name), "provider receipt " + name)
        for name in ("profile_digest", "request_digest"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(getattr(self, name))):
                raise ProviderError("provider receipt {} must be a sha256 digest".format(name))
        for name in ("runtime_version", "requested_model", "resolved_model"):
            _string(getattr(self, name), "provider receipt " + name)
        for name in ("authenticated", "model_available", "quota_available", "policy_allowed"):
            if type(getattr(self, name)) is not bool:
                raise ProviderError("provider receipt {} must be boolean".format(name))
        for name in ("max_usd_cents", "cost_usd_micros", "input_tokens", "output_tokens"):
            _integer(getattr(self, name), "provider receipt " + name)
        observed = _timestamp(self.observed_at, "provider receipt observed_at")
        expires = _timestamp(self.expires_at, "provider receipt expires_at")
        if observed >= expires:
            raise ProviderError("provider receipt validity window is empty")

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION, **{
            name: getattr(self, name) for name in self.FIELDS if name not in {"schema", "schema_version"}
        }}

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def valid_for(self, profile: ModelProfile, target_id: str, now: datetime) -> Tuple[bool, str]:
        if self.profile_digest != profile.digest():
            return False, "capability receipt names a different profile digest"
        if self.target_id != target_id:
            return False, "capability receipt names a different target"
        instant = now.astimezone(timezone.utc)
        if not (_timestamp(self.observed_at, "observed_at") <= instant < _timestamp(self.expires_at, "expires_at")):
            return False, "capability receipt is not currently fresh"
        if self.requested_model != profile.requested_model:
            return False, "capability receipt names a different requested model"
        if self.resolved_model not in profile.allowed_resolved_models:
            return False, "resolved model is outside the frozen allowlist"
        if not all((self.authenticated, self.model_available, self.quota_available, self.policy_allowed)):
            return False, "capability receipt contains a non-green provider decision"
        if self.max_usd_cents > profile.max_turn_usd_cents:
            return False, "capability preflight exceeded the frozen turn ceiling"
        return True, "provider capability receipt is fresh and profile-bound"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProviderCapabilityReceipt":
        if not isinstance(payload, dict):
            raise ProviderError("provider capability receipt must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "provider capability receipt")
        reject_unknown_fields(payload, cls.FIELDS, "provider capability receipt")
        missing = sorted(set(cls.FIELDS) - set(payload))
        if missing:
            raise ProviderError("provider capability receipt is missing fields: {}".format(", ".join(missing)))
        return cls(**{key: payload[key] for key in cls.FIELDS if key not in {"schema", "schema_version"}})


def capability_path(state_dir: Path, profile_id: str) -> Path:
    return Path(state_dir).resolve() / "provider-capabilities" / (sanitize_identifier(profile_id) + ".json")


def read_capability(state_dir: Path, profile: ModelProfile) -> Optional[ProviderCapabilityReceipt]:
    from .json_contracts import load_contract
    from .schema import SchemaError
    path = capability_path(state_dir, profile.profile_id)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return ProviderCapabilityReceipt.from_dict(load_contract(path))
    except (OSError, SchemaError, ProviderError):
        return None


def _resolved_model(payload: Mapping[str, Any]) -> Optional[str]:
    model_usage = payload.get("modelUsage") or payload.get("model_usage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        return next(iter(model_usage))
    model = payload.get("model") or payload.get("resolved_model")
    return model if isinstance(model, str) and model else None


def _usage(payload: Mapping[str, Any]) -> Tuple[int, int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError("provider response did not include a usage object")
    numbers = {}
    for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        value = usage.get(name, 0)
        if type(value) is not int or value < 0:
            raise ProviderError("provider usage {} must be a non-negative integer".format(name))
        numbers[name] = value
    input_tokens = numbers["input_tokens"] + numbers["cache_creation_input_tokens"] + numbers["cache_read_input_tokens"]
    output_tokens = numbers["output_tokens"]
    cost = payload.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(float(cost)) or cost < 0:
        raise ProviderError("provider response did not include a finite non-negative total_cost_usd")
    cost_micros = int(round(float(cost) * 1_000_000))
    return input_tokens, output_tokens, cost_micros


def create_claude_capability(
    profile: ModelProfile,
    *,
    target_id: str,
    state_dir: Path,
    cwd: Path,
    accept_spend: bool,
    spend_ceiling_cents: int = 10,
    now: Optional[datetime] = None,
    runner: Any = subprocess.run,
) -> ProviderCapabilityReceipt:
    """Run one explicit no-tools request and atomically persist its receipt.

    The caller must opt in.  This is intentionally not called by ``doctor`` or
    admission, because both are read-only and non-billable.
    """
    if profile.adapter_kind != "claude_cli" or profile.provider != "anthropic":
        raise ProviderError("Claude preflight requires an anthropic claude_cli profile")
    if not accept_spend:
        raise ProviderError("provider preflight requires explicit --accept-spend")
    runtime = shutil.which(profile.runtime_binary)
    if not runtime:
        raise ProviderError("provider runtime is not installed on PATH")
    _integer(spend_ceiling_cents, "provider preflight spend ceiling", minimum=1)
    maximum = min(profile.max_turn_usd_cents, spend_ceiling_cents)
    prompt = "Reply with exactly CAMOL_READY. Do not use tools."
    argv = [
        runtime, "-p", "--output-format", "json", "--model", profile.requested_model,
        "--effort", profile.effort, "--max-turns", "1", "--max-budget-usd",
        "{:.2f}".format(maximum / 100), "--permission-mode", "plan",
        "--permission-prompts", "none", "--no-session-persistence",
        "--disable-slash-commands", "--safe-mode", "--disallowedTools", "Bash", "Edit", "Write",
    ]
    clean_env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    try:
        completed = runner(
            argv, cwd=str(Path(cwd).resolve()), env=clean_env, input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderError("provider preflight could not run: {}".format(error.__class__.__name__)) from error
    if completed.returncode != 0:
        safe = Redactor().text(completed.stderr.decode("utf-8", "replace"))[:500]
        raise ProviderError("provider preflight failed: {}".format(safe or "non-zero exit"))
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("provider preflight did not return JSON") from error
    resolved = _resolved_model(payload)
    if not resolved:
        raise ProviderError("provider response did not prove the resolved model")
    if resolved not in profile.allowed_resolved_models:
        raise ProviderError("provider resolved model {!r}, outside the profile allowlist".format(resolved))
    input_tokens, output_tokens, cost_micros = _usage(payload)
    if cost_micros > maximum * 10_000:
        raise ProviderError("provider reported cost above the preflight ceiling")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    receipt = ProviderCapabilityReceipt(
        receipt_id="provider-" + canonical_digest({"profile": profile.digest(), "at": observed.isoformat()})[7:31],
        profile_digest=profile.digest(),
        target_id=target_id,
        runtime_version="observed-by-successful-preflight",
        requested_model=profile.requested_model,
        resolved_model=resolved,
        authenticated=True,
        model_available=True,
        quota_available=True,
        policy_allowed=True,
        max_usd_cents=maximum,
        cost_usd_micros=cost_micros,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_digest=canonical_digest({"argv": Redactor().argv(argv), "prompt": prompt}),
        observed_at=observed.isoformat(timespec="microseconds"),
        expires_at=(observed + timedelta(seconds=profile.capability_ttl_seconds)).isoformat(timespec="microseconds"),
    )
    path = capability_path(state_dir, profile.profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))
    return receipt
