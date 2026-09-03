"""Read-only readiness probes and their registry (M1).

Every probe here observes; none prepares. A probe may run a fixed, allowlisted
read-only command (``git --version``, ``git rev-parse``, ``git status
--porcelain``, ``<PATH interpreter> --version``), inspect the filesystem, or
open and immediately close a TCP connection. No probe installs, downloads,
authenticates, provisions, writes to a repository or the state directory, sends
a model request, or mutates a remote service. Anything that would need one of
those is reported as a typed non-green result naming the preparation action.

Execution guard and environment sanitization
--------------------------------------------
Every subprocess goes through :class:`GuardedRunner`, which refuses argv[0]
resolving under the workspace or the state directory (lexically and after
symlink resolution, relative to the command's working directory) and any argv
equal to a runbook adapter, step, or verification command. Version queries are
made only for PATH-resolved binaries on a small allowlist. Workspace-relative
scripts are checked for existence and file type, never launched.

Subprocesses receive a sanitized environment: only ``PATH``, ``HOME``, ``TMPDIR``
and a fixed C locale, with every ``GIT_*`` variable dropped. Git commands are
additionally run with ``GIT_OPTIONAL_LOCKS=0``, ``GIT_CONFIG_NOSYSTEM=1``,
``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_TERMINAL_PROMPT=0`` and with
``core.fsmonitor``, ``core.untrackedCache``, ``core.hooksPath`` and
``core.sshCommand`` overridden on the command line, so a repository that
configures an fsmonitor hook or hook path cannot execute anything when the
doctor reads it.

Probe definitions
-----------------
Each probe exposes ``definition(context)``: its implementation identity, a
``VERSION``, and the configuration it observed under. The digest of that record
is stamped on every :class:`ProbeResult` and pinned in the :class:`ProbePolicy`,
so a policy binds the exact probe that must have run, not merely its id.

Adapter policy
--------------
Requiredness of the provider/model and network probes derives from the adapter:
only a local adapter whose interpreter is a PATH-resolved, allowlisted binary
with a parsed version is *verified*. Any other adapter (hosted CLI, unknown
binary, unversioned interpreter) is *unverified*, and for it the provider
connection and network policy probes become required. Because no provider probe
adapter exists yet, such candidates cannot be green.

The registry is vendor-neutral: provider- and target-specific probes are
adapters registered against an adapter kind, not branches in this module. All
output passes through :class:`Redactor`.
"""

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .readiness import ProbeResult
from .schema import ID_PATTERN, SchemaError, canonical_digest

__all__ = [
    "ProbeExecutionError",
    "Redactor",
    "CommandOutcome",
    "run_command",
    "sanitized_environment",
    "GIT_SAFETY_ARGS",
    "GuardedRunner",
    "ProbeContext",
    "ProbeOutcome",
    "Probe",
    "ProbeRegistry",
    "default_registry",
    "sanitize_identifier",
    "local_target_id",
    "adapter_policy",
    "REDACTED",
]


class ProbeExecutionError(RuntimeError):
    """Camol could not reliably perform or interpret a probe (doctor exit code 3)."""


# --------------------------------------------------------------------------- redaction

REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(r"(SECRET|TOKEN|PASSW|API_?KEY|COOKIE|AUTH|CREDENTIAL|PRIVATE_?KEY|SESSION)", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_HEADER = re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*[^\n]+")
_SECRET_KV = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:secret|token|passw(?:or)?d|api[_-]?key|cookie|authorization|credential|private[_-]?key)[A-Za-z0-9_-]*)(\s*[=:]\s*)(\S+)"
)
_SECRET_FLAG = re.compile(r"(?i)^--?[A-Za-z0-9_-]*(?:secret|token|passw(?:or)?d|api[_-]?key|cookie|auth|credential)[A-Za-z0-9_-]*$")
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_TOKEN_SHAPES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
]


