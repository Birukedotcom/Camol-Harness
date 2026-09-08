"""Read-only target-local inspection of one authenticated, task-bound source copy."""

from datetime import datetime, timezone
import io
from pathlib import Path
import time

from .doctor import DoctorOptions, run_doctor
from .probes import Redactor
from .recovery import RecoveryError, _public, _separate
from .runbook import load_runbook, runbook_digest
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .source_binding import assert_source
from .source_handoff import _read, captured_source, validate_proposal


def _now():
    return datetime.now(timezone.utc)


@_public
def inspect_handoff(*, proposal, review_digest, by, archive, key, runbook, workspace,
                    state_dir, target_id, generation, agent_id, evaluator_digest):
    """No state initialization, reservation, inference call, source edit, or launch."""
    started = time.monotonic()
    proposal = validate_proposal(proposal)
    if proposal["schema_version"] != 2:
        raise RecoveryError("handoff doctor requires a task-bound V2 source proposal")
    if (review_digest != proposal["digest"] or by != proposal["owner"]
            or (target_id, generation) != (proposal["target"]["target_id"], proposal["target"]["generation"])):
        raise RecoveryError("handoff doctor requires the exact reviewed owner and target generation")
    require_identifier(agent_id, "handoff doctor worker")
    require_digest(evaluator_digest, "expected controller evaluator digest")
    def fresh():
        if not parse_timestamp(proposal["issued_at"], "source issued") <= _now() < parse_timestamp(proposal["expires_at"], "source expires"):
            raise RecoveryError("handoff doctor approval expired or is future issued")
    fresh()
    document = load_runbook(Path(runbook))
    task_id = proposal["selection"]["task_id"]
    task = next((item for item in document["tasks"] if item["id"] == task_id), None)
    if (runbook_digest(document) != proposal["source_binding"]["plan_digest"]
            or document["run"]["id"] != proposal["source_binding"]["run_id"]
            or task is None or canonical_digest(task) != proposal["selection"]["task_digest"]
            or not any(item["id"] == agent_id for item in document["agents"])):
        raise RecoveryError("handoff doctor plan, task or worker differs from the approved contract")
    expected = dict(captured_source(proposal), workspace=proposal["destination_workspace"])
    if str(Path(workspace).resolve()) != expected["workspace"]:
        raise RecoveryError("handoff doctor workspace differs from the reviewed destination")
    _separate(state_dir, (workspace, archive))
    _, receipt, _ = _read(archive, key, proposal)
    assert_source(expected, Path(workspace))
    # Use the already validated document, not a second read of a mutable path.
    doctor = run_doctor(DoctorOptions(runbook=Path(runbook), workspace=Path(workspace),
        state_dir=Path(state_dir), json_output=True, target_id=target_id, task_id=task_id, agent_id=agent_id),
        document=document, stdout=io.StringIO()).payload
    assert_source(expected, Path(workspace))
    fresh()
    problems = []
    if doctor["evaluator_digest"] != evaluator_digest:
        problems.append(dict(code="EVALUATOR_CONFLICT", detail="target evaluator differs from the supplied frozen controller digest"))
    exit_code = 3 if doctor["exit_code"] == 3 else 2 if problems or doctor["exit_code"] else 0
    expiry = min(parse_timestamp(proposal["expires_at"], "source expires"),
                 parse_timestamp(doctor["expires_at"], "doctor expires"))
    value = dict(schema="camol.handoff_doctor", schema_version=1,
        run_id=document["run"]["id"], plan_digest=doctor["plan_digest"], task_id=task_id, agent_id=agent_id,
        target_id=target_id, generation=generation, proposal_digest=proposal["digest"],
        selection_digest=proposal["selection"]["digest"], export_digest=receipt["digest"],
        capsule_digest=receipt["capsule_digest"], source=expected, source_matches=True,
        expected_evaluator_digest=evaluator_digest, evaluator_matches=not problems,
        evaluator_authority_basis="caller_supplied_expected_digest_not_controller_attestation",
        observed_at=doctor["observed_at"], expires_at=expiry.isoformat(),
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        read_only=True, controller_state_checked=False, target_authenticated=False,
        execution_authority=False, lease_authorized=False, model_calls=0,
        verdict="probe_failure" if exit_code == 3 else "not_ready" if exit_code == 2 else "selected_probes_coherent",
        exit_code=exit_code, problems=problems, doctor=doctor,
        basis="target_local_observation_not_live_controller_admission_or_machine_attestation",
        note="Source authentication and selected probes do not authenticate this machine, allocate an isolated box, reserve capacity, grant authority, or lease work. Revalidate current controller task/source/target state before any launch. The outer expiry also limits use of the nested probe receipts.")
    value = Redactor().value(value)
    return dict(value, digest=canonical_digest(value))
