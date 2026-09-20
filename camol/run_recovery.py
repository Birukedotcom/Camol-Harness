"""Owner-reviewed completed-run evidence/code recovery, not operational adoption."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time

from .archive_io import ArchiveRoot
from .artifacts import ArtifactStore, RunArchive
from .probes import Redactor
from .recovery import (RecoveryError, _Git, _check_bundle, _crypto, _hash, _header,
                       _materialize, _oid, _public, _read_capsule, _separate,
                       export_workspace_recovery, MAX_BUNDLE, MAX_CAPSULE)
from .revisions import _quiescent, verify_revision_lineage
from .schema import canonical_digest
from .state import project
from .workspace import SalvageReceipt


MANIFEST_MAGIC = b"CAMOL-RUN-RECOVERY\x00\x01"
COMMIT_MAGIC = b"CAMOL-COMMIT-RECOVERY\x00\x01"
MAX_PARTS = 1024
MAX_TOTAL_BYTES = 128 << 20
MAX_RESTORED_BYTES = 256 << 20
MAX_RESTORED_FILES = 50000
MAX_MANIFEST = 4 << 20
MAX_GIT_SECONDS = 900
MAX_EXPORT_SECONDS = 1800
EXCLUDED = ["live_process_authority", "credential_stores", "shared_accounting_journals",
            "unrecorded_or_ignored_files", "remote_effects", "automatic_resume", "cleanup_permission"]
EMPTY_DIGEST = _hash(b"")


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise RecoveryError("run recovery record has missing or unknown fields")


def _freeze(events, lineage):
    if not isinstance(events, list) or not isinstance(lineage, dict) or len(lineage) > RunArchive.MAX_LINEAGE:
        raise RecoveryError("run recovery streams are invalid or exceed their count ceiling")
    if len(events) + sum(len(values) for values in lineage.values()) > RunArchive.MAX_EVENTS:
        raise RecoveryError("run recovery event inventory exceeds its ceiling")
    # Copy through strict bounded encoding before deriving or reviewing anything.
    run_id = events[0].get("run_id") if events and isinstance(events[0], dict) else None
    budget = [0]
    encoded, selected = RunArchive._encode_stream(events, run_id, budget=budget)
    sources, total = {}, len(encoded)
    for identifier, values in lineage.items():
        RunArchive._lineage_path(identifier)
        raw, sources[identifier] = RunArchive._encode_stream(values, identifier,
            maximum=RunArchive.MAX_EVENT_TOTAL - total, budget=budget)
        total += len(raw)
    verify_revision_lineage(selected, sources)
    return selected, sources


def _compile(events, lineage):
    current = project(events)
    if current["status"] != "completed" or not current.get("integration_head"):
        raise RecoveryError("run recovery requires a completed run with recorded accepted code")
    _quiescent(current)
    states = {current["run_id"]: current}
    streams = {current["run_id"]: events, **lineage}
    states.update({identifier: project(values) for identifier, values in lineage.items()})
    captures, revisions = {}, set()
    inventory = {}
    for identifier, state in sorted(states.items()):
        if state["approved_by"] != current["approved_by"]:
            raise RecoveryError("all source lineages require the same recorded recovery owner")
        inventory[identifier] = dict(plan_digest=state["plan_digest"], status=state["status"],
            event_count=len(streams[identifier]), events_digest=canonical_digest(streams[identifier]),
            integration_head=state.get("integration_head"))
        receipts = [candidate["salvage"] for candidate in state.get("candidates", {}).values()]
        receipts.extend(item["salvage"] for item in state.get("salvages", []))
        for payload in receipts:
            receipt = SalvageReceipt.from_dict(payload)
            captures[receipt.digest()] = receipt.to_dict()
        revisions.update(_oid(receipt["revision"]) for receipt in state.get("integrations", []))
    if len(captures) + len(revisions) > MAX_PARTS:
        raise RecoveryError("run recovery exceeds its code-part ceiling")
    core = dict(schema="camol.run_recovery_plan", schema_version=1, run_id=current["run_id"],
                plan_digest=current["plan_digest"], owner=current["approved_by"], streams=inventory,
                salvages=dict(sorted(captures.items())), accepted_revisions=sorted(revisions),
                excluded=list(EXCLUDED), content_verified=False,
                limits=dict(code_parts=MAX_PARTS, stored_code_bytes=MAX_TOTAL_BYTES,
                            restored_bytes=MAX_RESTORED_BYTES, restored_files=MAX_RESTORED_FILES,
                            git_seconds=MAX_GIT_SECONDS, export_seconds=MAX_EXPORT_SECONDS),
                scope="recorded_completed_run_evidence_and_code")
    if Redactor().value(core) != core:
        raise RecoveryError("run recovery review contains material requiring redaction")
    result = dict(core, review_digest=canonical_digest(core))
    if len(json.dumps(result, sort_keys=True).encode("utf-8")) > MAX_MANIFEST // 2:
        raise RecoveryError("run recovery review exceeds its metadata ceiling")
    return result


@_public
def plan_run_recovery(*, events, lineage=None):
    """Derive an exact review from replayed events; do not read Git/CAS or spend."""
    selected, sources = _freeze(events, dict(lineage or {}))
    return _compile(selected, sources)


@dataclass(frozen=True)
class _CommitSnapshot:
    """Recovery-only content description, never a fabricated runtime salvage event."""
    revision: str
    patch_digest: str = EMPTY_DIGEST
    untracked: tuple = ()

    @property
    def base_revision(self):
        return self.revision

    @property
    def head_revision(self):
        return self.revision


def _seal(cipher, content, magic):
    nonce = os.urandom(12)
    return magic + nonce + cipher.encrypt(nonce, content, magic)


def _open(cipher, raw, magic):
    if not raw.startswith(magic) or len(raw) < len(magic) + 28:
        raise RecoveryError("invalid sealed run recovery record")
    try:
        return cipher.decrypt(raw[len(magic):len(magic) + 12], raw[len(magic) + 12:], magic)
    except Exception as error:
        raise RecoveryError("run recovery authentication failed") from error


def _ledger_members(manifest):
    result = ["manifest.json", "events.jsonl"]
    result.extend(RunArchive._lineage_path(identifier) for identifier in manifest.get("lineage", {}))
    result.extend("blobs/sha256/" + digest[7:9] + "/" + digest[9:] for digest in manifest["artifact_digests"])
    return result


def _pin_ledger(root, manifest):
    """Bind every ordinary member to this archive descriptor before publication."""
    expected = {"events.jsonl": manifest["events_sha256"]}
    expected.update({RunArchive._lineage_path(identifier): record["events_sha256"]
                     for identifier, record in manifest.get("lineage", {}).items()})
    expected.update({"blobs/sha256/" + digest[7:9] + "/" + digest[9:]: digest
                     for digest in manifest["artifact_digests"]})
    raw = root.read("ledger/manifest.json", RunArchive.MAX_MANIFEST_BYTES)
    if RunArchive._json(raw, RunArchive.MAX_MANIFEST_BYTES) != manifest:
        raise RecoveryError("run recovery ordinary manifest changed before publication")
    for relative, digest in expected.items():
        maximum = RunArchive.MAX_BLOB_BYTES if relative.startswith("blobs/") else RunArchive.MAX_EVENT_TOTAL
        if _hash(root.read("ledger/" + relative, maximum)) != digest:
            raise RecoveryError("run recovery ordinary evidence changed before publication")


def _part_paths(plan):
    result = {"captures/" + digest[7:] + "/recovery.camol": ("capture", digest) for digest in plan["salvages"]}
    result.update({"commits/" + oid + ".camol": ("commit", oid) for oid in plan["accepted_revisions"]})
    return result


def _budget():
    return {"bytes": MAX_RESTORED_BYTES, "files": MAX_RESTORED_FILES}


def _restore_parts(root, plan, part_records, cipher, key, destination):
    paths = _part_paths(plan)
    if set(part_records) != set(paths):
        raise RecoveryError("run recovery code inventory differs from the reviewed ledger")
    results, total, budget = {}, 0, _budget()
    with tempfile.TemporaryDirectory(prefix="camol-run-recovery-git-") as temporary:
        git = _Git(temporary)
        git.deadline = time.monotonic() + MAX_GIT_SECONDS
        # One aggregate Git deadline across all code parts.
        for relative, (kind, identity) in paths.items():
            record = part_records[relative]
            _exact(record, {"digest", "bytes", "tree"})
            if type(record["bytes"]) is not int or not 1 <= record["bytes"] <= MAX_CAPSULE:
                raise RecoveryError("invalid run recovery part byte count")
            raw = root.read(relative, min(MAX_CAPSULE, MAX_TOTAL_BYTES - total))
            total += len(raw)
            if len(raw) != record["bytes"] or _hash(raw) != record["digest"]:
                raise RecoveryError("run recovery code part is missing or changed")
            if kind == "capture":
                captured_raw, receipt, bundle, blobs = _read_capsule(root.path / Path(relative).parent, key)
                if captured_raw != raw or receipt.to_dict() != plan["salvages"][identity]:
                    raise RecoveryError("run recovery capture does not match its ledger receipt")
                target = Path(destination) / "captures" / identity[7:]
            else:
                receipt = _CommitSnapshot(identity)
                bundle = _open(cipher, raw, COMMIT_MAGIC + identity.encode("ascii"))
                _check_bundle(bundle, receipt)
                blobs = {EMPTY_DIGEST: b""}
                target = Path(destination) / "accepted" / identity
            tree = _materialize(git, target, receipt, bundle, blobs, resource_budget=budget)
            if tree != record["tree"]:
                raise RecoveryError("reconstructed code differs from its sealed tree identity")
            results[relative] = tree
    return results


def _result(plan, manifest_digest, trees):
    return dict(schema="camol.run_recovery_result", schema_version=1,
                run_id=plan["run_id"], plan_digest=plan["plan_digest"], review_digest=plan["review_digest"],
                manifest_digest=manifest_digest, stream_count=len(plan["streams"]),
                restored_captures=len(plan["salvages"]), restored_commits=len(plan["accepted_revisions"]),
                accepted_head=plan["streams"][plan["run_id"]]["integration_head"],
                reconstructed_trees=trees, excluded=list(EXCLUDED),
                operational_restore=False, resume_authorized=False, cleanup_authorized=False)


@_public
def export_run_recovery(*, source, state_dir, events, lineage=None, by, review_digest,
                        allow_encrypted_raw, key, output, recheck=None):
    """Export recorded evidence/code after exact review. Caller must own the run lock."""
    selected, sources = _freeze(events, dict(lineage or {}))
    plan = _compile(selected, sources)
    if by != plan["owner"] or review_digest != plan["review_digest"] or allow_encrypted_raw is not True:
        raise RecoveryError("run recovery requires the recorded owner, exact review digest and encrypted-raw opt-in")
    cipher = _crypto(key)
    _separate(output, (source, state_dir))
    deadline = time.monotonic() + MAX_EXPORT_SECONDS
    with tempfile.TemporaryDirectory(prefix="camol-run-export-") as temporary:
        git = _Git(temporary)
        git.deadline = time.monotonic() + MAX_GIT_SECONDS
        common = Path(os.fsdecode(git.call(source, "rev-parse", "--git-common-dir")).strip())
        _separate(output, (common if common.is_absolute() else Path(source) / common,))
        artifact_root = Path(state_dir) / "artifacts"
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            raise RecoveryError("run recovery requires its existing ordinary artifact store")
        with ArchiveRoot(output, create=True) as root:
            # Ordinary exports retain their redaction and complete-lineage checks.
            ledger = RunArchive.export(plan["run_id"], selected, ArtifactStore(state_dir), root.path / "ledger", lineage_events=sources)
            ledger_digest = ledger["manifest_digest"]
            parts, total = {}, 0
            for relative, (kind, identity) in _part_paths(plan).items():
                if time.monotonic() > deadline:
                    raise RecoveryError("run recovery export exceeded its aggregate deadline")
                if kind == "capture":
                    receipt = SalvageReceipt.from_dict(plan["salvages"][identity])
                    policy = dict(schema="camol.recovery_policy", schema_version=1, owner=by,
                                  purpose="workspace-recovery", salvage_digest=identity, allow_encrypted_raw=True)
                    result = export_workspace_recovery(source=source, state_dir=state_dir, salvage=receipt,
                        policy=policy, key=key, output=root.path / Path(relative).parent)
                    tree = result["candidate_tree"]
                else:
                    receipt = _CommitSnapshot(identity)
                    packed = git.call(source, "pack-objects", "--stdout", "--revs", "--no-reuse-delta",
                        data=(identity + "\n").encode("ascii"), maximum=MAX_BUNDLE)
                    bundle = _header(receipt) + packed
                    tree = _materialize(git, Path(temporary) / identity, receipt, bundle, {EMPTY_DIGEST: b""})
                    root.write(relative, _seal(cipher, bundle, COMMIT_MAGIC + identity.encode("ascii")))
                raw = root.read(relative, min(MAX_CAPSULE, MAX_TOTAL_BYTES - total))
                total += len(raw)
                parts[relative] = dict(digest=_hash(raw), bytes=len(raw), tree=tree)
            with tempfile.TemporaryDirectory(prefix="camol-run-verify-") as check:
                trees = _restore_parts(root, plan, parts, cipher, key, check)
            verified_ledger, verified_events, verified_sources = RunArchive.verify_snapshot(root.path / "ledger")
            if verified_ledger != ledger or verified_events != selected or verified_sources != sources:
                raise RecoveryError("run recovery ordinary ledger changed during export")
            _pin_ledger(root, ledger)
            # Re-read the owner-held ledger immediately before publication.
            if recheck is not None and recheck() != plan:
                raise RecoveryError("run advanced after recovery review; capsule publication refused")
            if time.monotonic() > deadline:
                raise RecoveryError("run recovery export exceeded its aggregate deadline")
            manifest = dict(schema="camol.run_recovery", schema_version=1, plan=plan,
                            ledger_manifest_digest=ledger_digest, parts=parts)
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) + len(MANIFEST_MAGIC) + 28 > MAX_MANIFEST:
                raise RecoveryError("run recovery manifest exceeds its byte ceiling")
            sealed = _seal(cipher, encoded, MANIFEST_MAGIC)
            root.verify()
            root.write("manifest.camol", sealed)
            return _result(plan, _hash(sealed), trees)


@_public
def restore_run_recovery(*, archive, key, output):
    """Verify ledger-derived coverage and reconstruct evidence/code, without adoption."""
    _separate(output, (archive,))
    cipher = _crypto(key)
    with ArchiveRoot(archive) as root:
        raw = root.read("manifest.camol", MAX_MANIFEST)
        manifest = RunArchive._json(_open(cipher, raw, MANIFEST_MAGIC), MAX_MANIFEST)
        _exact(manifest, {"schema", "schema_version", "plan", "ledger_manifest_digest", "parts"})
        if manifest["schema"] != "camol.run_recovery" or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise RecoveryError("unsupported run recovery manifest")
        ledger, events, lineage = RunArchive.verify_snapshot(root.path / "ledger")
        plan = plan_run_recovery(events=events, lineage=lineage)
        if plan != manifest["plan"] or ledger["manifest_digest"] != manifest["ledger_manifest_digest"]:
            raise RecoveryError("run recovery ledger or reviewed coverage changed")
        if not isinstance(manifest["parts"], dict):
            raise RecoveryError("invalid run recovery part inventory")
        with ArchiveRoot(output, create=True) as target:
            trees = _restore_parts(root, plan, manifest["parts"], cipher, key, target.path)
            # Preserve the exact verified ordinary ledger, never a new approved run.
            with ArchiveRoot(root.path / "ledger") as original, ArchiveRoot(target.path / "ledger", create=True) as copied:
                for relative in _ledger_members(ledger):
                    copied.write(relative, original.read(relative, RunArchive.MAX_EVENT_TOTAL))
            copied_manifest, copied_events, copied_lineage = RunArchive.verify_snapshot(target.path / "ledger")
            if copied_manifest != ledger or copied_events != events or copied_lineage != lineage:
                raise RecoveryError("run recovery ledger changed while copying")
            result = _result(plan, _hash(raw), trees)
            target.write("recovery-result.json", json.dumps(result, sort_keys=True).encode("utf-8"))
            return result


@_public
def verify_run_recovery(*, archive, key):
    with tempfile.TemporaryDirectory(prefix="camol-run-recovery-check-") as temporary:
        return restore_run_recovery(archive=archive, key=key, output=Path(temporary) / "recovered")
