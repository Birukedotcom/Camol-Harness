"""Encrypted approved-source handoff, not target readiness or execution authority."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from .archive_io import ArchiveRoot
from .debug_execution import source_identity
from .json_contracts import decode_contract
from .probes import Redactor
from .recovery import (_Git, _crypto, _decode_blob, _hash, _header, _materialize,
                       _public, _separate, RecoveryError, MAX_BUNDLE, MAX_CAPSULE)
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .source_binding import assert_source, validate_binding
from .targets import descriptor, _owner
from .workspace import SalvageReceipt


MAGIC = b"CAMOL-SOURCE-HANDOFF\x00\x01"
EMPTY_DIGEST = _hash(b"")
EVENT = "SOURCE_HANDOFF_EXPORTED"
MAX_EXPORTS = 1024


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise RecoveryError("source handoff has missing or unknown fields")


def _path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.isprintable()
            or not Path(value).is_absolute() or str(Path(value)) != value or ".." in Path(value).parts):
        raise RecoveryError("source destination must be a canonical absolute path")
    return value


def validate_proposal(value):
    version = value.get("schema_version") if isinstance(value, dict) else None
    fields = {"schema", "schema_version", "request_id", "owner", "source_binding", "target",
        "adoption_digest", "destination_workspace", "issued_at", "expires_at", "include_reachable_history",
        "allow_encrypted_source", "execution_authority", "digest"}
    if type(version) is int and version == 2:
        fields.add("selection")
    _fields(value, fields)
    if value["schema"] != "camol.source_handoff_proposal" or type(version) is not int or version not in (1, 2):
        raise RecoveryError("invalid source handoff proposal schema")
    if version == 2:
        from .task_source import validate_selection
        validate_selection(value["selection"])
    for name in ("request_id", "owner"):
        require_identifier(value[name], name)
        if len(value[name]) > 128:
            raise RecoveryError("source handoff identifier exceeds its bound")
    validate_binding(value["source_binding"])
    descriptor(value["target"])
    require_digest(value["adoption_digest"], "source destination adoption")
    _path(value["destination_workspace"])
    start, end = parse_timestamp(value["issued_at"], "source issued"), parse_timestamp(value["expires_at"], "source expiry")
    if not start < end <= start + timedelta(hours=1):
        raise RecoveryError("source handoff approval window must be positive and at most one hour")
    if value["include_reachable_history"] is not True or value["allow_encrypted_source"] is not True or value["execution_authority"] is not False:
        raise RecoveryError("source handoff permits encrypted committed history, never execution")
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise RecoveryError("source handoff proposal digest differs")
    return deepcopy(value)


def authorize(state, proposal, by, now):
    proposal = validate_proposal(proposal)
    _owner(state, by)
    source = proposal["source_binding"]
    record = state.get("execution_targets", {}).get(proposal["target"]["generation"])
    if (state.get("terminal") is not None or source != state.get("source_binding")
            or (source["run_id"], source["plan_digest"], proposal["owner"]) != (state["run_id"], state["plan_digest"], by)
            or record is None or record["status"] != "adopted"
            or record["proposal"]["digest"] != proposal["adoption_digest"]
            or record["proposal"]["descriptor"] != proposal["target"]):
        raise RecoveryError("source handoff no longer binds the approved source and active target")
    moment = parse_timestamp(now, "source authorization time")
    if not parse_timestamp(proposal["issued_at"], "issued") <= moment < parse_timestamp(proposal["expires_at"], "expires"):
        raise RecoveryError("source handoff approval is expired or future issued")
    if proposal["schema_version"] == 2:
        from .task_source import authorize_selection
        authorize_selection(state, proposal["selection"])
    return proposal


def propose(state, *, generation, adoption_digest, destination_workspace, request_id, by, issued_at, expires_at, task_id=None, source_workspace=None):
    _owner(state, by)
    require_identifier(generation, "source target generation")
    record = state.get("execution_targets", {}).get(generation)
    if record is None or state.get("source_binding") is None:
        raise RecoveryError("source handoff requires an adopted target and pre-approved source baseline")
    value = dict(schema="camol.source_handoff_proposal", schema_version=1, request_id=request_id, owner=by,
        source_binding=deepcopy(state["source_binding"]), target=deepcopy(record["proposal"]["descriptor"]),
        adoption_digest=adoption_digest, destination_workspace=destination_workspace,
        issued_at=issued_at, expires_at=expires_at, include_reachable_history=True,
        allow_encrypted_source=True, execution_authority=False)
    if task_id is not None:
        from .task_source import select
        value.update(schema_version=2, selection=select(state, task_id, source_workspace=source_workspace))
    elif source_workspace is not None:
        raise RecoveryError("an explicit source workspace requires a task-bound handoff")
    value["digest"] = canonical_digest(value)
    authorize(state, value, by, issued_at)
    if Redactor().value(value) != value:
        raise RecoveryError("source handoff proposal contains protected material")
    return value


def captured_source(proposal):
    return proposal["selection"]["source"] if proposal["schema_version"] == 2 else proposal["source_binding"]["source"]


def _scaffold(proposal):
    """Internal empty-diff input to the existing decoder, not a salvage claim."""
    source = captured_source(proposal)
    return SalvageReceipt(salvage_id="source-handoff", workspace_id="source-handoff",
        workspace_digest=canonical_digest(proposal["source_binding"]), base_revision=source["revision"],
        head_revision=source["revision"], patch_digest=EMPTY_DIGEST, patch_bytes=0,
        untracked=(), created_at=proposal["issued_at"])


def _restore(git, output, proposal, bundle):
    tree = _materialize(git, output, _scaffold(proposal), bundle, {EMPTY_DIGEST: b""})
    actual = source_identity(Path(output))
    expected = dict(captured_source(proposal), workspace=str(Path(output).resolve()))
    if actual != expected or tree != expected["tree"]:
        raise RecoveryError("received source differs from the approved commit, tree or working bytes")
    return actual


def _receipt(proposal, raw, exported_at):
    value = dict(schema="camol.source_handoff_export", schema_version=1, request_id=proposal["request_id"],
        proposal_digest=proposal["digest"], capsule_digest=_hash(raw), capsule_bytes=len(raw), exported_at=exported_at,
        source_binding_digest=canonical_digest(proposal["source_binding"]), target_generation=proposal["target"]["generation"],
        execution_authority=False, readiness_proven=False)
    return dict(value, digest=canonical_digest(value))


def apply(state, event):
    value = event["payload"]
    _fields(value, {"proposal", "receipt"})
    proposal = authorize(state, value["proposal"], event["actor_id"], event["occurred_at"])
    if event["run_id"] != state["run_id"]:
        raise RecoveryError("source export event belongs to another run")
    receipt = value["receipt"]
    _fields(receipt, {"schema", "schema_version", "request_id", "proposal_digest", "capsule_digest", "capsule_bytes",
        "exported_at", "source_binding_digest", "target_generation", "execution_authority", "readiness_proven", "digest"})
    if (receipt["schema"] != "camol.source_handoff_export" or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["request_id"] != proposal["request_id"] or receipt["proposal_digest"] != proposal["digest"]
            or receipt["source_binding_digest"] != canonical_digest(proposal["source_binding"])
            or receipt["target_generation"] != proposal["target"]["generation"]
            or receipt["execution_authority"] is not False or receipt["readiness_proven"] is not False
            or type(receipt["capsule_bytes"]) is not int or not len(MAGIC) + 28 <= receipt["capsule_bytes"] <= MAX_CAPSULE
            or receipt["digest"] != canonical_digest({k: v for k, v in receipt.items() if k != "digest"})):
        raise RecoveryError("invalid source export receipt or authority claim")
    require_digest(receipt["capsule_digest"], "source capsule digest")
    exported = parse_timestamp(receipt["exported_at"], "source exported")
    if not parse_timestamp(proposal["issued_at"], "issued") <= exported <= parse_timestamp(event["occurred_at"], "source event"):
        raise RecoveryError("invalid source export timestamp ordering")
    records = state.setdefault("source_handoffs", {})
    if proposal["request_id"] in records or len(records) >= MAX_EXPORTS:
        raise RecoveryError("source export request reused or history full")
    records[proposal["request_id"]] = deepcopy(value)


def _read(archive, key, proposal):
    cipher = _crypto(key)
    with ArchiveRoot(archive) as root:
        raw = root.read("source.camol", MAX_CAPSULE)
        receipt = decode_contract(root.read("receipt.json", 65536), max_bytes=65536)
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 28:
        raise RecoveryError("invalid encrypted source capsule")
    try:
        plaintext = cipher.decrypt(raw[len(MAGIC):len(MAGIC) + 12], raw[len(MAGIC) + 12:], MAGIC)
    except Exception as error:
        raise RecoveryError("source capsule authentication failed") from error
    value = decode_contract(plaintext, max_bytes=MAX_CAPSULE)
    _fields(value, {"schema", "schema_version", "proposal", "exported_at", "bundle"})
    if value["schema"] != "camol.source_handoff_payload" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RecoveryError("invalid encrypted source payload schema")
    if canonical_digest(value["proposal"]) != canonical_digest(proposal):
        raise RecoveryError("source capsule belongs to a different reviewed source or target")
    moment = parse_timestamp(value["exported_at"], "source capture time")
    if not parse_timestamp(proposal["issued_at"], "issued") <= moment < parse_timestamp(proposal["expires_at"], "expires"):
        raise RecoveryError("source capsule was not captured within its approval window")
    expected = _receipt(proposal, raw, value["exported_at"])
    if canonical_digest(receipt) != canonical_digest(expected):
        raise RecoveryError("source export receipt differs from its authenticated content")
    return raw, receipt, _decode_blob(value["bundle"], MAX_BUNDLE)


@_public
def export_source(*, proposal, by, review_digest, get_state, key, output, clock=None):
    """Create a private package; no network, target creation, or worker launch."""
    proposal = validate_proposal(proposal)
    if review_digest != proposal["digest"] or not callable(get_state):
        raise RecoveryError("source export requires exact approval and current controller state")
    clock = clock or (lambda: datetime.now(timezone.utc))
    check = lambda: authorize(get_state(), proposal, by, clock().isoformat())
    check()
    cipher = _crypto(key)
    source = Path(captured_source(proposal)["workspace"])
    baseline = proposal["source_binding"]["source"]
    _separate(output, (source, baseline["workspace"]))
    if Path(output).exists():
        _, receipt, bundle = _read(output, key, proposal)
        with tempfile.TemporaryDirectory(prefix="camol-source-check-") as scratch:
            _restore(_Git(scratch), Path(scratch) / "check", proposal, bundle)
        check()
        return receipt  # Exact package retry, never another encryption/publication.
    assert_source(baseline, Path(baseline["workspace"]))
    assert_source(captured_source(proposal), source)
    with tempfile.TemporaryDirectory(prefix="camol-source-export-") as scratch:
        git = _Git(scratch)
        common = Path(os.fsdecode(git.call(source, "rev-parse", "--git-common-dir")).strip())
        _separate(output, (common if common.is_absolute() else source / common,))
        if git.call(source, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise RecoveryError("source handoff requires complete captured Git history")
        revision = captured_source(proposal)["revision"]
        packed = git.call(source, "pack-objects", "--stdout", "--revs", "--no-reuse-delta",
                          data=(revision + "\n").encode(), maximum=MAX_BUNDLE)
        bundle = _header(_scaffold(proposal)) + packed
        _restore(git, Path(scratch) / "check", proposal, bundle)
        assert_source(baseline, Path(baseline["workspace"]))
        assert_source(captured_source(proposal), source)
        check()
        exported_at = clock().isoformat()
        value = dict(schema="camol.source_handoff_payload", schema_version=1, proposal=proposal,
                     exported_at=exported_at, bundle=base64.b64encode(bundle).decode("ascii"))
        plain = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(plain) + len(MAGIC) + 28 > MAX_CAPSULE:
            raise RecoveryError("source capsule exceeds its byte ceiling")
        nonce = os.urandom(12)
        raw = MAGIC + nonce + cipher.encrypt(nonce, plain, MAGIC)
        receipt = _receipt(proposal, raw, exported_at)
        check()
        with ArchiveRoot(output, create=True) as root:
            root.write("source.camol", raw)
            root.write("receipt.json", json.dumps(receipt, sort_keys=True).encode())
        return receipt


@_public
def receive_source(*, archive, key, proposal, review_digest, target_id, generation, output, by, clock=None):
    """Explicit receiver-side copy approval, not a controller lease or live admission."""
    proposal = validate_proposal(proposal)
    clock = clock or (lambda: datetime.now(timezone.utc))
    def fresh():
        if not parse_timestamp(proposal["issued_at"], "issued") <= clock() < parse_timestamp(proposal["expires_at"], "expires"):
            raise RecoveryError("source receive approval is expired or future issued")
    fresh()
    if (review_digest != proposal["digest"] or by != proposal["owner"]
            or (target_id, generation) != (proposal["target"]["target_id"], proposal["target"]["generation"])):
        raise RecoveryError("source receiver must approve the exact owner/source/target proposal")
    selected = Path(output).absolute()
    if str(selected.parent.resolve() / selected.name) != proposal["destination_workspace"]:
        raise RecoveryError("source receiver output differs from the reviewed destination")
    _separate(output, (archive,))
    raw, receipt, bundle = _read(archive, key, proposal)
    with tempfile.TemporaryDirectory(prefix="camol-source-receive-") as scratch:
        actual = _restore(_Git(scratch), output, proposal, bundle)
    fresh()
    value = dict(schema="camol.source_handoff_received", schema_version=1, proposal_digest=proposal["digest"],
        export_digest=receipt["digest"], capsule_digest=_hash(raw), source=actual,
        target_id=target_id, generation=generation, execution_authority=False, readiness_proven=False,
        basis="verified_source_copy_not_target_attestation_or_live_controller_authorization")
    return dict(value, digest=canonical_digest(value))