class Redactor:
    """Removes credential material from any string before it is recorded."""

    def __init__(self, env: Optional[Mapping[str, str]] = None, minimum_length: int = 6):
        source = os.environ if env is None else env
        self._values = sorted(
            {value for name, value in source.items() if _SECRET_NAME.search(name) and len(value) >= minimum_length},
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        text = value
        for secret in self._values:
            text = text.replace(secret, REDACTED)
        text = _PEM_BLOCK.sub(REDACTED, text)
        text = _URL_USERINFO.sub(lambda match: "{}{}@".format(match.group("scheme"), REDACTED), text)
        text = _COOKIE_HEADER.sub(lambda match: "{}: {}".format(match.group(1), REDACTED), text)
        text = _AUTH_SCHEME.sub(lambda match: "{} {}".format(match.group(1), REDACTED), text)
        text = _SECRET_KV.sub(lambda match: "{}{}{}".format(match.group(1), match.group(2), REDACTED), text)
        for shape in _TOKEN_SHAPES:
            text = shape.sub(REDACTED, text)
        return text

    def argv(self, argv: Sequence[str]) -> List[str]:
        redacted: List[str] = []
        mask_next = False
        for item in argv:
            if mask_next:
                redacted.append(REDACTED)
                mask_next = False
                continue
            if _SECRET_FLAG.match(item):
                redacted.append(item)
                mask_next = True
                continue
            if "=" in item and _SECRET_FLAG.match(item.split("=", 1)[0]):
                redacted.append(item.split("=", 1)[0] + "=" + REDACTED)
                continue
            redacted.append(self.text(item))
        return redacted

    def mapping(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: self.value(item) for key, item in value.items()}

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return self.mapping(value)
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class CommandOutcome:
    argv: Tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], Optional[Path], int], CommandOutcome]
Which = Callable[[str], Optional[str]]

_ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
}
# Command-line config overrides win over repository config, so a repo that sets
# core.fsmonitor to a script, or a custom hooks path, cannot run anything.
GIT_SAFETY_ARGS = (
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "core.sshCommand=false",
    "-c", "core.pager=cat",
    "-c", "protocol.allow=never",
)


