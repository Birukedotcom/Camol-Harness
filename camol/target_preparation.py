"""Owner-approved target-local preparation, not a distributed execution grant."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from .admission import AdmissionBundle, AdmissionController
from .admission_process import prepare_async
from .archive_io import ArchiveRoot
from .json_contracts import decode_contract
from .probes import Redactor
from .recovery import RecoveryError, _separate
from .runbook import validate_runbook, runbook_digest
from .schema import canonical_digest, canonical_json_bytes, parse_timestamp, require_digest, require_identifier
from .source_binding import assert_source, make_binding
from .source_handoff import validate_proposal as validate_source_proposal, captured_source, _read, _path
from .workspace import WorkspaceManager


MAX_BYTES = 20 << 20
EFFECTS = dict(create_isolated_worktree=True, write_private_state=True, mutate_received_git_metadata=True,
               launch_worker=False, provision_target=False, allow_model_spend=False)


def _now():
    return datetime.now(timezone.utc)


def propose(*, source_proposal, source_export_digest, runbook, agent_id, evaluator_digest,
            state_dir, request_id, by, issued_at, expires_at):
    source_proposal = validate_source_proposal(source_proposal)
    if source_proposal["schema_version"] != 2 or by != source_proposal["owner"]:
        raise RecoveryError("target preparation requires the exact V2 source owner")
    runbook = validate_runbook(runbook)
    task_id = source_proposal["selection"]["task_id"]
    task = next((item for item in runbook["tasks"] if item["id"] == task_id), None)
    worker = next((item for item in runbook["agents"] if item["id"] == agent_id), None)
    if (runbook_digest(runbook) != source_proposal["source_binding"]["plan_digest"]
            or runbook["run"]["id"] != source_proposal["source_binding"]["run_id"]
            or task is None or canonical_digest(task) != source_proposal["selection"]["task_digest"]
            or worker is None or not set(task["capabilities"]).issubset(worker["capabilities"])):
        raise RecoveryError("target preparation must bind a compatible frozen task and worker")
    for value, name in ((source_export_digest, "source export"), (evaluator_digest, "controller evaluator")):
        require_digest(value, name)
    for value, name in ((request_id, "preparation request"), (agent_id, "preparation worker")):
        require_identifier(value, name)
        if len(value) > 128:
            raise RecoveryError("preparation identity exceeds its bound")
    _path(state_dir)
    start, expiry = parse_timestamp(issued_at, "preparation issued"), parse_timestamp(expires_at, "preparation expires")
    if not parse_timestamp(source_proposal["issued_at"], "source issued") <= start < expiry <= parse_timestamp(source_proposal["expires_at"], "source expires"):
        raise RecoveryError("preparation approval must fit inside the source approval window")
    value = dict(schema="camol.target_preparation_proposal", schema_version=1, source_proposal=source_proposal,
        source_export_digest=source_export_digest, runbook=runbook, agent_id=agent_id, evaluator_digest=evaluator_digest,
        state_dir=state_dir, request_id=request_id, owner=by, issued_at=issued_at, expires_at=expires_at,
        effects=deepcopy(EFFECTS))
    if Redactor().value(value) != value or len(canonical_json_bytes(value)) > MAX_BYTES:
        raise RecoveryError("preparation proposal contains protected material or exceeds its byte bound")
    return dict(value, digest=canonical_digest(value))


def validate(value):
    try:
        expected = propose(source_proposal=value["source_proposal"], source_export_digest=value["source_export_digest"],
            runbook=value["runbook"], agent_id=value["agent_id"], evaluator_digest=value["evaluator_digest"],
            state_dir=value["state_dir"], request_id=value["request_id"], by=value["owner"],
            issued_at=value["issued_at"], expires_at=value["expires_at"])
        if canonical_digest(value) != canonical_digest(expected):
            raise RecoveryError("preparation proposal changed its exact contract or effect scope")
        return expected
    except (TypeError, KeyError) as error:
        raise RecoveryError("invalid preparation proposal") from error


def _fresh(proposal):
    if not parse_timestamp(proposal["issued_at"], "preparation issued") <= _now() < parse_timestamp(proposal["expires_at"], "preparation expires"):
        raise RecoveryError("preparation approval expired or is future issued")


def _source(proposal):
    handoff = proposal["source_proposal"]
    return dict(captured_source(handoff), workspace=handoff["destination_workspace"])


def _private(root):
    if root.identity.st_uid != os.getuid() or root.identity.st_mode & 0o077:
        raise RecoveryError("preparation state must be a private owner directory")


def _result(proposal, status, started, *, bundle=None, error=None):
    finished = _now().isoformat()
    value = dict(schema="camol.target_preparation_result", schema_version=1,
        proposal_digest=proposal["digest"], request_id=proposal["request_id"], status=status,
        finished_at=finished, elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        source_binding=make_binding(_source(proposal), proposal["runbook"]["run"]["id"], runbook_digest(proposal["runbook"])),
        bundle=bundle.to_dict() if bundle else None, bundle_digest=bundle.digest() if bundle else None,
        error_kind=error, execution_authority=False, controller_admission=False, target_authenticated=False,
        global_capacity_reserved=False, worker_started=False,
        partial_output_retained=status != "prepared",
        basis="retained_target_preparation_not_live_controller_admission_or_machine_attestation")
    return dict(value, digest=canonical_digest(value))


def _validate_result(value, proposal):
    fields = {"schema", "schema_version", "proposal_digest", "request_id", "status", "finished_at", "elapsed_ms",
              "source_binding", "bundle", "bundle_digest", "error_kind", "execution_authority", "controller_admission",
              "target_authenticated", "global_capacity_reserved", "worker_started", "partial_output_retained", "basis", "digest"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.target_preparation_result"
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["proposal_digest"] != proposal["digest"] or value["request_id"] != proposal["request_id"]
            or not isinstance(value["status"], str) or value["status"] not in {"prepared", "failed", "cancelled"}
            or value["digest"] != canonical_digest({k: v for k, v in value.items() if k != "digest"})
            or type(value["elapsed_ms"]) is not int or not 0 <= value["elapsed_ms"] < 2**63
            or value["basis"] != "retained_target_preparation_not_live_controller_admission_or_machine_attestation"
            or any(value[field] is not False for field in ("execution_authority", "controller_admission", "target_authenticated", "global_capacity_reserved", "worker_started"))
            or value["partial_output_retained"] is not (value["status"] != "prepared")):
        raise RecoveryError("invalid retained preparation result")
    finished = parse_timestamp(value["finished_at"], "preparation finished")
    if finished < parse_timestamp(proposal["issued_at"], "preparation issued") or (
            value["status"] == "prepared" and finished >= parse_timestamp(proposal["expires_at"], "preparation expires")):
        raise RecoveryError("preparation result falls outside its approved window")
    if value["source_binding"] != make_binding(_source(proposal), proposal["runbook"]["run"]["id"], runbook_digest(proposal["runbook"])):
        raise RecoveryError("preparation result source identity changed")
    if value["status"] == "prepared":
        candidate = AdmissionBundle.from_dict(value["bundle"])
        handoff = proposal["source_proposal"]
        if (value["error_kind"] is not None or candidate.digest() != value["bundle_digest"]
                or candidate.digest() != canonical_digest(value["bundle"])
                or (candidate.binding.run_id, candidate.binding.plan_digest, candidate.binding.task_id,
                    candidate.binding.worker_id, candidate.binding.box_id, candidate.binding.target_id) != (
                    proposal["runbook"]["run"]["id"], runbook_digest(proposal["runbook"]), handoff["selection"]["task_id"],
                    proposal["agent_id"], proposal["agent_id"], handoff["target"]["target_id"])
                or candidate.workspace.base_revision != _source(proposal)["revision"]
                or candidate.grant.granted_by != proposal["owner"]):
            raise RecoveryError("prepared candidate differs from the approved subject")
        try:
            _path(candidate.workspace.path)
            Path(candidate.workspace.path).relative_to(Path(proposal["state_dir"]) / "worktrees")
        except ValueError as error:
            raise RecoveryError("prepared workspace is outside its declared state directory") from error
    elif value["bundle"] is not None or value["bundle_digest"] is not None or value["error_kind"] not in ("preparation_failed", "cancelled"):
        raise RecoveryError("incomplete preparation cannot claim an admission candidate")
    return value


def inspect(state_dir):
    """Noncreating retained metadata; does not re-probe or revive expired receipts."""
    with ArchiveRoot(state_dir) as root:
        _private(root)
        intent = decode_contract(root.read("preparation-intent.json", MAX_BYTES), max_bytes=MAX_BYTES)
        proposal = validate(intent)
        if str(root.path) != proposal["state_dir"]:
            raise RecoveryError("preparation journal belongs to another directory")
        try:
            result = decode_contract(root.read("preparation-result.json", MAX_BYTES), max_bytes=MAX_BYTES)
        except FileNotFoundError:
            result = None
        if result is not None:
            _validate_result(result, proposal)
    return dict(proposal=proposal, result=result, status="PREPARATION_UNKNOWN" if result is None else result["status"])


async def prepare(proposal, *, by, review_digest, archive, key):
    """Prepare through a tracked child; no kernel state or worker invocation."""
    proposal = validate(proposal)
    if by != proposal["owner"] or review_digest != proposal["digest"]:
        raise RecoveryError("preparation requires exact human review of its filesystem effects")
    state_dir = Path(proposal["state_dir"])
    intent = state_dir / "preparation-intent.json"
    if intent.exists() or intent.is_symlink():
        prior = inspect(state_dir)
        if prior["proposal"] != proposal:
            raise RecoveryError("preparation directory already names a different request")
        if prior["result"] is None:
            raise RecoveryError("PREPARATION_UNKNOWN: inspect retained work; preparation will not automatically repeat")
        return prior["result"]  # Historical result, never fresh admission or another process.
    _fresh(proposal)
    handoff = proposal["source_proposal"]
    source = _source(proposal)
    _separate(state_dir, (source["workspace"], archive))
    if not state_dir.parent.is_dir() or state_dir.parent.resolve() != state_dir.parent:
        raise RecoveryError("preparation requires an existing canonical parent directory")
    _, export, _ = _read(archive, key, handoff)
    if export["digest"] != proposal["source_export_digest"]:
        raise RecoveryError("preparation source package differs from its reviewed export")
    assert_source(source, Path(source["workspace"]))
    manager = WorkspaceManager(Path(source["workspace"]), state_dir)
    if manager.git_common_dir != Path(source["workspace"]) / ".git":
        raise RecoveryError("preparation may mutate only the received standalone Git repository")
    _fresh(proposal)
    started = time.monotonic()
    # Exclusive intent publication wins the race before any Git/workspace write.
    with ArchiveRoot(state_dir, create=True) as root:
        root.write("preparation-intent.json", canonical_json_bytes(proposal))
    try:
        manager.bind_source_contract(make_binding(source, proposal["runbook"]["run"]["id"], runbook_digest(proposal["runbook"])))
        task = next(item for item in proposal["runbook"]["tasks"] if item["id"] == handoff["selection"]["task_id"])
        agent = next(item for item in proposal["runbook"]["agents"] if item["id"] == proposal["agent_id"])
        controller = AdmissionController(proposal["runbook"], manager, target_id=handoff["target"]["target_id"])
        bundle, _ = await prepare_async(controller, dict(plan_digest=runbook_digest(proposal["runbook"]), task=task,
            agent=agent, granted_by=by, base_revision=source["revision"], expected_evaluator_digest=proposal["evaluator_digest"]))
        assert_source(source, Path(source["workspace"]))
        _fresh(proposal)
        result = _result(proposal, "prepared", started, bundle=bundle)
    except asyncio.CancelledError:
        result = _result(proposal, "cancelled", started, error="cancelled")
        _save(state_dir, proposal, result)
        raise
    except Exception:
        result = _result(proposal, "failed", started, error="preparation_failed")
    _save(state_dir, proposal, result)
    return result


def _save(state_dir, proposal, result):
    _validate_result(result, proposal)
    with ArchiveRoot(state_dir) as root:
        _private(root)
        if decode_contract(root.read("preparation-intent.json", MAX_BYTES), max_bytes=MAX_BYTES) != proposal:
            raise RecoveryError("preparation intent changed during execution")
        root.write("preparation-result.json", canonical_json_bytes(result))
