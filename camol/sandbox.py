"""Explicit process sandbox policies and execution backends."""

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .readiness import TRUST_TIERS
from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_schema_header,
    require_string_list,
)


class SandboxError(RuntimeError):
    """A process could not be launched under the requested sandbox policy."""


def _real(path: str) -> str:
    return os.path.realpath(path)


def _escape_profile(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class SandboxPolicy:
    """Versioned, digestible authority actually enforced for one invocation.

    Network is either denied (empty destinations) or explicitly unrestricted
    (``("*",)``).  Hostname-only allowlists would be misleading at this layer
    because macOS Seatbelt filters sockets by resolved address, not stable DNS
    identity; a provider proxy can provide a narrower future backend.
    """

    SCHEMA = "camol.sandbox_policy"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "policy_id", "workspace", "read_paths",
        "write_paths", "environment_names", "network_destinations",
        "credential_refs", "trust_tier",
    )

    policy_id: str
    workspace: str
    read_paths: Tuple[str, ...]
    write_paths: Tuple[str, ...]
    environment_names: Tuple[str, ...]
    network_destinations: Tuple[str, ...]
    credential_refs: Tuple[str, ...]
    trust_tier: str

    def __post_init__(self) -> None:
        if not self.policy_id or any(character.isspace() for character in self.policy_id):
            raise SandboxError("sandbox policy_id must be a non-empty token")
        if self.trust_tier not in TRUST_TIERS:
            raise SandboxError("unknown sandbox trust tier")
        workspace = _real(self.workspace)
        reads = tuple(sorted(set(_real(item) for item in self.read_paths)))
        writes = tuple(sorted(set(_real(item) for item in self.write_paths)))
        names = tuple(sorted(set(self.environment_names)))
        network = tuple(sorted(set(self.network_destinations)))
        credentials = tuple(sorted(set(self.credential_refs)))
        if workspace not in reads or workspace not in writes:
            raise SandboxError("sandbox workspace must be explicitly readable and writable")
        if any(not name or "=" in name for name in names):
            raise SandboxError("environment allowlist contains an invalid name")
        if network not in ((), ("*",)):
            raise SandboxError("this sandbox backend supports only denied or explicitly unrestricted network")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "read_paths", reads)
        object.__setattr__(self, "write_paths", writes)
        object.__setattr__(self, "environment_names", names)
        object.__setattr__(self, "network_destinations", network)
        object.__setattr__(self, "credential_refs", credentials)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "workspace": self.workspace,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "environment_names": list(self.environment_names),
            "network_destinations": list(self.network_destinations),
            "credential_refs": list(self.credential_refs),
            "trust_tier": self.trust_tier,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SandboxPolicy":
        if not isinstance(payload, dict):
            raise SandboxError("sandbox policy must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "sandbox policy")
        reject_unknown_fields(payload, cls.FIELDS, "sandbox policy")
        missing = sorted(set(cls.FIELDS) - set(payload))
        if missing:
            raise SandboxError("sandbox policy is missing fields: {}".format(", ".join(missing)))
        for field in (
            "read_paths", "write_paths", "environment_names", "network_destinations", "credential_refs"
        ):
            require_string_list(payload[field], "sandbox policy " + field)
        return cls(
            policy_id=payload["policy_id"],
            workspace=payload["workspace"],
            read_paths=tuple(payload["read_paths"]),
            write_paths=tuple(payload["write_paths"]),
            environment_names=tuple(payload["environment_names"]),
            network_destinations=tuple(payload["network_destinations"]),
            credential_refs=tuple(payload["credential_refs"]),
            trust_tier=payload["trust_tier"],
        )

    def environment(self, overrides: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        source = dict(os.environ)
        if overrides:
            source.update(overrides)
        return {name: source[name] for name in self.environment_names if name in source}


@dataclass(frozen=True)
class SandboxResult:
    argv: Tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: bytes
    stderr: bytes
    started_at: str
    finished_at: str
    backend: str
    policy_digest: str


class SandboxBackend:
    name = "sandbox"

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        environment: Optional[Mapping[str, str]] = None,
    ) -> SandboxResult:
        raise NotImplementedError

    async def _spawn(
        self,
        effective_argv: Sequence[str],
        recorded_argv: Sequence[str],
        *,
        cwd: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        environment: Optional[Mapping[str, str]],
    ) -> SandboxResult:
        resolved_cwd = Path(_real(str(cwd)))
        try:
            resolved_cwd.relative_to(Path(policy.workspace))
        except ValueError as error:
            raise SandboxError("sandbox cwd must stay inside the policy workspace") from error
        started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            process = await asyncio.create_subprocess_exec(
                *effective_argv,
                cwd=str(resolved_cwd),
                env=policy.environment(environment),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError as error:
                process.kill()
                await process.wait()
                raise SandboxError("sandboxed process timed out") from error
        except OSError as error:
            raise SandboxError("sandboxed process could not launch: {}".format(error.__class__.__name__)) from error
        finished = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return SandboxResult(
            argv=tuple(recorded_argv),
            cwd=str(resolved_cwd),
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            started_at=started,
            finished_at=finished,
            backend=self.name,
            policy_digest=policy.digest(),
        )


class DeveloperTrustedBackend(SandboxBackend):
    """Visible unsandboxed compatibility backend; never claims sandboxed trust."""

    name = "developer_trusted"

    async def run(self, argv, *, cwd, policy, timeout_seconds, environment=None):
        if policy.trust_tier != "developer_trusted":
            raise SandboxError("unsandboxed backend cannot satisfy a sandboxed trust tier")
        return await self._spawn(
            argv, argv, cwd=cwd, policy=policy, timeout_seconds=timeout_seconds, environment=environment
        )


class MacOSSandboxBackend(SandboxBackend):
    """Seatbelt backend with read/write roots and deny-by-default networking."""

    name = "macos-seatbelt"

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()

    @staticmethod
    def profile(policy: SandboxPolicy) -> str:
        # Apple's base profile supplies the runtime/Mach primitives required by
        # ordinary command-line programs while retaining deny-by-default file
        # and network behavior.  It is materially narrower than ``allow
        # default`` and lets the following roots be the only content access.
        rules = ["(version 1)", '(import "system.sb")', "(allow process*)"]
        for path in policy.read_paths:
            rules.append('(allow file-read* (subpath "{}"))'.format(_escape_profile(path)))
        for path in policy.write_paths:
            rules.append('(allow file-write* (subpath "{}"))'.format(_escape_profile(path)))
        if policy.network_destinations == ("*",):
            rules.append("(allow network*)")
        return " ".join(rules)

    async def run(self, argv, *, cwd, policy, timeout_seconds, environment=None):
        if not self.available():
            raise SandboxError("macOS sandbox-exec is unavailable")
        if policy.trust_tier == "developer_trusted":
            raise SandboxError("sandbox backend requires a sandboxed trust tier")
        effective = ["/usr/bin/sandbox-exec", "-p", self.profile(policy)] + list(argv)
        return await self._spawn(
            effective, argv, cwd=cwd, policy=policy, timeout_seconds=timeout_seconds, environment=environment
        )


def system_read_paths(executable: Optional[str] = None) -> Tuple[str, ...]:
    """Conservative platform runtime roots; never includes the user's whole home."""
    candidates = ["/System", "/usr", "/bin", "/sbin", "/Library"]
    if executable:
        resolved = Path(_real(executable))
        candidates.append(str(resolved.parent))
    return tuple(sorted({_real(item) for item in candidates if Path(item).exists()}))


def select_backend(policy: SandboxPolicy) -> SandboxBackend:
    if policy.trust_tier == "developer_trusted":
        return DeveloperTrustedBackend()
    if MacOSSandboxBackend.available():
        return MacOSSandboxBackend()
    raise SandboxError("no enforcing sandbox backend is available for trust tier {}".format(policy.trust_tier))