def sanitized_environment(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A minimal environment for probes: PATH/HOME/TMPDIR, C locale, no GIT_* variables."""
    base = os.environ if source is None else source
    env = {name: base[name] for name in _ENV_PASSTHROUGH if name in base}
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env.update(GIT_ENV)
    return env


def run_command(argv: Sequence[str], cwd: Optional[Path], timeout_seconds: int = 20) -> CommandOutcome:
    """Run a command without a shell in a sanitized environment. Launch failure is a probe failure."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=sanitized_environment(),
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeExecutionError("could not run {}: {}".format(Path(argv[0]).name, error.__class__.__name__))
    return CommandOutcome(
        argv=tuple(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace"),
        stderr=completed.stderr.decode("utf-8", "replace"),
    )


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _nested(path: Path, root: Path) -> bool:
    """True when ``path`` is at or below ``root`` in either lexical or resolved form."""
    return any(_inside(candidate, base) for candidate in (_lexical(path), _real(path)) for base in (_lexical(root), _real(root)))


class GuardedRunner:
    """Wraps a command runner so nothing under a guarded root or from the runbook can run."""

    def __init__(self, inner: CommandRunner, roots: Sequence[Path], forbidden_argv: Sequence[Sequence[str]]):
        self._inner = inner
        self._roots = [Path(root) for root in roots]
        self._forbidden = {tuple(argv) for argv in forbidden_argv}

    def __call__(self, argv: Sequence[str], cwd: Optional[Path], timeout_seconds: int = 20) -> CommandOutcome:
        if not argv:
            raise ProbeExecutionError("refused to run an empty command")
        head = argv[0]
        if "/" in head:
            base = Path(cwd) if cwd is not None else Path.cwd()
            resolved = Path(head) if os.path.isabs(head) else base / head
        else:
            found = shutil.which(head)
            if found is None:
                raise ProbeExecutionError("refused to run {}: not found on PATH".format(head))
            resolved = Path(found)
        for root in self._roots:
            if _nested(resolved, root):
                raise ProbeExecutionError("refused to run {}: it resolves inside a guarded root".format(Path(head).name))
        if tuple(argv) in self._forbidden:
            raise ProbeExecutionError("refused to run a runbook task command from doctor")
        return self._inner(argv, cwd, timeout_seconds)


def runbook_commands(runbook: Dict[str, Any], workspace: Path) -> List[Tuple[str, ...]]:
    """Every adapter, step, and verification argv (raw and workspace-substituted)."""
    commands: List[Tuple[str, ...]] = []

    def add(argv: Sequence[str]) -> None:
        commands.append(tuple(argv))
        commands.append(tuple(item.replace("{workspace}", str(workspace)) for item in argv))

    for agent in runbook["agents"]:
        add(agent["adapter"]["argv"])
    for task in runbook["tasks"]:
        for step in task["steps"]:
            for command in step["commands"]:
                add(command["argv"])
        for command in task["verification"]:
            add(command["argv"])
    return commands


# --------------------------------------------------------------------------- context


def sanitize_identifier(text: str, fallback: str = "unnamed") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:/@-]", "-", text).strip("-")
    if not cleaned or not ID_PATTERN.fullmatch(cleaned):
        cleaned = fallback
    return cleaned


def local_target_id() -> str:
    return "local:" + sanitize_identifier(socket.gethostname(), "host")


VERSION_ALLOWLIST = ("python3", "python", "git", "node", "bash", "sh", "npm", "pnpm", "cargo", "go")


@dataclass(frozen=True)
class ProbeContext:
    """Everything a probe may look at. Immutable for the whole doctor run."""

    runbook: Dict[str, Any]
    workspace: Path
    state_dir: Path
    now: datetime
    ttl_seconds: int
    target_id: str
    redactor: Redactor
    runner: CommandRunner
    which: Which = shutil.which
    services: Tuple[str, ...] = ()
    min_free_bytes: int = 1 << 30
    version_allowlist: Tuple[str, ...] = VERSION_ALLOWLIST

    @classmethod
    def guarded(cls, *, runner: CommandRunner = run_command, **values: Any) -> "ProbeContext":
        workspace = Path(values["workspace"])
        state_dir = Path(values["state_dir"])
        guarded = GuardedRunner(runner, [workspace, state_dir], runbook_commands(values["runbook"], workspace))
        return cls(runner=guarded, **values)

    def observed_at(self) -> str:
        return self.now.isoformat(timespec="microseconds")

    def expires_at(self) -> str:
        return (self.now + timedelta(seconds=self.ttl_seconds)).isoformat(timespec="microseconds")

    def path_binary(self, binary: str) -> Optional[str]:
        """Resolve a bare binary name through PATH only; never a path inside a guarded root."""
        if "/" in binary or binary.startswith("{"):
            return None
        resolved = self.which(binary)
        if resolved is None:
            return None
        if _nested(Path(resolved), self.workspace) or _nested(Path(resolved), self.state_dir):
            return None
        return resolved

    def version_of(self, binary: str, resolved: str) -> Tuple[Optional[str], Tuple[str, ...]]:
        """``<resolved> --version`` for allowlisted PATH binaries only."""
        if Path(binary).name not in self.version_allowlist or "/" in binary:
            return None, ()
        argv = (resolved, "--version")
        outcome = self.runner(argv, None, 20)
        lines = (outcome.stdout or outcome.stderr).strip().splitlines()
        return (lines[0] if lines and outcome.exit_code == 0 else None), argv

    def git(self, *args: str, timeout_seconds: int = 20) -> Optional[CommandOutcome]:
        """Run a read-only git command with safety overrides; None when git is unavailable."""
        git = self.path_binary("git")
        if git is None:
            return None
        return self.runner((git,) + GIT_SAFETY_ARGS + tuple(args), None, timeout_seconds)

    def workspace_path(self, item: str) -> Path:
        """Resolve a (possibly relative) runbook path against the target workspace, never the process cwd."""
        substituted = item.replace("{workspace}", str(self.workspace))
        path = Path(substituted)
        return path if path.is_absolute() else self.workspace / path

    def protected_roots(self) -> List[Path]:
        """Roots nothing may live in: the workspace and its git common dir (when it is a repository)."""
        roots = [self.workspace]
        if _real(self.workspace).is_dir():
            common = self.git("-C", str(self.workspace), "rev-parse", "--git-common-dir")
            if common is not None and common.exit_code == 0 and common.stdout.strip():
                common_dir = Path(common.stdout.strip())
                roots.append(common_dir if common_dir.is_absolute() else self.workspace / common_dir)
        return roots


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _executable_file(path: Path) -> bool:
    return _regular_file(path) and os.access(str(path), os.X_OK)


def adapter_policy(agent: Dict[str, Any], context: ProbeContext) -> Dict[str, Any]:
    """Classify an adapter: verifiable local interpreter, or unverified/hosted.

    Only a ``process`` adapter whose argv[0] is a bare, allowlisted binary name
    is *verifiable*; verification itself (a parsed version) happens in the
    adapter probe. Everything else requires provider/model and network proof.
    """
    adapter = agent["adapter"]
    argv0 = adapter["argv"][0] if adapter.get("argv") else ""
    verifiable = adapter.get("kind") == "process" and "/" not in argv0 and not argv0.startswith("{") and argv0 in context.version_allowlist
    return {
        "kind": adapter.get("kind"),
        "interpreter": argv0,
        "verifiable_local_interpreter": verifiable,
        "requires_provider_proof": not verifiable,
        "requires_network_policy": not verifiable,
    }


@dataclass(frozen=True)
class ProbeOutcome:
    result: ProbeResult
    facts: Dict[str, Any] = field(default_factory=dict)


class Probe:
    """Base class for a read-only probe. Subclasses implement ``observe``."""

    probe_id = "probe"
    kind = "generic"
    target_bound = True
    #: Bump when the observation logic changes; part of the definition digest.
    VERSION = 1
    #: Informational probes are required only for unverified adapters (see adapter_policy).
    required = True

    def observe(self, context: ProbeContext) -> ProbeOutcome:  # pragma: no cover - abstract
        raise NotImplementedError

    def config(self, context: ProbeContext) -> Dict[str, Any]:
        """Configuration the observation depends on; included in the definition digest."""
        return {}

    def definition(self, context: ProbeContext) -> Dict[str, Any]:
        return {
            "implementation": "{}.{}".format(type(self).__module__, type(self).__qualname__),
            "version": self.VERSION,
            "probe_id": self.probe_id,
            "kind": self.kind,
            "target_bound": self.target_bound,
            "config": self.config(context),
        }

    def definition_digest(self, context: ProbeContext) -> str:
        return canonical_digest(self.definition(context))

    def green(self, context, summary, *, method="in_process", command=(), tool_version=None, facts=None):
        return self._outcome(context, "green", summary, method=method, command=command, tool_version=tool_version, facts=facts)

    def red(self, context, summary, *, reason, wake, missing, method="in_process", command=(), tool_version=None, facts=None):
        return self._outcome(context, "red", summary, reason=reason, wake=wake, missing=missing, method=method, command=command, tool_version=tool_version, facts=facts)

    def unknown(self, context, summary, *, reason, wake, missing, method="in_process", command=(), facts=None):
        return self._outcome(context, "unknown", summary, reason=reason, wake=wake, missing=missing, method=method, command=command, facts=facts)

    def _outcome(self, context, status, summary, *, reason=None, wake=None, missing=(), method="in_process", command=(), tool_version=None, facts=None):
        redactor = context.redactor
        safe_facts = redactor.mapping(facts or {})
        result = ProbeResult(
            probe_id=self.probe_id,
            kind=self.kind,
            target_id=context.target_id if self.target_bound else None,
            status=status,
            observed_at=context.observed_at(),
            expires_at=context.expires_at() if status == "green" else None,
            method="process" if command else method,
            command=redactor.argv(command),
            tool_version=redactor.text(tool_version) if tool_version else None,
            summary=redactor.text(summary),
            missing_requirements=[redactor.text(item) for item in missing],
            evidence_digest=canonical_digest(safe_facts) if safe_facts else None,
            reason_code=reason,
            wake_condition=redactor.text(wake) if wake else None,
            definition_digest=self.definition_digest(context),
        )
        return ProbeOutcome(result=result, facts=safe_facts)


# --------------------------------------------------------------------------- probes


class StateDirProbe(Probe):
    probe_id = "control-plane.state-dir"
    kind = "control_plane"

    def observe(self, context):
        state_dir = context.state_dir
        facts = {"state_dir": str(_lexical(state_dir)), "state_dir_resolved": str(_real(state_dir)), "workspace": str(_real(context.workspace))}
        roots = context.protected_roots()
        facts["protected_roots"] = [str(_real(root)) for root in roots]
        for root in roots:
            if _nested(state_dir, root) or _nested(root, state_dir):
                return self.red(context, "state directory must live outside the source repository (checked lexical and resolved paths, including the git common dir)", reason="POLICY_DENIED", wake="pass --state-dir pointing outside the workspace and its git dir", missing=["external state directory"], method="filesystem", facts=facts)
        resolved = _real(state_dir)
        if not resolved.exists():
            return self.red(context, "state directory does not exist", reason="OPERATOR_ATTENTION", wake="create the state directory (preparation action; doctor does not create it)", missing=["state directory {}".format(_lexical(state_dir))], method="filesystem", facts=facts)
        if not resolved.is_dir():
            return self.red(context, "state directory path is not a directory", reason="OPERATOR_ATTENTION", wake="replace the path with a directory", missing=["directory at {}".format(_lexical(state_dir))], method="filesystem", facts=facts)
        if not os.access(str(resolved), os.W_OK | os.X_OK):
            return self.red(context, "state directory is not writable", reason="OPERATOR_ATTENTION", wake="grant write access to the state directory", missing=["write access to {}".format(_lexical(state_dir))], method="filesystem", facts=facts)
        return self.green(context, "state directory exists, is writable, and is outside the source repository", method="filesystem", facts=facts)


class ArtifactSinkProbe(Probe):
    """``<state-dir>/artifacts`` must be a real (non-symlink) writable directory inside the
    state dir, or absent under a writable state directory. A symlinked sink, or one
    resolving into the workspace, the git common dir, or outside the state dir, is rejected."""

    probe_id = "control-plane.artifact-sink"
    kind = "control_plane"
    VERSION = 2

    def observe(self, context):
        state_dir = _real(context.state_dir)
        sink = _lexical(context.state_dir) / "artifacts"
        facts = {"artifact_sink": str(sink), "state_dir_resolved": str(state_dir), "exists": sink.exists() or sink.is_symlink()}
        for root in context.protected_roots():
            if _nested(context.state_dir, root):
                return self.red(context, "artifact sink would live inside a protected root (workspace or git dir) because the state directory resolves there", reason="POLICY_DENIED", wake="move the state directory outside the repository", missing=["artifact sink outside protected roots"], method="filesystem", facts=facts)
        if sink.is_symlink():
            facts["resolves_to"] = str(_real(sink))
            return self.red(context, "artifact sink is a symlink; Camol will not write evidence through a link", reason="POLICY_DENIED", wake="replace <state-dir>/artifacts with a real directory", missing=["non-symlink artifact sink"], method="filesystem", facts=facts)
        if sink.exists():
            resolved = _real(sink)
            facts["resolves_to"] = str(resolved)
            for root in context.protected_roots():
                if _nested(resolved, root):
                    return self.red(context, "artifact sink resolves inside a protected root (workspace or git dir)", reason="POLICY_DENIED", wake="move the state directory outside the repository", missing=["artifact sink outside protected roots"], method="filesystem", facts=facts)
            if not _inside(resolved, state_dir):
                return self.red(context, "artifact sink resolves outside the state directory", reason="POLICY_DENIED", wake="make <state-dir>/artifacts a plain directory under the state dir", missing=["artifact sink inside the state directory"], method="filesystem", facts=facts)
            if not resolved.is_dir() or not os.access(str(resolved), os.W_OK | os.X_OK):
                return self.red(context, "artifact sink exists but is not a writable directory", reason="OPERATOR_ATTENTION", wake="make <state-dir>/artifacts a writable directory", missing=["writable artifact sink"], method="filesystem", facts=facts)
            return self.green(context, "artifact sink is a writable directory inside the state directory", method="filesystem", facts=facts)
        if state_dir.is_dir() and os.access(str(state_dir), os.W_OK | os.X_OK):
            return self.green(context, "artifact sink is absent; the writable state directory can hold it (created at first write, not by doctor)", method="filesystem", facts=facts)
        return self.red(context, "artifact sink cannot be created: state directory is missing or not writable", reason="OPERATOR_ATTENTION", wake="create a writable state directory", missing=["writable state directory for artifacts"], method="filesystem", facts=facts)


class GitBinaryProbe(Probe):
    probe_id = "source.git"
    kind = "runtime"

    def observe(self, context):
        resolved = context.path_binary("git")
        if not resolved:
            return self.red(context, "git is not installed on PATH", reason="NEEDS_DOWNLOAD", wake="install git (preparation action)", missing=["git"])
        version, argv = context.version_of("git", resolved)
        if not version:
            raise ProbeExecutionError("git --version produced no parseable output")
        return self.green(context, "git is available", command=argv, tool_version=version, facts={"git": version, "path": resolved})


class RepositoryProbe(Probe):
    probe_id = "source.repository"
    kind = "source"
    VERSION = 2  # sanitized environment + config overrides

    def config(self, context):
        return {"git_safety_args": list(GIT_SAFETY_ARGS), "git_env": sorted(GIT_ENV)}

    def observe(self, context):
        if not context.path_binary("git"):
            return self.red(context, "cannot inspect the repository without git", reason="NEEDS_DOWNLOAD", wake="install git", missing=["git"])
        workspace = _real(context.workspace)
        if not workspace.is_dir():
            return self.red(context, "workspace path is not a directory", reason="TARGET_UNREACHABLE", wake="pass --workspace pointing at the source repository", missing=["workspace directory"], method="filesystem", facts={"workspace": str(workspace)})
        toplevel = context.git("-C", str(workspace), "rev-parse", "--show-toplevel")
        if toplevel.exit_code != 0:
            return self.red(context, "workspace is not inside a Git repository", reason="WORKSPACE_CONFLICT", wake="pass --workspace pointing at a Git checkout", missing=["git repository at the workspace"], command=toplevel.argv, facts={"workspace": str(workspace)})
        root = _real(Path(toplevel.stdout.strip()))
        if root != workspace:
            return self.red(context, "workspace must be the repository root, not a subdirectory", reason="WORKSPACE_CONFLICT", wake="pass --workspace {}".format(root), missing=["workspace == repository root"], command=toplevel.argv, facts={"workspace": str(workspace), "toplevel": str(root)})
        head = context.git("-C", str(workspace), "rev-parse", "HEAD")
        if head.exit_code != 0:
            return self.red(context, "repository has no commit at HEAD", reason="WORKSPACE_CONFLICT", wake="commit an initial revision", missing=["committed HEAD revision"], command=head.argv, facts={"workspace": str(workspace)})
        revision = head.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise ProbeExecutionError("unparseable HEAD revision from git")
        branch = context.git("-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        remote = context.git("-C", str(workspace), "remote", "get-url", "origin")
        repository_id = context.redactor.text(remote.stdout.strip()) if remote.exit_code == 0 and remote.stdout.strip() else "local:" + workspace.name
        status = context.git("-C", str(workspace), "status", "--porcelain", "--untracked-files=all", timeout_seconds=60)
        if status.exit_code != 0:
            raise ProbeExecutionError("git status exited {}".format(status.exit_code))
        dirty = sorted(line for line in status.stdout.splitlines() if line.strip())
        facts = {"repository_id": repository_id, "base_revision": revision, "branch": branch, "workspace": str(workspace), "dirty_entries": len(dirty), "dirty_digest": canonical_digest(dirty)}
        if dirty:
            return self.red(context, "source checkout has {} uncommitted or untracked entries; a dirty checkout is rejected by default".format(len(dirty)), reason="WORKSPACE_CONFLICT", wake="commit, stash, or clean the checkout (Camol never snapshots it silently)", missing=["clean source checkout"], command=status.argv, facts=facts)
        return self.green(context, "clean checkout at {} on {}".format(revision[:12], branch), command=status.argv, facts=facts)


class PythonRuntimeProbe(Probe):
    probe_id = "runtime.python"
    kind = "runtime"

    def observe(self, context):
        version = "Python {}".format(platform.python_version())
        facts = {"executable": sys.executable, "version": platform.python_version(), "implementation": platform.python_implementation()}
        return self.green(context, "control-plane Python runtime observed in-process", tool_version=version, facts=facts)


def _looks_like_path(raw: str) -> bool:
    return "{workspace}" in raw or raw.startswith(("./", "../", "/"))


def _check_binary(binary_raw: str, context: ProbeContext) -> Tuple[Optional[str], Optional[str]]:
    """(resolved path, problem). Path-form binaries must be regular executable files resolved against the workspace."""
    if "/" in binary_raw or binary_raw.startswith("{"):
        path = context.workspace_path(binary_raw)
        if not path.exists() and not path.is_symlink():
            return None, "{} does not exist".format(path)
        if not _executable_file(path):
            return None, "{} is not a regular executable file".format(path)
        return str(path), None
    resolved = context.path_binary(binary_raw)
    return resolved, (None if resolved else "{} is not on PATH".format(binary_raw))


class AdapterBinaryProbe(Probe):
    """Adapter launchability without execution.

    A verifiable interpreter (bare allowlisted name on PATH) has its version
    queried and is green when every workspace-relative script argument is a
    regular file. A path-form or unknown binary is checked for being a regular
    executable file but reported ``unknown``: its identity cannot be proven
    without running it, and its candidates additionally require provider and
    network proof (see ``adapter_policy``).
    """

    kind = "adapter"
    VERSION = 2

    def __init__(self, agent: Dict[str, Any]):
        self.agent = agent
        self.probe_id = "adapter." + sanitize_identifier(agent["id"])

    def config(self, context):
        return {"argv": list(self.agent["adapter"]["argv"]), "version_allowlist": list(context.version_allowlist)}

    def observe(self, context):
        adapter = self.agent["adapter"]
        policy = adapter_policy(self.agent, context)
        if adapter.get("kind") != "process":
            return self.unknown(context, "no probe adapter registered for adapter kind {!r}".format(adapter.get("kind")), reason="OPERATOR_ATTENTION", wake="register a probe adapter for this adapter kind", missing=["probe adapter for {}".format(adapter.get("kind"))], facts={"policy": policy})
        raw = list(adapter["argv"])
        missing: List[str] = []
        for item in raw[1:]:
            if item.startswith("{") and "{workspace}" not in item:
                continue
            if _looks_like_path(item):
                path = context.workspace_path(item)
                if not _regular_file(path):
                    missing.append("{} (regular file)".format(path))
        resolved, problem = _check_binary(raw[0], context)
        facts: Dict[str, Any] = {"argv": raw, "resolved_binary": resolved, "policy": policy}
        if problem:
            missing.insert(0, problem)
        if missing:
            return self.red(context, "adapter command for agent {} is not launchable".format(self.agent["id"]), reason="NEEDS_DOWNLOAD", wake="install or restore: {}".format("; ".join(missing)), missing=missing, method="filesystem", facts=facts)
        if not policy["verifiable_local_interpreter"]:
            return self.unknown(
                context,
                "adapter binary {} for agent {} exists but is not a verifiable local interpreter; doctor will not execute it and its identity is unproven".format(raw[0], self.agent["id"]),
                reason="POLICY_DENIED",
                wake="use a PATH-resolved allowlisted interpreter, or register a provider/adapter probe that proves identity, connection, and model entitlement without execution",
                missing=["verified adapter runtime identity"],
                method="filesystem",
                facts=facts,
            )
        version, version_argv = context.version_of(raw[0], resolved)
        facts["version"] = version
        if not version:
            return self.unknown(context, "interpreter {} for agent {} did not report a parseable version".format(raw[0], self.agent["id"]), reason="OPERATOR_ATTENTION", wake="install a version-reporting interpreter", missing=["parseable interpreter version"], command=version_argv, facts=facts)
        return self.green(context, "adapter interpreter {} for agent {} is launchable; script arguments are regular files".format(raw[0], self.agent["id"]), command=version_argv, tool_version=version, facts=facts)


def _check_command_binaries(commands: Sequence[Sequence[str]], context: ProbeContext) -> Tuple[Dict[str, Optional[str]], List[str]]:
    resolved_map: Dict[str, Optional[str]] = {}
    problems: List[str] = []
    for argv in commands:
        binary = argv[0]
        if binary in resolved_map:
            continue
        resolved, problem = _check_binary(binary, context)
        resolved_map[binary] = resolved
        if problem:
            problems.append(problem)
    return {name: resolved_map[name] for name in sorted(resolved_map)}, sorted(problems)


class ToolsProbe(Probe):
    probe_id = "tools.commands"
    kind = "tool"
    VERSION = 2

    def observe(self, context):
        commands = [command["argv"] for task in context.runbook["tasks"] for step in task["steps"] for command in step["commands"]]
        resolved_map, missing = _check_command_binaries(commands, context)
        facts = {"binaries": resolved_map}
        if missing:
            return self.red(context, "{} task command binaries are not available".format(len(missing)), reason="NEEDS_DOWNLOAD", wake="install or restore: {}".format("; ".join(missing)), missing=missing, method="filesystem", facts=facts)
        return self.green(context, "all {} distinct task command binaries resolve (not executed)".format(len(resolved_map)), method="filesystem", facts=facts)


class EvaluatorBundleProbe(Probe):
    probe_id = "evaluator.bundle"
    kind = "evaluator"
    VERSION = 2

    @staticmethod
    def bundle(runbook: Dict[str, Any]) -> Dict[str, Any]:
        return {task["id"]: task["verification"] for task in runbook["tasks"]}

    def observe(self, context):
        bundle = self.bundle(context.runbook)
        digest = canonical_digest(bundle)
        resolved_map, missing = _check_command_binaries([command["argv"] for commands in bundle.values() for command in commands], context)
        facts = {"evaluator_digest": digest, "tasks": len(bundle), "binaries": resolved_map}
        if missing:
            return self.red(context, "evaluator commands are not launchable", reason="EVALUATOR_NOT_READY", wake="install or restore: {}".format("; ".join(missing)), missing=missing, method="filesystem", facts=facts)
        return self.green(context, "evaluator bundle for {} tasks is frozen (digest recorded) and launchable; not executed".format(len(bundle)), method="filesystem", facts=facts)


class DiskHeadroomProbe(Probe):
    probe_id = "resources.disk"
    kind = "resource"

    def config(self, context):
        return {"min_free_bytes": context.min_free_bytes}

    def observe(self, context):
        target = _real(context.state_dir)
        probe_path = target if target.exists() else target.parent
        try:
            usage = shutil.disk_usage(str(probe_path))
        except OSError:
            return self.unknown(context, "could not measure free disk at the state directory", reason="OPERATOR_ATTENTION", wake="make the state directory path reachable", missing=["reachable state directory filesystem"], method="filesystem", facts={"path": str(probe_path)})
        free_gib = usage.free // (1 << 30)
        facts = {"path": str(probe_path), "free_gib_floor": free_gib, "min_free_bytes": context.min_free_bytes}
        if usage.free < context.min_free_bytes:
            return self.red(context, "free disk (~{} GiB) is below the required {} bytes".format(free_gib, context.min_free_bytes), reason="CAPACITY_EXHAUSTED", wake="free disk space or lower --min-free-bytes", missing=["{} more free bytes".format(context.min_free_bytes - usage.free)], method="filesystem", facts=facts)
        return self.green(context, "at least {} GiB free at the state directory".format(free_gib), method="filesystem", facts=facts)


class CapacityProbe(Probe):
    probe_id = "capacity.local"
    kind = "capacity"

    def observe(self, context):
        run = context.runbook["run"]
        max_concurrency = run.get("max_concurrency", run.get("max_agents"))
        cpus = os.cpu_count() or 0
        facts = {"cpu_count": cpus, "max_concurrency": max_concurrency, "registered_workers": len(context.runbook["agents"]), "reservation_ledger": "none (M3)"}
        if cpus < 1:
            return self.unknown(context, "could not determine CPU count", reason="OPERATOR_ATTENTION", wake="expose CPU count to the control plane", missing=["CPU count"], facts=facts)
        return self.green(context, "{} CPUs observed for a concurrency ceiling of {}; no reservations exist yet".format(cpus, max_concurrency), facts=facts)


class NetworkPolicyProbe(Probe):
    probe_id = "network.policy"
    kind = "network"
    required = False

    def observe(self, context):
        tiers = sorted({agent.get("trust_tier", "developer_trusted") for agent in context.runbook["agents"]})
        return self.unknown(context, "no sandbox exists yet; the process adapter runs with unconstrained egress (trust tiers: {})".format(", ".join(tiers)), reason="OPERATOR_ATTENTION", wake="M2 sandbox boundary with an explicit network policy", missing=["sandbox network policy"], facts={"trust_tiers": tiers})


class ProviderConnectionProbe(Probe):
    probe_id = "provider.connection"
    kind = "provider"
    required = False

    def observe(self, context):
        kinds = sorted({agent["adapter"]["kind"] for agent in context.runbook["agents"]})
        return self.unknown(
            context,
            "runbook declares no hosted-model connection and no provider probe adapter is registered for adapter kinds {}; provider auth and model availability are unproven, not assumed".format(", ".join(kinds)),
            reason="AUTH_REQUIRED",
            wake="register a provider probe adapter (M5) that checks connection, auth scope, and model entitlement without a billable request",
            missing=["provider connection declaration", "provider probe adapter"],
            facts={"adapter_kinds": kinds},
        )


class ServiceEndpointProbe(Probe):
    kind = "service"

    def __init__(self, endpoint: str):
        host, _, port = endpoint.rpartition(":")
        if not host or not port.isdigit():
            raise SchemaError("service endpoint must look like HOST:PORT, got {!r}".format(endpoint))
        self.host = host
        self.port = int(port)
        self.probe_id = "service." + sanitize_identifier("{}-{}".format(host, port))

    def config(self, context):
        return {"host": self.host, "port": self.port}

    def observe(self, context):
        facts = {"host": self.host, "port": self.port}
        try:
            with socket.create_connection((self.host, self.port), timeout=2):
                pass
        except OSError as error:
            return self.red(context, "service {}:{} is unreachable".format(self.host, self.port), reason="TARGET_UNREACHABLE", wake="start or expose the service", missing=["{}:{}".format(self.host, self.port)], method="socket", facts=dict(facts, error=error.__class__.__name__))
        return self.green(context, "service {}:{} accepted a TCP connection".format(self.host, self.port), method="socket", facts=facts)


# --------------------------------------------------------------------------- registry


class ProbeRegistry:
    def __init__(self) -> None:
        self._shared: List[Probe] = []
        self._agent_factories: List[Callable[[Dict[str, Any]], Probe]] = []
        self._service_factory: Optional[Callable[[str], Probe]] = None

    def register(self, probe: Probe) -> "ProbeRegistry":
        if any(existing.probe_id == probe.probe_id for existing in self._shared):
            raise SchemaError("duplicate probe id {}".format(probe.probe_id))
        self._shared.append(probe)
        return self

    def register_agent_factory(self, factory: Callable[[Dict[str, Any]], Probe]) -> "ProbeRegistry":
        self._agent_factories.append(factory)
        return self

    def register_service_factory(self, factory: Callable[[str], Probe]) -> "ProbeRegistry":
        self._service_factory = factory
        return self

    def shared_probes(self, context: ProbeContext) -> List[Probe]:
        probes = list(self._shared)
        if self._service_factory is not None:
            probes.extend(self._service_factory(endpoint) for endpoint in context.services)
        return probes

    def agent_probes(self, agent: Dict[str, Any]) -> List[Probe]:
        return [factory(agent) for factory in self._agent_factories]


def default_registry() -> ProbeRegistry:
    registry = ProbeRegistry()
    for probe in (
        StateDirProbe(),
        ArtifactSinkProbe(),
        GitBinaryProbe(),
        RepositoryProbe(),
        PythonRuntimeProbe(),
        ToolsProbe(),
        EvaluatorBundleProbe(),
        DiskHeadroomProbe(),
        CapacityProbe(),
        NetworkPolicyProbe(),
        ProviderConnectionProbe(),
    ):
        registry.register(probe)
    registry.register_agent_factory(AdapterBinaryProbe)
    registry.register_service_factory(ServiceEndpointProbe)
    return registry
