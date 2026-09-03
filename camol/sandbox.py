"""Explicit process sandbox policies and execution backends."""

import asyncio
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .readiness import TRUST_TIERS
from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_digest,
    require_optional_timestamp,
    require_schema_header,
    require_string,
    require_string_list,
    require_timestamp,
)


class SandboxError(RuntimeError):
    """A process could not be launched under the requested sandbox policy."""


def process_start_fingerprint(pid: int) -> str:
    """Return a stable-enough OS birth marker used to reject PID reuse."""
    executable = "/bin/ps" if Path("/bin/ps").is_file() else shutil.which("ps")
    if not executable or type(pid) is not int or pid <= 1:
        raise SandboxError("cannot establish process-start identity")
    try:
        result = subprocess.run(
            [executable, "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxError("cannot establish process-start identity") from error
    marker = result.stdout.decode("ascii", "replace").strip()
    if result.returncode != 0 or not marker:
        raise SandboxError("cannot establish process-start identity")
    return marker


INVOCATION_STATES = frozenset({
    "active", "completed", "terminated", "timed_out", "orphan_dead", "terminated_by_supervisor",
})
INVOCATION_FIELDS = (
    "schema", "schema_version", "state", "owner_pid", "pid", "pgid", "process_started",
    "cwd", "argv_digest", "policy_digest", "started_at", "finished_at", "exit_code",
)


def validate_process_invocation(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate the durable identity/lifecycle record for one child."""
    if not isinstance(payload, dict):
        raise SandboxError("process invocation must be an object")
    require_schema_header(payload, "camol.process_invocation", 1, "process invocation")
    reject_unknown_fields(payload, INVOCATION_FIELDS, "process invocation")
    missing = sorted(set(INVOCATION_FIELDS) - set(payload))
    if missing:
        raise SandboxError("process invocation is missing fields: {}".format(", ".join(missing)))
    if payload["state"] not in INVOCATION_STATES:
        raise SandboxError("process invocation state is invalid")
    for name in ("owner_pid", "pid", "pgid"):
        if type(payload[name]) is not int or payload[name] <= 1:
            raise SandboxError("process invocation {} must be a safe process id".format(name))
    if payload["pgid"] != payload["pid"]:
        raise SandboxError("process invocation must identify its own process group")
    require_string(payload["process_started"], "process invocation start fingerprint")
    cwd = Path(require_string(payload["cwd"], "process invocation cwd"))
    if not cwd.is_absolute():
        raise SandboxError("process invocation cwd must be absolute")
    require_digest(payload["argv_digest"], "process invocation argv digest")
    require_digest(payload["policy_digest"], "process invocation policy digest")
    require_timestamp(payload["started_at"], "process invocation started_at")
    require_optional_timestamp(payload["finished_at"], "process invocation finished_at")
    if payload["exit_code"] is not None and type(payload["exit_code"]) is not int:
        raise SandboxError("process invocation exit_code must be an integer or null")
    if payload["state"] == "active" and (
        payload["finished_at"] is not None or payload["exit_code"] is not None
    ):
        raise SandboxError("active process invocation cannot have a terminal result")
    if payload["state"] in {"completed", "terminated", "timed_out"} and payload["finished_at"] is None:
        raise SandboxError("finished process invocation needs finished_at")
    return dict(payload)


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
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    started_at: str
    finished_at: str
    backend: str
    policy_digest: str
    process_id: int
    process_group_id: int


class SandboxBackend:
    name = "sandbox"
    max_capture_bytes = 1 << 20

    async def _drain(self, stream: asyncio.StreamReader) -> Tuple[bytes, str, int, bool]:
        retained = bytearray()
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if len(retained) < self.max_capture_bytes:
                retained.extend(chunk[: self.max_capture_bytes - len(retained)])
        return bytes(retained), "sha256:" + digest.hexdigest(), total, total > len(retained)

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        environment: Optional[Mapping[str, str]] = None,
        stdin_bytes: Optional[bytes] = None,
        invocation_record: Optional[Path] = None,
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
        stdin_bytes: Optional[bytes],
        invocation_record: Optional[Path],
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
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            if invocation_record is not None:
                try:
                    process_started = process_start_fingerprint(process.pid)
                except SandboxError:
                    # A very short-lived command can be reaped before ``ps``
                    # sees it. It cannot be a live orphan, so retain an explicit
                    # marker; a running child without a fingerprint is unsafe.
                    await asyncio.sleep(0)
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
                        raise
                    process_started = "exited-before-os-observation"
                try:
                    self._write_invocation(
                        invocation_record,
                        policy,
                        {
                            "schema": "camol.process_invocation", "schema_version": 1,
                            "state": "active", "owner_pid": os.getpid(), "pid": process.pid,
                            "pgid": process.pid, "process_started": process_started,
                            "cwd": str(resolved_cwd),
                            "argv_digest": canonical_digest(list(recorded_argv)),
                            "policy_digest": policy.digest(), "started_at": started,
                            "finished_at": None, "exit_code": None,
                        },
                    )
                except Exception:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    raise
            stdout_task = asyncio.create_task(self._drain(process.stdout))
            stderr_task = asyncio.create_task(self._drain(process.stderr))
            if stdin_bytes is not None:
                process.stdin.write(stdin_bytes)
                await process.stdin.drain()
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError as error:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                await asyncio.gather(stdout_task, stderr_task)
                if invocation_record is not None:
                    self._finish_invocation(invocation_record, "timed_out", process.returncode)
                raise SandboxError("sandboxed process timed out") from error
            except asyncio.CancelledError:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                await asyncio.gather(stdout_task, stderr_task)
                if invocation_record is not None:
                    self._finish_invocation(invocation_record, "terminated", process.returncode)
                raise
            stdout_capture, stderr_capture = await asyncio.gather(stdout_task, stderr_task)
            if invocation_record is not None:
                self._finish_invocation(invocation_record, "completed", process.returncode)
        except OSError as error:
            raise SandboxError("sandboxed process could not launch: {}".format(error.__class__.__name__)) from error
        finished = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return SandboxResult(
            argv=tuple(recorded_argv),
            cwd=str(resolved_cwd),
            exit_code=process.returncode,
            stdout=stdout_capture[0],
            stderr=stderr_capture[0],
            stdout_sha256=stdout_capture[1],
            stderr_sha256=stderr_capture[1],
            stdout_bytes=stdout_capture[2],
            stderr_bytes=stderr_capture[2],
            stdout_truncated=stdout_capture[3],
            stderr_truncated=stderr_capture[3],
            started_at=started,
            finished_at=finished,
            backend=self.name,
            policy_digest=policy.digest(),
            process_id=process.pid,
            process_group_id=process.pid,
        )

    @staticmethod
    def _write_invocation(path: Path, policy: SandboxPolicy, payload: Dict[str, Any]) -> None:
        payload = validate_process_invocation(payload)
        destination = Path(path)
        if destination.exists() and destination.is_symlink():
            raise SandboxError("invocation record must not be a symlink")
        if destination.is_file():
            try:
                previous = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SandboxError("existing invocation record is malformed") from error
            if previous.get("state") == "active":
                raise SandboxError("an earlier invocation is still unresolved; supervisor reconciliation is required")
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = Path(_real(str(destination.parent)))
        if not any(
            parent == Path(root) or parent.is_relative_to(Path(root))
            for root in policy.write_paths
        ):
            raise SandboxError("invocation record must be inside a frozen write path")
        descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=str(parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(destination))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _finish_invocation(self, path: Path, state: str, exit_code: Optional[int]) -> None:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SandboxError("active invocation record could not be recovered") from error
        payload.update(
            state=state,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            exit_code=exit_code,
        )
        payload = validate_process_invocation(payload)
        # The policy digest and destination were validated at creation. Use a
        # minimal reconstructed policy-free atomic replacement in the same dir.
        descriptor, temporary = tempfile.mkstemp(prefix=Path(path).name + ".", dir=str(Path(path).parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class DeveloperTrustedBackend(SandboxBackend):
    """Visible unsandboxed compatibility backend; never claims sandboxed trust."""

    name = "developer_trusted"

    async def run(self, argv, *, cwd, policy, timeout_seconds, environment=None, stdin_bytes=None, invocation_record=None):
        if policy.trust_tier != "developer_trusted":
            raise SandboxError("unsandboxed backend cannot satisfy a sandboxed trust tier")
        return await self._spawn(
            argv, argv, cwd=cwd, policy=policy, timeout_seconds=timeout_seconds, environment=environment,
            stdin_bytes=stdin_bytes,
            invocation_record=invocation_record,
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
        # Runtime launchers commonly realpath their own executable. Permit
        # metadata-only traversal of ancestors without granting content reads
        # outside the explicit roots (notably for venvs below /private/tmp).
        ancestors = set()
        for item in policy.read_paths + policy.write_paths:
            cursor = Path(item).parent
            while str(cursor) not in {"", "/"}:
                ancestors.add(str(cursor))
                cursor = cursor.parent
        for path in sorted(ancestors):
            rules.append('(allow file-read-metadata (literal "{}"))'.format(_escape_profile(path)))
        for path in policy.read_paths:
            rules.append('(allow file-read* (subpath "{}"))'.format(_escape_profile(path)))
        for path in policy.write_paths:
            rules.append('(allow file-write* (subpath "{}"))'.format(_escape_profile(path)))
        if policy.network_destinations == ("*",):
            rules.append("(allow network*)")
        return " ".join(rules)

    async def run(self, argv, *, cwd, policy, timeout_seconds, environment=None, stdin_bytes=None, invocation_record=None):
        if not self.available():
            raise SandboxError("macOS sandbox-exec is unavailable")
        if policy.trust_tier == "developer_trusted":
            raise SandboxError("sandbox backend requires a sandboxed trust tier")
        effective = ["/usr/bin/sandbox-exec", "-p", self.profile(policy)] + list(argv)
        return await self._spawn(
            effective, argv, cwd=cwd, policy=policy, timeout_seconds=timeout_seconds, environment=environment,
            stdin_bytes=stdin_bytes,
            invocation_record=invocation_record,
        )


def system_read_paths(executable: Optional[str] = None) -> Tuple[str, ...]:
    """Conservative platform runtime roots; never includes the user's whole home."""
    candidates = ["/System", "/usr", "/bin", "/sbin", "/Library"]
    if executable:
        lexical = Path(os.path.abspath(executable))
        resolved = Path(_real(executable))
        candidates.append(str(lexical.parent))
        candidates.append(str(resolved.parent))
    return tuple(sorted({_real(item) for item in candidates if Path(item).exists()}))


def select_backend(policy: SandboxPolicy) -> SandboxBackend:
    if policy.trust_tier == "developer_trusted":
        return DeveloperTrustedBackend()
    if MacOSSandboxBackend.available():
        return MacOSSandboxBackend()
    raise SandboxError("no enforcing sandbox backend is available for trust tier {}".format(policy.trust_tier))
