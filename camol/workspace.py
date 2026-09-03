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
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .probes import GIT_SAFETY_ARGS, Redactor, sanitized_environment
from .readiness import WorkspaceReceipt
from .schema import canonical_digest


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
    return value or datetime.now(timezone.utc).isoformat(timespec="microseconds")


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

    def _git_result(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        git = shutil.which("git")
        if not git:
            raise WorkspaceError("git is not installed")
        try:
            result = subprocess.run(
                [git] + list(GIT_SAFETY_ARGS) + list(args),
                stdin=subprocess.DEVNULL,
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
        dirty = sorted(
            line for line in self._git(
                "-C", str(handle.path), "status", "--porcelain", "--untracked-files=all"
            ).splitlines() if line.strip()
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
        patch = self._git_result("-C", str(handle.path), "diff", "--binary", "HEAD").stdout
        status_lines = tuple(
            sorted(
                line
                for line in self._git(
                    "-C", str(handle.path), "status", "--porcelain", "--untracked-files=all"
                ).splitlines()
                if line.strip()
            )
        )
        names = self._git_result(
            "-C", str(handle.path), "ls-files", "-z", "--modified", "--others", "--exclude-standard"
        ).stdout.split(b"\0")
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
        patch = self._git_result("-C", str(handle.path), "diff", "--binary", "HEAD").stdout
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
