"""Explicit encrypted recovery of a captured workspace; never resume authority."""

import base64
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata

from .archive_io import ArchiveRoot
from .git_safety import GIT_SAFETY_ARGS
from .json_contracts import decode_contract
from .probes import sanitized_environment
from .schema import require_identifier
from .workspace import SalvageReceipt, WorkspaceError


class RecoveryError(ValueError):
    pass


MAGIC = b"CAMOL-RECOVERY\x00\x01"
MAX_CAPSULE = 64 << 20
MAX_BUNDLE = 16 << 20
MAX_FILE = 8 << 20
MAX_CONTENT = 32 << 20
MAX_FILES = 10000
MAX_OBJECTS = 100000


def _public(function):
    @functools.wraps(function)
    def guarded(**kwargs):
        try:
            return function(**kwargs)
        except RecoveryError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError, WorkspaceError) as error:
            raise RecoveryError("recovery refused invalid, unsafe or changed input; partial output may remain") from error
    return guarded


def _hash(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _crypto(key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise RecoveryError("recovery requires an explicit 32-byte encryption key")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise RecoveryError("install camol-harness[recovery] for encrypted recovery") from error
    return AESGCM(key)


def _policy(value, salvage):
    fields = {"schema", "schema_version", "owner", "purpose", "salvage_digest", "allow_encrypted_raw"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.recovery_policy" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RecoveryError("invalid recovery policy")
    require_identifier(value["owner"], "recovery owner")
    if value["purpose"] != "workspace-recovery" or value["allow_encrypted_raw"] is not True or value["salvage_digest"] != salvage.digest():
        raise RecoveryError("explicit encrypted-raw policy must bind this exact salvage receipt")
    return dict(value)


def _oid(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise RecoveryError("recovery revisions must be exact Git object IDs")
    return value


def _path(value):
    ArchiveRoot._parts(value)
    for part in value.split("/"):
        normalized = unicodedata.normalize("NFKC", part).rstrip(" .").casefold()
        if normalized == ".git" or re.fullmatch(r"\.?git~[0-9]+", normalized) or ":" in part or any(unicodedata.category(char) in {"Cf", "Cc"} for char in part):
            raise RecoveryError("recovery content cannot use Git control aliases or ambiguous path characters")
    return value


def _separate(output, inputs):
    target = Path(output).resolve()
    for value in inputs:
        source = Path(value).resolve()
        if target == source or target in source.parents or source in target.parents:
            raise RecoveryError("recovery output must be separate from its source stores")


class _Git:
    """Trusted local plumbing with time/output limits and no network protocols."""

    def __init__(self, scratch):
        self.scratch = Path(scratch)
        self.deadline = time.monotonic() + 120
        self.binary = shutil.which("git", path="/usr/bin:/bin")
        if not self.binary:
            raise RecoveryError("a trusted system Git is required for recovery")
        self.env = sanitized_environment({"PATH": "/usr/bin:/bin", "HOME": str(self.scratch)})
        self.env.update(GIT_NO_LAZY_FETCH="1", GIT_LFS_SKIP_SMUDGE="1")

    def call(self, repo, *args, data=b"", maximum=1 << 20):
        remaining = min(30, self.deadline - time.monotonic())
        if remaining <= 0:
            raise RecoveryError("recovery exceeded its operation deadline")
        argv = [self.binary, *GIT_SAFETY_ARGS]
        for protocol in ("file", "http", "https", "ssh", "git", "ext"):
            argv.extend(["-c", "protocol." + protocol + ".allow=never"])
        argv.extend(["-C", str(repo), *args])
        with tempfile.TemporaryFile(dir=str(self.scratch)) as stdin, tempfile.TemporaryFile(dir=str(self.scratch)) as stdout, tempfile.TemporaryFile(dir=str(self.scratch)) as stderr:
            stdin.write(data)
            stdin.seek(0)
            process = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr,
                                       env=self.env, start_new_session=True)
            deadline = time.monotonic() + remaining
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline or os.fstat(stdout.fileno()).st_size > maximum or os.fstat(stderr.fileno()).st_size > 1 << 20:
                        raise RecoveryError("recovery Git exceeded its time or output ceiling")
                    time.sleep(0.01)
                if process.returncode or os.fstat(stdout.fileno()).st_size > maximum or os.fstat(stderr.fileno()).st_size > 1 << 20:
                    raise RecoveryError("recovery Git refused the captured objects or patch")
                stdout.seek(0)
                return stdout.read(maximum + 1)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def _header(salvage):
    base, head = _oid(salvage.base_revision), _oid(salvage.head_revision)
    if len(base) != len(head):
        raise RecoveryError("recovery cannot mix Git object formats")
    algorithm = "sha1" if len(base) == 40 else "sha256"
    return ("# v3 git bundle\n@object-format=" + algorithm + "\n" + base +
            " refs/recovery/base\n" + head + " refs/recovery/head\n\n").encode("ascii")


def _check_bundle(bundle, salvage):
    header = _header(salvage)
    if len(bundle) > MAX_BUNDLE or not bundle.startswith(header):
        raise RecoveryError("recovery bundle has unexpected references or capabilities")
    pack = bundle[len(header):]
    if len(pack) < 12 or pack[:4] != b"PACK" or int.from_bytes(pack[4:8], "big") not in {2, 3} or not 1 <= int.from_bytes(pack[8:12], "big") <= MAX_OBJECTS:
        raise RecoveryError("recovery Git object inventory exceeds its supported bound")


def _references(salvage):
    if len(salvage.untracked) > MAX_FILES:
        raise RecoveryError("recovery file inventory exceeds its ceiling")
    references = {salvage.patch_digest: salvage.patch_bytes}
    for item in salvage.untracked:
        _path(item["path"])
        if item["digest"] in references and references[item["digest"]] != item["bytes"]:
            raise RecoveryError("recovery contains conflicting blob sizes")
        references[item["digest"]] = item["bytes"]
    if any(size > MAX_FILE for size in references.values()) or sum(references.values()) > MAX_CONTENT:
        raise RecoveryError("recovery content exceeds its byte ceiling")
    return references


def _decode_blob(value, maximum):
    if not isinstance(value, str) or len(value) > 4 * ((maximum + 2) // 3):
        raise RecoveryError("recovery encoded content exceeds its byte ceiling")
    result = base64.b64decode(value, validate=True)
    if len(result) > maximum or base64.b64encode(result).decode("ascii") != value:
        raise RecoveryError("recovery content encoding is invalid")
    return result


def _read_capsule(archive, key):
    cipher = _crypto(key)
    with ArchiveRoot(archive) as reader:
        raw = reader.read("recovery.camol", MAX_CAPSULE)
    if len(raw) < len(MAGIC) + 28 or not raw.startswith(MAGIC):
        raise RecoveryError("invalid encrypted recovery capsule")
    try:
        decoded = cipher.decrypt(raw[len(MAGIC):len(MAGIC) + 12], raw[len(MAGIC) + 12:], MAGIC)
    except Exception as error:
        raise RecoveryError("recovery authentication failed; key or capsule is incorrect") from error
    value = decode_contract(decoded, max_bytes=MAX_CAPSULE)
    if not isinstance(value, dict) or set(value) != {"schema", "schema_version", "policy", "salvage", "bundle", "blobs"} or value["schema"] != "camol.workspace_recovery" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RecoveryError("invalid recovery payload")
    salvage = SalvageReceipt.from_dict(value["salvage"])
    _policy(value["policy"], salvage)
    references = _references(salvage)
    if not isinstance(value["blobs"], dict) or set(value["blobs"]) != set(references):
        raise RecoveryError("recovery blob inventory does not match its receipt")
    blobs = {}
    for digest, size in references.items():
        content = _decode_blob(value["blobs"][digest], min(size, MAX_FILE))
        if len(content) != size or _hash(content) != digest:
            raise RecoveryError("recovery blob failed integrity verification")
        blobs[digest] = content
    bundle = _decode_blob(value["bundle"], MAX_BUNDLE)
    _check_bundle(bundle, salvage)
    return raw, salvage, bundle, blobs


@_public
def generate_recovery_key(*, output):
    """Generate a private key file without displaying or logging its bytes."""
    with ArchiveRoot(output, create=True) as target:
        target.write("key.bin", os.urandom(32))
    return {"key_file": str(Path(output).absolute() / "key.bin"), "key_bytes": 32}


@_public
def load_recovery_key(*, path):
    path = Path(path).absolute()
    with ArchiveRoot(path.parent) as root:
        info = os.stat(path.name, dir_fd=root.fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RecoveryError("recovery key must be an owner-private regular file")
        key = root.read(path.name, 32)
        if len(key) != 32:
            raise RecoveryError("recovery key file must contain exactly 32 bytes")
        return key


def _tree_content(git, destination, entries):
    """Batch raw object reads so a large tree does not spawn two processes/file."""
    records, objects = [], {}
    for entry in entries:
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        relative = _path(raw_path.decode("utf-8", "strict"))
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise RecoveryError("external submodule or unsupported tree content cannot be recovered")
        _oid(oid)
        records.append((mode, relative, oid))
        objects[oid] = None
    if not objects:
        return records, {}, 0
    request = ("\n".join(objects) + "\n").encode("ascii")
    metadata = git.call(destination, "cat-file", "--batch-check", data=request, maximum=2 << 20).splitlines()
    if len(metadata) != len(objects):
        raise RecoveryError("recovery Git object size inventory is incomplete")
    for oid, record in zip(objects, metadata):
        fields = record.split(b" ")
        if len(fields) != 3 or fields[:2] != [oid.encode("ascii"), b"blob"] or not fields[2].isdigit():
            raise RecoveryError("recovery Git object metadata is invalid")
        size = int(fields[2])
        if size > MAX_FILE:
            raise RecoveryError("restored workspace exceeds its byte ceiling")
        objects[oid] = size
    total = sum(objects[oid] for _, _, oid in records)
    if total > MAX_CONTENT:
        raise RecoveryError("restored workspace exceeds its byte ceiling")
    raw = git.call(destination, "cat-file", "--batch", data=request,
                   maximum=MAX_CONTENT + MAX_FILES * 128)
    content, cursor = {}, 0
    for oid, size in objects.items():
        end = raw.find(b"\n", cursor, cursor + 128)
        if end < 0 or raw[cursor:end] != (oid + " blob " + str(size)).encode("ascii"):
            raise RecoveryError("recovery Git object data header is invalid")
        cursor = end + 1
        if raw[cursor + size:cursor + size + 1] != b"\n":
            raise RecoveryError("recovery Git object data is incomplete")
        content[oid] = raw[cursor:cursor + size]
        cursor += size + 1
    if cursor != len(raw):
        raise RecoveryError("recovery Git object data has trailing content")
    return records, content, total


def _materialize(git, destination, salvage, bundle, blobs):
    """No checkout filters: reconstruct the index, then copy object bytes."""
    _check_bundle(bundle, salvage)
    algorithm = "sha1" if len(salvage.base_revision) == 40 else "sha256"
    destination = Path(destination)
    with ArchiveRoot(destination, create=True) as output:
        git.call(destination, "init", "--quiet", "--template=", "--object-format=" + algorithm)
        git.call(destination, "bundle", "verify", "-", data=bundle)
        git.call(destination, "bundle", "unbundle", "-", data=bundle)
        for name, oid in (("base", salvage.base_revision), ("head", salvage.head_revision)):
            actual = git.call(destination, "rev-parse", oid + "^{commit}").decode().strip()
            if actual != oid:
                raise RecoveryError("recovery reference is not the captured commit")
            git.call(destination, "update-ref", "refs/recovery/" + name, oid)
        git.call(destination, "update-ref", "--no-deref", "HEAD", salvage.head_revision)
        git.call(destination, "fsck", "--strict", "--no-reflogs", "--no-dangling")
        git.call(destination, "read-tree", salvage.base_revision)
        patch = blobs[salvage.patch_digest]
        if patch:
            git.call(destination, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", data=patch)
        tree = git.call(destination, "write-tree").decode().strip()
        listing = git.call(destination, "ls-tree", "-r", "-z", tree, maximum=4 << 20)
        entries = [entry for entry in listing.split(b"\0") if entry]
        if len(entries) + len(salvage.untracked) > MAX_FILES:
            raise RecoveryError("restored workspace exceeds its file ceiling")
        records, contents, used = _tree_content(git, destination, entries)
        paths = set()
        for mode, relative, oid in records:
            content = contents[oid]
            if mode == "120000":
                output.symlink(relative, os.fsdecode(content))
            else:
                output.write(relative, content, executable=mode == "100755")
            paths.add(relative)
        for item in salvage.untracked:
            relative = _path(item["path"])
            if relative in paths:
                raise RecoveryError("untracked recovery content collides with the captured tree")
            used += item["bytes"]
            if used > MAX_CONTENT:
                raise RecoveryError("restored workspace exceeds its byte ceiling")
            output.write(relative, blobs[item["digest"]])
        return tree


def _result(raw, salvage, tree):
    return dict(schema="camol.recovery_result", schema_version=1, capsule_digest=_hash(raw),
                capsule_bytes=len(raw), salvage_digest=salvage.digest(), candidate_tree=tree,
                git_history_restored=True, captured_content_restored=True,
                resume_authorized=False, cleanup_authorized=False)


@_public
def export_workspace_recovery(*, source, state_dir, salvage, policy, key, output):
    """Explicitly encrypt captured salvage plus self-contained base/head history.

    Policy is a caller-supplied opt-in, not a kernel approval or proof of owner
    identity. It must bind the exact receipt. The source and CAS are never changed.
    """
    cipher = _crypto(key)
    if not isinstance(salvage, SalvageReceipt):
        raise RecoveryError("recovery requires a typed captured salvage receipt")
    salvage = SalvageReceipt.from_dict(salvage.to_dict())
    policy = _policy(policy, salvage)
    references = _references(salvage)
    blobs = {}
    with ArchiveRoot(state_dir) as state:
        for digest, size in references.items():
            content = state.read("salvage/blobs/" + digest[7:9] + "/" + digest[7:], min(size, MAX_FILE))
            if len(content) != size or _hash(content) != digest:
                raise RecoveryError("captured salvage is missing or corrupt")
            blobs[digest] = content
    source = Path(source).resolve(strict=True)
    _separate(output, (source, state_dir))
    with tempfile.TemporaryDirectory(prefix="camol-recovery-") as temporary:
        git = _Git(temporary)
        common = Path(os.fsdecode(git.call(source, "rev-parse", "--git-common-dir")).strip())
        _separate(output, (common if common.is_absolute() else source / common,))
        if git.call(source, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise RecoveryError("shallow Git history is not a complete recovery source")
        revisions = (_oid(salvage.base_revision) + "\n" + _oid(salvage.head_revision) + "\n").encode("ascii")
        packed = git.call(source, "pack-objects", "--stdout", "--revs", "--no-reuse-delta", data=revisions, maximum=MAX_BUNDLE)
        bundle = _header(salvage) + packed
        tree = _materialize(git, Path(temporary) / "verify", salvage, bundle, blobs)
        payload = dict(schema="camol.workspace_recovery", schema_version=1, policy=policy,
                       salvage=salvage.to_dict(), bundle=base64.b64encode(bundle).decode("ascii"),
                       blobs={digest: base64.b64encode(content).decode("ascii") for digest, content in blobs.items()})
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(plaintext) + len(MAGIC) + 28 > MAX_CAPSULE:
            raise RecoveryError("encrypted recovery capsule exceeds its byte ceiling")
        nonce = os.urandom(12)
        raw = MAGIC + nonce + cipher.encrypt(nonce, plaintext, MAGIC)
        with ArchiveRoot(output, create=True) as target:
            target.write("recovery.camol", raw)
        return _result(raw, salvage, tree)


@_public
def restore_workspace_recovery(*, archive, key, output):
    """Reconstruct captured content into a new private repository, without running it."""
    _separate(output, (archive,))
    raw, salvage, bundle, blobs = _read_capsule(archive, key)
    with tempfile.TemporaryDirectory(prefix="camol-recovery-") as temporary:
        tree = _materialize(_Git(temporary), output, salvage, bundle, blobs)
    return _result(raw, salvage, tree)


@_public
def verify_workspace_recovery(*, archive, key):
    """Actually reconstruct in private scratch storage, not merely decrypt/hash."""
    with tempfile.TemporaryDirectory(prefix="camol-recovery-check-") as temporary:
        return restore_workspace_recovery(archive=archive, key=key, output=Path(temporary) / "repository")
