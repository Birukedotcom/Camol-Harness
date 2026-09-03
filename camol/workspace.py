"""Isolated Git workspaces and salvage-gated cleanup.

The source checkout is an input.  Write-capable work happens in task worktrees
below an explicit state directory and integration happens in a distinct
orchestrator-owned worktree.  Workspace creation is idempotent through a small
persisted record; cleanup is impossible until the current diff and every
regular untracked file have been copied into the content-addressed salvage
store.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .probes import GIT_SAFETY_ARGS, Redactor, sanitized_environment
from .readiness import WorkspaceReceipt
from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_digest,
    require_identifier,
    require_non_negative_int,
    require_schema_header,
    require_string,
    require_timestamp,
)


class WorkspaceError(RuntimeError):
    """A workspace could not be created or proved safe."""


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _component(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value) or len(value) > 80:
        raise WorkspaceError("workspace identity must be a 1-80 character safe identifier")
    return value


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    try:
        _real(path).relative_to(_real(root))
        return True
    except ValueError:
        return False


def _nested(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _timestamp(value: Optional[str] = None) -> str:
    return require_timestamp(
        value or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "workspace timestamp",
    )


@dataclass(frozen=True)
class WorkspaceHandle:
    run_id: str
    task_id: str
    box_id: str
    path: Path
    branch: str
    receipt: WorkspaceReceipt
    integration: bool = False


@dataclass(frozen=True)
class SalvageReceipt:
    """Content-addressed proof captured before a Camol workspace is removed."""

    SCHEMA = "camol.salvage_receipt"
    SCHEMA_VERSION = 1

    salvage_id: str
    workspace_id: str
    workspace_digest: str
    base_revision: str
    head_revision: str
    patch_digest: str
    patch_bytes: int
    untracked: Tuple[Dict[str, Any], ...]
    created_at: str

    FIELDS = (
        "schema", "schema_version", "salvage_id", "workspace_id", "workspace_digest",
        "base_revision", "head_revision", "patch_digest", "patch_bytes", "untracked", "created_at",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "salvage_id", require_identifier(self.salvage_id, "salvage id"))
        object.__setattr__(self, "workspace_id", require_identifier(self.workspace_id, "salvage workspace id"))
        object.__setattr__(
            self, "workspace_digest", require_digest(self.workspace_digest, "salvage workspace digest")
        )
        object.__setattr__(self, "base_revision", require_string(self.base_revision, "salvage base revision"))
        object.__setattr__(self, "head_revision", require_string(self.head_revision, "salvage head revision"))
        object.__setattr__(self, "patch_digest", require_digest(self.patch_digest, "salvage patch digest"))
        object.__setattr__(self, "patch_bytes", require_non_negative_int(self.patch_bytes, "salvage patch bytes"))
        if not isinstance(self.untracked, tuple):
            raise WorkspaceError("salvage untracked must be a tuple")
        normalized = []
        seen = set()
        for index, item in enumerate(self.untracked):
            if not isinstance(item, dict):
                raise WorkspaceError("salvage untracked item must be an object")
            reject_unknown_fields(item, ("path", "digest", "bytes"), "salvage untracked item")
            if set(item) != {"path", "digest", "bytes"}:
                raise WorkspaceError("salvage untracked item is missing fields")
            path = require_string(item["path"], "salvage untracked path")
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or path in seen:
                raise WorkspaceError("salvage untracked path must be unique and workspace-relative")
            seen.add(path)
            normalized.append({
                "path": path,
                "digest": require_digest(item["digest"], "salvage untracked digest"),
                "bytes": require_non_negative_int(item["bytes"], "salvage untracked bytes"),
            })
        if tuple(item["path"] for item in normalized) != tuple(sorted(seen)):
            raise WorkspaceError("salvage untracked entries must be sorted by path")
        object.__setattr__(self, "untracked", tuple(normalized))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, "salvage created_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "salvage_id": self.salvage_id,
            "workspace_id": self.workspace_id,
            "workspace_digest": self.workspace_digest,
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "patch_digest": self.patch_digest,
            "patch_bytes": self.patch_bytes,
            "untracked": [dict(item) for item in self.untracked],
            "created_at": self.created_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SalvageReceipt":
        if not isinstance(payload, dict):
            raise WorkspaceError("salvage receipt must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "salvage receipt")
        reject_unknown_fields(payload, cls.FIELDS, "salvage receipt")
        missing = sorted(set(cls.FIELDS) - set(payload))
        if missing:
            raise WorkspaceError("salvage receipt is missing fields: {}".format(", ".join(missing)))
        if not isinstance(payload["untracked"], list):
            raise WorkspaceError("salvage untracked must be an array")
        return cls(
            salvage_id=payload["salvage_id"],
            workspace_id=payload["workspace_id"],
            workspace_digest=payload["workspace_digest"],
            base_revision=payload["base_revision"],
            head_revision=payload["head_revision"],
            patch_digest=payload["patch_digest"],
            patch_bytes=payload["patch_bytes"],
            untracked=tuple(payload["untracked"]),
            created_at=payload["created_at"],
        )


class WorkspaceManager:
    """Creates isolated task/integration worktrees below an external state root."""

    def __init__(self, source: Path, state_dir: Path, *, redactor: Optional[Redactor] = None):
        self.source = _real(Path(source))
        self.state_dir = _real(Path(state_dir))
        self.redactor = redactor or Redactor()
        if not self.source.is_dir():
            raise WorkspaceError("source repository does not exist")
        top = _real(Path(self._git("-C", str(self.source), "rev-parse", "--show-toplevel")))
        if top != self.source:
            raise WorkspaceError("source must be the Git repository root")
        common = Path(self._git("-C", str(self.source), "rev-parse", "--git-common-dir"))
        self.git_common_dir = _real(common if common.is_absolute() else self.source / common)
        for protected in (self.source, self.git_common_dir):
            if _nested(self.state_dir, protected):
                raise WorkspaceError("state directory must be outside the source repository and Git common directory")

    def _git_result(
        self, *args: str, check: bool = True, timeout: int = 120,
        input_bytes: Optional[bytes] = None,
    ) -> subprocess.CompletedProcess:
        git = shutil.which("git")
        if not git:
            raise WorkspaceError("git is not installed")
        try:
            result = subprocess.run(
                [git] + list(GIT_SAFETY_ARGS) + list(args),
                input=input_bytes,
                stdin=subprocess.DEVNULL if input_bytes is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_environment(),
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkspaceError("git operation could not run: {}".format(error.__class__.__name__)) from error
        if check and result.returncode != 0:
            detail = self.redactor.text(result.stderr.decode("utf-8", "replace").strip())
            raise WorkspaceError("git operation failed: {}".format(detail or "exit {}".format(result.returncode)))
        return result

    def _git(self, *args: str, check: bool = True, timeout: int = 120) -> str:
        return self._git_result(*args, check=check, timeout=timeout).stdout.decode("utf-8", "replace").strip()

    def _assert_clean_source(self) -> None:
        dirty = self._git("-C", str(self.source), "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise WorkspaceError("source checkout is dirty; Camol will not copy uncommitted state implicitly")

    def assert_source_ready(self) -> None:
        """Prove the immutable source input before any provider spend or worker launch."""
        self._assert_clean_source()
        self._git("-C", str(self.source), "rev-parse", "HEAD^{commit}")

    def _repository_id(self) -> str:
        remote = self._git("-C", str(self.source), "remote", "get-url", "origin", check=False)
        return self.redactor.text(remote) if remote else "local:" + self.source.name

    def _record_path(self, workspace_id: str) -> Path:
        return self.state_dir / "records" / "workspaces" / (workspace_id + ".json")

    @staticmethod
    def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
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

    def _branch_exists(self, branch: str) -> bool:
        result = self._git_result(
            "-C", str(self.source), "show-ref", "--verify", "--quiet", "refs/heads/" + branch, check=False
        )
        return result.returncode == 0

    def _load_existing(
        self, record_path: Path, *, run_id: str, task_id: str, box_id: str, integration: bool
    ) -> WorkspaceHandle:
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            if payload.get("run_id") != run_id or payload.get("task_id") != task_id or payload.get("box_id") != box_id:
                raise WorkspaceError("workspace record belongs to a different subject")
            if payload.get("integration") is not integration:
                raise WorkspaceError("workspace record has the wrong workspace role")
            receipt = WorkspaceReceipt.from_dict(payload["receipt"])
        except (OSError, ValueError, KeyError) as error:
            if isinstance(error, WorkspaceError):
                raise
            raise WorkspaceError("workspace record is invalid") from error
        path = _real(Path(receipt.path))
        if Path(receipt.path).is_symlink() or not path.is_dir() or not _inside(path, self.state_dir / "worktrees"):
            raise WorkspaceError("persisted workspace path is missing or escaped its state root")
        branch = self._git("-C", str(path), "rev-parse", "--abbrev-ref", "HEAD")
        if branch != receipt.branch:
            raise WorkspaceError("persisted workspace branch no longer matches its receipt")
        return WorkspaceHandle(run_id, task_id, box_id, path, branch, receipt, integration)

    def prepare_task(
        self,
        run_id: str,
        task_id: str,
        box_id: str,
        *,
        base_revision: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> WorkspaceHandle:
        return self._prepare(run_id, task_id, box_id, base_revision=base_revision, created_at=created_at, integration=False)

    def prepare_integration(
        self, run_id: str, *, base_revision: Optional[str] = None, created_at: Optional[str] = None
    ) -> WorkspaceHandle:
        return self._prepare(
            run_id, "integration", "integration", base_revision=base_revision, created_at=created_at, integration=True
        )

    def prepare_verifier(
        self, run_id: str, task_id: str, candidate_id: str, *, base_revision: str
    ) -> WorkspaceHandle:
        suffix = _component(candidate_id)[-24:]
        return self.prepare_task(run_id, "verify-" + _component(task_id), "candidate-" + suffix, base_revision=base_revision)

    def prepare_integration_generation(
        self, run_id: str, task_id: str, candidate_id: str, *, base_revision: str
    ) -> WorkspaceHandle:
        suffix = _component(candidate_id)[-24:]
        generation_task = "integrate-" + _component(task_id)
        generation_box = "candidate-" + suffix
        workspace_id = "ws-{}-{}-{}".format(
            _component(run_id), _component(generation_task), _component(generation_box)
        )
        record_path = self._record_path(workspace_id)
        if record_path.exists():
            return self._load_existing(
                record_path,
                run_id=run_id,
                task_id=generation_task,
                box_id=generation_box,
                integration=True,
            )
        handle = self._prepare(
            run_id, generation_task, generation_box,
            base_revision=base_revision, created_at=None, integration=False,
        )
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        payload["integration"] = True
        self._atomic_json(record_path, payload)
        return WorkspaceHandle(
            handle.run_id, handle.task_id, handle.box_id, handle.path,
            handle.branch, handle.receipt, True,
        )

    def _prepare(
        self,
        run_id: str,
        task_id: str,
        box_id: str,
        *,
        base_revision: Optional[str],
        created_at: Optional[str],
        integration: bool,
    ) -> WorkspaceHandle:
        self._assert_clean_source()
        safe_run, safe_task, safe_box = map(_component, (run_id, task_id, box_id))
        workspace_id = "ws-{}-{}-{}".format(safe_run, safe_task, safe_box)
        record_path = self._record_path(workspace_id)
        if record_path.exists():
            return self._load_existing(
                record_path, run_id=run_id, task_id=task_id, box_id=box_id, integration=integration
            )

        revision = base_revision or self._git("-C", str(self.source), "rev-parse", "HEAD")
        resolved_revision = self._git("-C", str(self.source), "rev-parse", "{}^{{commit}}".format(revision))
        branch = "camol/{}/{}/{}".format(safe_run, "integration" if integration else safe_task, safe_box)
        path = self.state_dir / "worktrees" / safe_run / (
            "integration" if integration else "{}--{}".format(safe_task, safe_box)
        )
        if path.exists() or path.is_symlink():
            raise WorkspaceError("workspace path exists without its matching persisted record")
        if self._branch_exists(branch):
            raise WorkspaceError("workspace branch already exists without its matching persisted record")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(path.parent, self.state_dir) or path.parent.is_symlink():
            raise WorkspaceError("workspace parent escaped the state directory")

        created = False
        try:
            self._git(
                "-C", str(self.source), "worktree", "add", "--no-track", "-b", branch, str(path), resolved_revision,
                timeout=300,
            )
            created = True
            if path.is_symlink() or not _inside(path, self.state_dir / "worktrees"):
                raise WorkspaceError("created workspace escaped the state directory")
            receipt = WorkspaceReceipt(
                workspace_id=workspace_id,
                repository_id=self._repository_id(),
                base_revision=resolved_revision,
                branch=branch,
                path=str(_real(path)),
                dirty_digest=canonical_digest([]),
                filesystem_policy="isolated_worktree_write",
                cleanup_owner="camol",
                created_at=_timestamp(created_at),
            )
            self._atomic_json(
                record_path,
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "box_id": box_id,
                    "integration": integration,
                    "receipt": receipt.to_dict(),
                },
            )
            return WorkspaceHandle(run_id, task_id, box_id, _real(path), branch, receipt, integration)
        except Exception:
            if created:
                self._git("-C", str(self.source), "worktree", "remove", "--force", str(path), check=False)
                self._git("-C", str(self.source), "branch", "-D", branch, check=False)
            raise

    def refresh_receipt(self, handle: WorkspaceHandle) -> WorkspaceReceipt:
        """Describe current workspace state without changing the original persisted receipt."""
        self._validate_handle(handle)
        status = self._git_result(
            "-C", str(handle.path), "status", "--porcelain", "--untracked-files=all"
        ).stdout.decode("utf-8", "surrogateescape")
        dirty = sorted(
            line for line in status.splitlines() if line.strip()
        )
        return WorkspaceReceipt(
            workspace_id=handle.receipt.workspace_id,
            repository_id=handle.receipt.repository_id,
            base_revision=handle.receipt.base_revision,
            branch=handle.receipt.branch,
            path=handle.receipt.path,
            dirty_digest=canonical_digest(dirty),
            filesystem_policy=handle.receipt.filesystem_policy,
            cleanup_owner=handle.receipt.cleanup_owner,
            created_at=handle.receipt.created_at,
        )

    def diff_snapshot(self, handle: WorkspaceHandle) -> Tuple[bytes, Tuple[Path, ...], Tuple[str, ...]]:
        """Return the tracked binary patch, changed regular files, and status lines."""
        self._validate_handle(handle)
        patch = self._git_result(
            "-C", str(handle.path), "diff", "--binary", handle.receipt.base_revision
        ).stdout
        status = self._git_result(
            "-C", str(handle.path), "status", "--porcelain", "--untracked-files=all"
        ).stdout.decode("utf-8", "surrogateescape")
        status_lines = tuple(sorted(line for line in status.splitlines() if line.strip()))
        names = (
            self._git_result(
                "-C", str(handle.path), "diff", "--name-only", "-z", handle.receipt.base_revision
            ).stdout
            + self._git_result(
                "-C", str(handle.path), "ls-files", "-z", "--others", "--exclude-standard"
            ).stdout
        ).split(b"\0")
        files = []
        for raw in names:
            if not raw:
                continue
            relative = Path(raw.decode("utf-8", "surrogateescape"))
            candidate = handle.path / relative
            if candidate.is_symlink() or not candidate.is_file() or not _inside(candidate, handle.path):
                continue
            files.append(_real(candidate))
        return patch, tuple(sorted(set(files))), status_lines

    def _validate_handle(self, handle: WorkspaceHandle) -> None:
        path = _real(handle.path)
        if handle.path.is_symlink() or not path.is_dir() or not _inside(path, self.state_dir / "worktrees"):
            raise WorkspaceError("workspace is missing or outside the managed worktree root")
        branch = self._git("-C", str(path), "rev-parse", "--abbrev-ref", "HEAD")
        if branch != handle.branch:
            raise WorkspaceError("workspace branch changed")

    def _write_blob(self, content: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        path = self.state_dir / "salvage" / "blobs" / digest[7:9] / digest[7:]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest[7:]:
                raise WorkspaceError("existing salvage blob is corrupt")
            return digest
        descriptor, temporary = tempfile.mkstemp(prefix="blob.", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return digest

    def salvage(self, handle: WorkspaceHandle, *, created_at: Optional[str] = None) -> SalvageReceipt:
        self._validate_handle(handle)
        patch = self._git_result(
            "-C", str(handle.path), "diff", "--binary", handle.receipt.base_revision
        ).stdout
        patch_digest = self._write_blob(patch)
        raw_untracked = self._git_result(
            "-C", str(handle.path), "ls-files", "--others", "--exclude-standard", "-z"
        ).stdout.decode("utf-8", "surrogateescape")
        artifacts = []
        for relative in sorted(item for item in raw_untracked.split("\0") if item):
            candidate = handle.path / relative
            resolved = _real(candidate)
            if candidate.is_symlink() or not candidate.is_file() or not _inside(resolved, handle.path):
                raise WorkspaceError("cannot salvage unsafe untracked path {!r}".format(relative))
            content = candidate.read_bytes()
            artifacts.append({"path": relative, "digest": self._write_blob(content), "bytes": len(content)})
        head = self._git("-C", str(handle.path), "rev-parse", "HEAD")
        body = {
            "workspace_id": handle.receipt.workspace_id,
            "workspace_digest": handle.receipt.digest(),
            "base_revision": handle.receipt.base_revision,
            "head_revision": head,
            "patch_digest": patch_digest,
            "patch_bytes": len(patch),
            "untracked": artifacts,
            "created_at": _timestamp(created_at),
        }
        salvage_id = "salvage-" + canonical_digest(body)[7:31]
        receipt = SalvageReceipt(
            salvage_id=salvage_id,
            workspace_id=body["workspace_id"],
            workspace_digest=body["workspace_digest"],
            base_revision=body["base_revision"],
            head_revision=body["head_revision"],
            patch_digest=body["patch_digest"],
            patch_bytes=body["patch_bytes"],
            untracked=tuple(artifacts),
            created_at=body["created_at"],
        )
        record = self.state_dir / "salvage" / "receipts" / (handle.receipt.workspace_id + ".json")
        self._atomic_json(record, receipt.to_dict())
        return receipt

    def _read_salvage_blob(self, digest: str) -> bytes:
        validated = require_digest(digest, "salvage blob digest")
        path = self.state_dir / "salvage" / "blobs" / validated[7:9] / validated[7:]
        if path.is_symlink() or not path.is_file():
            raise WorkspaceError("candidate salvage blob is missing")
        content = path.read_bytes()
        if "sha256:" + hashlib.sha256(content).hexdigest() != validated:
            raise WorkspaceError("candidate salvage blob is corrupt")
        return content

    def materialize_candidate(self, salvage: SalvageReceipt, destination: WorkspaceHandle) -> None:
        """Replay a captured candidate into a clean verifier/integration generation."""
        self._validate_handle(destination)
        if self._git("-C", str(destination.path), "status", "--porcelain", "--untracked-files=all"):
            raise WorkspaceError("candidate destination is not clean")
        patch = self._read_salvage_blob(salvage.patch_digest)
        if len(patch) != salvage.patch_bytes:
            raise WorkspaceError("candidate patch length does not match its receipt")
        if patch:
            self._git_result(
                "-C", str(destination.path), "apply", "--3way", "--index", "-",
                input_bytes=patch, timeout=300,
            )
        for item in salvage.untracked:
            relative = Path(item["path"])
            target = destination.path / relative
            if target.is_symlink() or not _inside(target, destination.path):
                raise WorkspaceError("candidate untracked path escaped its destination")
            content = self._read_salvage_blob(item["digest"])
            if len(content) != item["bytes"]:
                raise WorkspaceError("candidate untracked length does not match its receipt")
            if target.exists():
                if not target.is_file() or target.read_bytes() != content:
                    raise WorkspaceError("candidate untracked path conflicts with integration state")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def commit_workspace(self, handle: WorkspaceHandle, message: str) -> str:
        """Commit one Camol-owned integration generation and return its exact revision."""
        self._validate_handle(handle)
        if not isinstance(message, str) or not message.strip():
            raise WorkspaceError("integration commit message is required")
        name = self._git("-C", str(handle.path), "config", "--local", "--get", "user.name", check=False)
        email = self._git("-C", str(handle.path), "config", "--local", "--get", "user.email", check=False)
        if not name or not email:
            inherited = self._git(
                "-C", str(handle.path), "show", "-s", "--format=%an%x00%ae", handle.receipt.base_revision
            ).split("\x00")
            if len(inherited) != 2:
                raise WorkspaceError("base commit has no usable integration identity")
            name = name or inherited[0]
            email = email or inherited[1]
        if any(not value or len(value) > 320 or any(character in value for character in "\x00\r\n") for value in (name, email)):
            raise WorkspaceError("integration identity is empty or contains control characters")
        self._git("-C", str(handle.path), "add", "-A")
        self._git(
            "-C", str(handle.path),
            "-c", "user.name=" + name,
            "-c", "user.email=" + email,
            "commit", "--allow-empty", "-m", message,
            timeout=300,
        )
        return self._git("-C", str(handle.path), "rev-parse", "HEAD")

    def head_revision(self, handle: Optional[WorkspaceHandle] = None) -> str:
        target = handle.path if handle is not None else self.source
        return self._git("-C", str(target), "rev-parse", "HEAD")

    def cleanup(self, handle: WorkspaceHandle, salvage: SalvageReceipt) -> None:
        """Remove a Camol-created worktree only after validating its salvage receipt."""
        if handle.receipt.cleanup_owner != "camol":
            raise WorkspaceError("adopted workspaces are never destroyed automatically")
        self._validate_handle(handle)
        record = self.state_dir / "salvage" / "receipts" / (handle.receipt.workspace_id + ".json")
        if not record.is_file():
            raise WorkspaceError("cleanup requires a persisted salvage receipt")
        persisted = json.loads(record.read_text(encoding="utf-8"))
        if persisted != salvage.to_dict() or salvage.workspace_digest != handle.receipt.digest():
            raise WorkspaceError("salvage receipt does not bind this workspace")
        # Recompute immediately before removal.  A receipt captured before a
        # later edit is not permission to discard that edit.
        current = self.salvage(handle, created_at=salvage.created_at)
        if current.to_dict() != salvage.to_dict():
            raise WorkspaceError("workspace changed after salvage; capture a new salvage receipt")
        self._git("-C", str(self.source), "worktree", "remove", "--force", str(handle.path))
        self._git("-C", str(self.source), "branch", "-D", handle.branch)
