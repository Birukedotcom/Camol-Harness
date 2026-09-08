"""Explicit process sandbox policies and execution backends."""

import asyncio
import hashlib
import os
import signal
import shutil
import stat
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
    readonly_paths: Tuple[str, ...] = ()

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
        readonly = tuple(sorted(set(_real(item) for item in self.readonly_paths)))
        if readonly and self.trust_tier == "developer_trusted":
            raise SandboxError("developer_trusted cannot enforce read-only path exclusions")
        for item in readonly:
            if not any(Path(item) == Path(root) or Path(item).is_relative_to(Path(root)) for root in reads):
                raise SandboxError("read-only exclusions must be inside explicitly readable roots")
        if workspace not in reads:
            raise SandboxError("sandbox workspace must be explicitly readable")
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
        object.__setattr__(self, "readonly_paths", readonly)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "schema_version": 2 if self.readonly_paths else self.SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "workspace": self.workspace,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "environment_names": list(self.environment_names),
            "network_destinations": list(self.network_destinations),
            "credential_refs": list(self.credential_refs),
            "trust_tier": self.trust_tier,
        }
        if self.readonly_paths:
            payload["readonly_paths"] = list(self.readonly_paths)
        return payload

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SandboxPolicy":
        if not isinstance(payload, dict):
            raise SandboxError("sandbox policy must be an object")
        version = payload.get("schema_version")
        if type(version) is not int or version not in {1, 2}:
            raise SandboxError("unsupported sandbox policy version")
        require_schema_header(payload, cls.SCHEMA, version, "sandbox policy")
        fields = cls.FIELDS + (("readonly_paths",) if version == 2 else ())
        reject_unknown_fields(payload, fields, "sandbox policy")
        missing = sorted(set(fields) - set(payload))
        if missing:
            raise SandboxError("sandbox policy is missing fields: {}".format(", ".join(missing)))
        for field in (
            "read_paths", "write_paths", "environment_names", "network_destinations", "credential_refs"
        ):
            require_string_list(payload[field], "sandbox policy " + field)
        if version == 2:
            require_string_list(payload["readonly_paths"], "sandbox policy readonly_paths")
            if not payload["readonly_paths"]:
                raise SandboxError("sandbox policy v2 requires explicit read-only exclusions")
        return cls(
            policy_id=payload["policy_id"],
            workspace=payload["workspace"],
            read_paths=tuple(payload["read_paths"]),
            write_paths=tuple(payload["write_paths"]),
            environment_names=tuple(payload["environment_names"]),
            network_destinations=tuple(payload["network_destinations"]),
            credential_refs=tuple(payload["credential_refs"]),
            trust_tier=payload["trust_tier"],
            readonly_paths=tuple(payload.get("readonly_paths", ())),
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
    invocation_root = None

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
                except SandboxError as fingerprint_error:
                    # A very short-lived command can be reaped before ``ps``
                    # sees it. Give asyncio one bounded chance to observe that
                    # exit. A genuinely running child without a fingerprint is
                    # unsafe and must be terminated.
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
                        raise fingerprint_error
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
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    raise
            stdout_task = asyncio.create_task(self._drain(process.stdout))
            stderr_task = asyncio.create_task(self._drain(process.stderr))

            async def communicate_bounded():
                if stdin_bytes is not None:
                    try:
                        process.stdin.write(stdin_bytes)
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        # An early provider rejection can close stdin before
                        # accepting the complete prompt. Preserve its output.
                        pass
                    finally:
                        process.stdin.close()
                await process.wait()
                return await asyncio.gather(stdout_task, stderr_task)

            async def finish_stdin():
                if process.stdin is not None:
                    process.stdin.close()
                    try:
                        await process.stdin.wait_closed()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            try:
                stdout_capture, stderr_capture = await asyncio.wait_for(
                    communicate_bounded(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError as error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                await finish_stdin()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                if invocation_record is not None:
                    self._finish_invocation(invocation_record, "timed_out", process.returncode)
                raise SandboxError("sandboxed process timed out") from error
            except asyncio.CancelledError:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                await finish_stdin()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                if invocation_record is not None:
                    self._finish_invocation(invocation_record, "terminated", process.returncode)
                raise
            await finish_stdin()
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

    def _write_invocation(self, path: Path, policy: SandboxPolicy, payload: Dict[str, Any]) -> None:
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
        # Logging is a controller capability, not child write authority. The
        # host selects this root out-of-band; it is never added to the sandbox
        # policy. Legacy standalone backend callers may log in their workspace.
        roots = (str(self.invocation_root),) if self.invocation_root is not None else policy.write_paths
        if not any(
            parent == Path(root) or parent.is_relative_to(Path(root))
            for root in roots
        ):
            raise SandboxError("invocation record must be inside its explicit controller logging root")
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
            if path == str(_MACOS_DEVELOPER_SELECTOR.parent):
                # xcode-select reads an OS-owned symlink here. Permit link
                # metadata, not arbitrary contents of the selector directory.
                rules.append('(allow file-read-metadata (subpath "{}"))'.format(_escape_profile(path)))
                selected, links = _macos_developer_selection()
                if selected and selected[1] not in policy.read_paths:
                    raise SandboxError("selected macOS developer runtime changed since policy preparation")
                for link in links:
                    rules.append('(allow file-read-metadata (literal "{}"))'.format(_escape_profile(link)))
            else:
                rules.append('(allow file-read* (subpath "{}"))'.format(_escape_profile(path)))
        for path in policy.write_paths:
            rules.append('(allow file-write* (subpath "{}"))'.format(_escape_profile(path)))
        protected_parents = set()
        for path in policy.readonly_paths:
            rules.append('(deny file-write* (subpath "{}"))'.format(_escape_profile(path)))
            # Renaming an enclosing writable directory must not replace a
            # protected oracle via a fresh pathname. Protect ancestor entries
            # themselves without denying writes to unrelated child outputs.
            cursor = Path(path).parent
            while cursor != Path("/"):
                protected_parents.add(str(cursor))
                cursor = cursor.parent
        for path in sorted(protected_parents):
            rules.append('(deny file-write* (literal "{}"))'.format(_escape_profile(path)))
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


_MACOS_DEVELOPER_SELECTOR = Path("/private/var/select/developer_dir")
_MACOS_APPLICATIONS = Path("/Applications")
_MACOS_COMMAND_LINE_TOOLS = Path("/Library/Developer/CommandLineTools")


def _macos_developer_selection():
    """Read only the administrator-selected Xcode toolchain, never a user override.

    /usr/bin/git is an Apple launcher. CI selects a versioned Xcode app outside
    /Library; denying the selector makes that otherwise installed Git unusable.
    Resolve a bounded system-selected link chain without running xcode-select or
    honoring ambient DEVELOPER_DIR. A selector into user storage is not an
    implicit permission to read it. Xcode bundles installed by the current
    trusted host owner may be owner-owned (as on hosted CI); the system selector
    and its parent must still be root-owned. This is runtime discovery, not a
    root-only package immutability or publisher attestation claim.
    """
    if sys.platform != "darwin":
        return (), ()
    selector = _MACOS_DEVELOPER_SELECTOR
    try:
        selected = selector.lstat()
    except FileNotFoundError:
        # CommandLineTools installations commonly have no explicit selector.
        return (), ()

    def trusted(path, *, directory=True, container=False, owner_runtime=False):
        info = path.lstat()
        owners = {0, os.getuid()} if owner_runtime else {0}
        if (info.st_uid not in owners or (directory and not stat.S_ISDIR(info.st_mode))
                or (not directory and not stat.S_ISLNK(info.st_mode))
                or (directory and info.st_mode & (0o002 if container else 0o022))):
            raise SandboxError("selected macOS developer runtime has unsafe system/owner metadata")
        return info

    trusted(selector.parent)
    if selected.st_uid != 0 or not stat.S_ISLNK(selected.st_mode):
        raise SandboxError("macOS developer selector must be a root-owned symbolic link")
    target = os.readlink(selector)
    if not target or len(target) > 4096 or not Path(target).is_absolute() or ".." in Path(target).parts:
        raise SandboxError("macOS developer selector has an unsafe target")
    pending, traversed, links = Path(target), set(), []
    for _ in range(16):
        if str(pending) in traversed:
            raise SandboxError("macOS developer runtime link chain is cyclic")
        traversed.add(str(pending))
        if pending == _MACOS_COMMAND_LINE_TOOLS:
            trusted(pending)
            break
        try:
            relative = pending.relative_to(_MACOS_APPLICATIONS)
        except ValueError:
            raise SandboxError("selected developer runtime is outside supported system toolchain locations") from None
        if len(relative.parts) != 3 or not relative.parts[0].endswith(".app") or relative.parts[1:] != ("Contents", "Developer"):
            raise SandboxError("selected developer runtime is not a precise Xcode Developer directory")
        # The system Applications container can be admin-group writable. The
        # selected bundle and its actual runtime must not be group writable.
        trusted(_MACOS_APPLICATIONS, container=True)
        cursor, changed = _MACOS_APPLICATIONS, False
        for index, part in enumerate(relative.parts):
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode):
                trusted(cursor, directory=False, owner_runtime=True)
                links.append(str(cursor))
                link = Path(os.readlink(cursor))
                linked = link if link.is_absolute() else cursor.parent / link
                pending = Path(os.path.abspath(str(linked.joinpath(*relative.parts[index + 1:]))))
                changed = True
                break
            trusted(cursor, owner_runtime=True)
        if not changed:
            break
    else:
        raise SandboxError("macOS developer runtime link chain exceeds its safety bound")
    current = selector.lstat()
    if ((current.st_dev, current.st_ino) != (selected.st_dev, selected.st_ino)
            or current.st_uid != 0 or os.readlink(selector) != target):
        raise SandboxError("macOS developer selector changed during observation")
    return (str(selector.parent), str(pending)), tuple(links)


def system_read_paths(executable: Optional[str] = None) -> Tuple[str, ...]:
    """Conservative platform runtime roots; never includes the user's whole home."""
    candidates = ["/System", "/usr", "/bin", "/sbin", "/Library"]
    candidates.extend(_macos_developer_selection()[0])
    if executable:
        lexical = Path(os.path.abspath(executable))
        resolved = Path(_real(executable))
        for binary in (lexical, resolved):
            candidates.append(str(binary if binary.parent in {Path("/"), Path.home()} else binary.parent))
        # A venv may traverse several executable symlinks (venv -> shim ->
        # managed runtime). Seatbelt must read each intermediate link, not
        # merely the final image; granting a home-wide root is unnecessary.
        pending, traversed = lexical, set()
        for _ in range(64):
            if str(pending) in traversed:
                raise SandboxError("runtime executable symlink chain is cyclic")
            traversed.add(str(pending))
            changed = False
            cursor = Path(pending.anchor)
            parts = pending.parts[1:]
            for index, part in enumerate(parts):
                cursor = cursor / part
                if cursor.is_symlink():
                    if cursor.parent not in {Path("/"), Path.home()}:
                        candidates.append(str(cursor.parent))
                    target = Path(os.readlink(cursor))
                    target = target if target.is_absolute() else cursor.parent / target
                    pending = Path(os.path.abspath(str(target.joinpath(*parts[index + 1:]))))
                    changed = True
                    break
            if not changed:
                break
        else:
            raise SandboxError("runtime executable symlink chain exceeds the safety bound")
        # Managed Python/venv and similar CLI runtimes keep standard libraries
        # beside bin/, outside platform /usr roots. Read only those runtime
        # directories and config, never their entire parent or user's home.
        for binary in (lexical, resolved):
            if binary.parent.name in {"bin", "Scripts"}:
                prefix = binary.parent.parent
                for name in ("lib", "lib64", "Lib", "pyvenv.cfg"):
                    candidate = prefix / name
                    if candidate.exists():
                        candidates.append(str(candidate))
    return tuple(sorted({_real(item) for item in candidates if Path(item).exists()}))


def select_backend(policy: SandboxPolicy, *, invocation_root=None) -> SandboxBackend:
    if policy.trust_tier == "developer_trusted":
        backend = DeveloperTrustedBackend()
    elif MacOSSandboxBackend.available():
        backend = MacOSSandboxBackend()
    else:
        raise SandboxError("no enforcing sandbox backend is available for trust tier {}".format(policy.trust_tier))
    if invocation_root is not None:
        backend.invocation_root = Path(invocation_root).resolve()
    return backend
