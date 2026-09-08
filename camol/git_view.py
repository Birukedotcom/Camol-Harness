"""Bounded private Git inspection metadata, never the shared repository store.

The view contains one shallow approved commit and its reachable trees/blobs.
It has no remote URLs, source configuration, alternate object stores, parents,
hooks, other worktrees, or unreachable objects. Workers may inspect, not commit.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path

from .git_safety import GitSafetyError, safe_git_argv
from .probes import Probe, sanitized_environment
from .schema import canonical_digest
from .sandbox import SandboxError


class GitViewError(SandboxError):
    pass


MAX_OBJECTS = 100000
MAX_BYTES = 64 << 20
ENVIRONMENT_NAMES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_OPTIONAL_LOCKS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT", "TMPDIR")


def _git(source, arguments, *, data=None, environment=None):
    env = sanitized_environment()
    if environment:
        env.update(environment)
    binary = shutil.which("git")
    if not binary:
        raise GitViewError("private Git inspection requires Git on PATH")
    try:
        argv = safe_git_argv(binary, ["-C", str(source), *arguments], env=env)
        result = subprocess.run(argv, input=data, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired, GitSafetyError) as error:
        raise GitViewError("private Git inspection plumbing unavailable") from error
    if result.returncode:
        # Never expose source config values or credentialed URLs from stderr.
        raise GitViewError("private Git inspection plumbing failed")
    return result.stdout


def _files(root):
    result = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GitViewError("private Git metadata must not contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise GitViewError("private Git metadata must contain only regular files")
        if path.name == "camol-view.json" and path.parent == root:
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_BYTES + (16 << 20) or len(result) > MAX_OBJECTS + 32:
            raise GitViewError("private Git metadata exceeds its inspection bound")
        result[str(path.relative_to(root))] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def view_path(state_dir, workspace_id):
    # Receipt IDs are not necessarily safe path components. Never trust them.
    return Path(state_dir).resolve() / "git-views" / hashlib.sha256(workspace_id.encode()).hexdigest()


def scratch_path(state_dir, workspace_id):
    return Path(state_dir).resolve() / "worker-scratch" / hashlib.sha256(workspace_id.encode()).hexdigest()


def load_view(root, *, workspace, base_revision=None):
    root = Path(root)
    manifest_path = root / "camol-view.json"
    if root.resolve() != root or manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 16 << 20:
        raise GitViewError("private Git inspection view is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, OSError) as error:
        raise GitViewError("private Git inspection manifest is unreadable") from error
    if (set(manifest) != {"schema", "schema_version", "workspace", "base_revision", "capabilities", "files"}
            or manifest["schema"] != "camol.git_inspection" or manifest["schema_version"] != 1
            or manifest["workspace"] != str(Path(workspace).resolve())
            or manifest["capabilities"] != ["status", "diff", "shallow_baseline_history"]
            or (base_revision is not None and manifest["base_revision"] != base_revision)
            or manifest["files"] != _files(root)):
        raise GitViewError("private Git inspection bytes differ from the pinned view")
    return manifest


def prepare_view(source, state_dir, handle):
    root = view_path(state_dir, handle.receipt.workspace_id)
    base = handle.receipt.base_revision
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
        raise GitViewError("private Git inspection requires an exact object revision")
    if root.exists():
        return root, load_view(root, workspace=handle.path, base_revision=base)
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.parent.resolve() != root.parent:
        raise GitViewError("private Git inspection storage must not traverse symlinks")
    stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=str(root.parent)))
    try:
        listing = _git(source, ["ls-tree", "-r", "-t", "-z", base])
        if len(listing) > 16 << 20:
            raise GitViewError("approved tree exceeds the Git inspection bound")
        root_tree = _git(source, ["rev-parse", base + "^{tree}"]).decode().strip()
        objects = {base, root_tree}
        for row in listing.split(b"\0"):
            if row:
                metadata = row.split(b"\t", 1)[0].split()
                if len(metadata) != 3:
                    raise GitViewError("Git tree metadata is malformed")
                if metadata[1] in {b"tree", b"blob"}:
                    objects.add(metadata[2].decode("ascii"))
        if len(objects) > MAX_OBJECTS:
            raise GitViewError("approved tree contains too many Git objects")
        query = ("\n".join(sorted(objects)) + "\n").encode()
        checked = _git(source, ["cat-file", "--batch-check"], data=query).splitlines()
        sizes = {}
        for line in checked:
            parts = line.split()
            if len(parts) != 3 or parts[0].decode() not in objects or parts[1] not in {b"commit", b"tree", b"blob"} or not parts[2].isdigit():
                raise GitViewError("approved Git object metadata is invalid")
            sizes[parts[0].decode()] = (parts[1], int(parts[2]))
        if set(sizes) != objects or sum(item[1] for item in sizes.values()) > MAX_BYTES:
            raise GitViewError("approved Git objects exceed the inspection byte bound")
        raw = _git(source, ["cat-file", "--batch"], data=query)
        offset = 0
        for oid in sorted(objects):
            end = raw.find(b"\n", offset)
            kind, length = sizes[oid]
            if end < 0 or raw[offset:end] != oid.encode() + b" " + kind + b" " + str(length).encode():
                raise GitViewError("Git object response does not match the requested identity")
            content = raw[end + 1:end + 1 + length]
            offset = end + 2 + length
            encoded = kind + b" " + str(length).encode() + b"\0" + content
            if len(content) != length or hashlib.new("sha1" if len(oid) == 40 else "sha256", encoded).hexdigest() != oid:
                raise GitViewError("Git object bytes do not match their approved identity")
            target = stage / "objects" / oid[:2] / oid[2:]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zlib.compress(encoded))
        if offset != len(raw):
            raise GitViewError("unexpected extra Git object bytes")
        (stage / "refs").mkdir()
        (stage / "HEAD").write_text(base + "\n")
        (stage / "shallow").write_text(base + "\n")
        config = "[core]\n\trepositoryformatversion = {}\n\tbare = false\n\thooksPath = /dev/null\n\tfsmonitor = false\n\tignoreStat = false\n".format(1 if len(base) == 64 else 0)
        if len(base) == 64:
            config += "[extensions]\n\tobjectFormat = sha256\n"
        (stage / "config").write_text(config)
        environment = dict(GIT_DIR=str(stage), GIT_WORK_TREE=str(handle.path), GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0")
        _git(handle.path, ["read-tree", base], environment=environment)
        manifest = dict(schema="camol.git_inspection", schema_version=1, workspace=str(handle.path.resolve()),
                        base_revision=base, capabilities=["status", "diff", "shallow_baseline_history"], files=_files(stage))
        (stage / "camol-view.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        os.replace(str(stage), str(root))
        return root, manifest
    finally:
        if stage.exists():
            # Exact temporary directory created by this call; never source data.
            shutil.rmtree(stage)


class GitInspectionProbe(Probe):
    probe_id = "control-plane.git-inspection"
    kind = "control_plane"
    VERSION = 1

    def __init__(self, root, manifest):
        self.root, self.manifest = Path(root), manifest

    def config(self, context):
        return {"view_path": str(self.root), "view_digest": canonical_digest(self.manifest),
                "capabilities": self.manifest["capabilities"]}

    def observe(self, context):
        load_view(self.root, workspace=context.workspace, base_revision=self.manifest["base_revision"])
        return self.green(context, "private shallow Git inspection view is pinned; shared metadata and Git mutations are not granted",
                          facts=self.config(context))


def execution_environment(state_dir, workspace, bundle):
    root = view_path(state_dir, bundle.workspace.workspace_id)
    manifest = load_view(root, workspace=workspace, base_revision=bundle.workspace.base_revision)
    expected = GitInspectionProbe(root, manifest).definition_digest(None)
    if not any(item.probe_id == GitInspectionProbe.probe_id and item.definition_digest == expected
               for item in bundle.probe_policy.required_probes):
        raise GitViewError("admission lacks the exact private Git inspection capability; owner-approved migration required")
    scratch = scratch_path(state_dir, bundle.workspace.workspace_id)
    if str(root) not in bundle.sandbox_policy.read_paths or str(scratch) not in bundle.sandbox_policy.write_paths:
        raise GitViewError("private Git inspection or scratch is outside the admitted authority")
    return dict(GIT_DIR=str(root), GIT_WORK_TREE=str(Path(workspace).resolve()), GIT_OPTIONAL_LOCKS="0",
                GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_TERMINAL_PROMPT="0", TMPDIR=str(scratch))
