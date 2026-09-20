"""State-model gates backed by frozen verifier commands and replayed evidence."""

from datetime import datetime, timedelta
from uuid import uuid4

from .admission import AdmissionBundle
from .evaluation import CandidateRecord, IntegrationReceipt
from .evidence import EvidenceRecord
from .gates import GateApproval, GateAssessment, GateBinding, GateError, GateObservation, GatePolicy, Invariant, Obligation, assess_gate
from .schema import canonical_digest, require_digest, require_identifier, require_timestamp


GATE_EVENTS = frozenset({"GATE_ASSESSED", "GATE_APPROVED", "RUN_AWAITING_ACCEPTANCE", "RUN_ACCEPTED"})


def task_gate(state, task_id):
    if state["runbook"]["schema_version"] < 5:
        return None
    return next(item for item in state["runbook"]["state_model"]["gates"] if item["task_id"] == task_id)


def binding_for(state, candidate, phase, revision=None):
    if phase not in {"candidate", "integration"}:
        raise GateError("gate phase must be candidate or integration")
    admission = AdmissionBundle.from_dict(state["admissions"][candidate.task_id + ":" + candidate.agent_id])
    if phase == "integration" and not revision:
        raise GateError("integration gate requires the exact integrated revision")
    artifact = candidate.salvage.digest() if phase == "candidate" else canonical_digest({"candidate_digest": candidate.digest(), "revision": revision})
    environment = canonical_digest({"target_id": admission.binding.target_id, "runtime_id": admission.receipt.runtime_id,
                                    "probe_policy_digest": admission.probe_policy.digest(), "authority_digest": admission.authority_policy.digest()})
    return GateBinding(candidate.run_id, state["plan_digest"], candidate.evaluator_digest, artifact, environment)


def _assess(state, candidate, phase, revision, check_ids, result_ids, now):
    specification = task_gate(state, candidate.task_id)
    if specification is None:
        raise GateError("legacy runbooks do not have state-model gates")
    binding = binding_for(state, candidate, phase, revision)
    policy = GatePolicy.from_dict(specification["policy"])
    if len(check_ids) != len(result_ids):
        raise GateError("gate command/result evidence pairs differ")
    commands = []
    results = []
    for command_id, result_id in zip(check_ids, result_ids):
        try:
            command = EvidenceRecord.from_dict(state["evidence"][command_id])
            result = EvidenceRecord.from_dict(state["evidence"][result_id])
        except (KeyError, ValueError) as error:
            raise GateError("gate refers to missing or invalid evidence") from error
        for item in (command, result):
            if item.run_id != candidate.run_id or item.task_id != candidate.task_id or item.agent_id != candidate.agent_id or item.lease_id != candidate.lease_id or item.fence_digest != candidate.fence_digest or item.epistemic_status != "EXECUTED" or item.producer != "verifier":
                raise GateError("gate evidence does not bind the candidate's independent verifier")
        if command.kind != "command" or result.kind != "test_result" or result.data != {"check_evidence_id": command_id, "check_digest": canonical_digest(command.data), "passed": command.data.get("passed") is True, "phase": phase}:
            raise GateError("gate test result does not derive from its executed command")
        if command.data.get("phase") != phase or command.data.get("evaluator_digest") != candidate.evaluator_digest:
            raise GateError("gate command has a different phase or evaluator identity")
        commands.append(command)
        results.append(result)
    own = [(command, result) for command, result in zip(commands, results) if command.data.get("evaluated_task_id") == candidate.task_id]
    plan_task = state["tasks"][candidate.task_id]
    mapped = []
    for mapping in specification["evaluators"]:
        index = mapping["verification_index"]
        if index >= len(own):
            raise GateError("gate is missing an executed frozen evaluator")
        command, result = own[index]
        expected = plan_task["verification"][index]
        if command.data.get("argv") != expected["argv"] or command.data.get("purpose") != expected["purpose"]:
            raise GateError("executed evaluator does not match its frozen mapping")
        verdict = "SUPPORTED_BY_REQUIRED_EVIDENCE" if command.data.get("passed") is True and command.data.get("exit_code") == 0 else "DISPROVED"
        for invariant_id in mapping["invariant_ids"]:
            for source in (command, result):
                mapped.append(GateObservation("gate-" + canonical_digest({"evidence": source.evidence_id, "invariant": invariant_id})[7:31], binding,
                                              invariant_id, mapping["family"], "kernel-verifier", verdict, source))
    # Unmapped guardrail checks are still binding: a failure cannot be hidden
    # by mapping only the convenient subset of evaluator outputs.
    if any(command.data.get("passed") is not True or command.data.get("exit_code") != 0 for command in commands):
        first = commands[0]
        mapped.append(GateObservation("guardrail-" + first.evidence_id, binding, specification["invariant_ids"][0],
                                      "deterministic", "kernel-verifier", "DISPROVED", first))
    model = state["runbook"]["state_model"]
    approval_data = state.get("gate_approvals", {}).get(candidate.candidate_id + ":" + phase)
    approval = GateApproval.from_dict(approval_data) if approval_data else None
    return assess_gate(binding=binding, policy=policy,
                       invariants=[Invariant.from_dict(item) for item in model["invariants"] if item["invariant_id"] in specification["invariant_ids"]],
                       obligations=[Obligation.from_dict(item) for item in model["obligations"] if item["obligation_id"] in specification["obligation_ids"]],
                       observations=mapped, builder_ids=list(state["agents"]), now=now, approval=approval)


def require_integration_gate(state, receipt):
    if state["runbook"]["schema_version"] < 5:
        return
    candidate = CandidateRecord.from_dict(state["candidates"][receipt.candidate_id])
    for phase in ("candidate", "integration"):
        payload = state.get("gate_assessments", {}).get(candidate.candidate_id + ":" + phase)
        if payload is None:
            raise GateError("integration is missing its " + phase + " invariant gate")
        assessment = GateAssessment.from_dict(payload["assessment"])
        expected = binding_for(state, candidate, phase, receipt.revision if phase == "integration" else None)
        if not assessment.passed or assessment.binding != expected:
            raise GateError("integration gate is not green for the exact candidate/revision")
        if phase == "integration" and payload["checks_digest"] != receipt.checks_digest:
            raise GateError("integration gate does not bind all integration checks")


def acceptance_digest(state):
    """Final owner approval names the exact integrated result and gate history."""
    return canonical_digest({"run_id": state["run_id"], "plan_digest": state["plan_digest"],
                             "integration_head": state.get("integration_head"), "integrations": state.get("integrations", []),
                             "gates": state.get("gate_assessments", {}),
                             "evidence": state.get("evidence", {}), "debug_cases": state.get("debug_cases", {})})


def apply_gate_event(state, event):
    payload = event["payload"]
    if state["runbook"]["schema_version"] < 5:
        raise GateError("state-model gate events require schema v5")
    kind = event["type"]
    if kind == "GATE_ASSESSED":
        expected = {"task_id", "candidate_id", "phase", "revision", "integration_receipt", "check_evidence_ids", "result_evidence_ids", "checks_digest", "assessed_at", "assessment", "assessment_digest"}
        if set(payload) != expected:
            raise GateError("gate assessment event has missing or unknown fields")
        candidate = CandidateRecord.from_dict(state["candidates"][payload["candidate_id"]])
        task = state["tasks"][candidate.task_id]
        if task["status"] != "verifying" or task["lease_id"] != candidate.lease_id or payload["task_id"] != candidate.task_id:
            raise GateError("gate assessment requires the candidate's active verifying task")
        assessment = _assess(state, candidate, payload["phase"], payload["revision"], payload["check_evidence_ids"], payload["result_evidence_ids"], payload["assessed_at"])
        checks = [state["evidence"][identifier]["data"] for identifier in payload["check_evidence_ids"]]
        if assessment.to_dict() != payload["assessment"] or assessment.digest() != payload["assessment_digest"] or canonical_digest(checks) != payload["checks_digest"]:
            raise GateError("gate assessment cannot be reproduced from its bound evidence")
        if payload["phase"] == "integration":
            receipt = IntegrationReceipt.from_dict(payload["integration_receipt"])
            if receipt.candidate_id != candidate.candidate_id or receipt.task_id != candidate.task_id or receipt.run_id != candidate.run_id or receipt.revision != payload["revision"] or receipt.checks_digest != payload["checks_digest"] or receipt.evaluator_digest != candidate.evaluator_digest:
                raise GateError("provisional integration receipt does not bind the gated artifact")
        elif payload["integration_receipt"] is not None:
            raise GateError("candidate gate cannot carry an integration receipt")
        state.setdefault("gate_assessments", {})[candidate.candidate_id + ":" + payload["phase"]] = payload
        if assessment.status == "AWAITING_HUMAN":
            task["gate_wait"] = dict(candidate_id=candidate.candidate_id, phase=payload["phase"], assessment_digest=assessment.digest(),
                                     binding=assessment.binding.to_dict(), policy_digest=assessment.policy_digest)
        else:
            task["gate_wait"] = None
    elif kind == "GATE_APPROVED":
        if set(payload) != {"task_id", "candidate_id", "phase", "assessment_digest", "approval"}:
            raise GateError("gate approval event has missing or unknown fields")
        task = state["tasks"][payload["task_id"]]
        waiting = task.get("gate_wait")
        approval = GateApproval.from_dict(payload["approval"])
        if not waiting or any(waiting[name] != payload[name] for name in ("candidate_id", "phase", "assessment_digest")) or approval.binding.to_dict() != waiting["binding"] or approval.policy_digest != waiting["policy_digest"]:
            raise GateError("gate approval does not match the pending candidate")
        if approval.approved_by != state["approved_by"] or approval.approved_by in state["agents"] or event["actor_id"] != approval.approved_by or not approval.fresh(event["occurred_at"]):
            raise GateError("gate approval requires the authorized human owner")
        state.setdefault("gate_approvals", {})[payload["candidate_id"] + ":" + payload["phase"]] = approval.to_dict()
        task["gate_wait"] = None
    elif kind == "RUN_AWAITING_ACCEPTANCE":
        if set(payload) != {"outcome_digest"} or state["status"] != "running" or any(task["status"] != "succeeded" for task in state["tasks"].values()):
            raise GateError("final acceptance requires all tasks succeeded")
        for receipt in state["integrations"]:
            require_integration_gate(state, IntegrationReceipt.from_dict(receipt))
        if payload["outcome_digest"] != acceptance_digest(state):
            raise GateError("final acceptance outcome digest changed")
        state["status"] = "awaiting_acceptance"
        state["acceptance"] = payload
    elif kind == "RUN_ACCEPTED":
        if set(payload) != {"outcome_digest", "approved_by"} or state["status"] != "awaiting_acceptance":
            raise GateError("run is not awaiting final acceptance")
        if payload["outcome_digest"] != acceptance_digest(state) or payload["outcome_digest"] != state["acceptance"]["outcome_digest"]:
            raise GateError("final acceptance names a stale outcome")
        if payload["approved_by"] != state["approved_by"] or payload["approved_by"] in state["agents"] or event["actor_id"] != payload["approved_by"]:
            raise GateError("final acceptance requires the authorized human owner")
        state["status"] = "completed"
        state["terminal"] = dict(verdict="human accepted the exact integrated outcome", **payload)


class GateOrchestratorMixin:
    def assess_task_gate(self, run_id, assignment, candidate_id, checks, *, phase, revision=None, integration_receipt=None):
        state = self.state(run_id)
        if task_gate(state, assignment["task_id"]) is None:
            return None
        self._require_lease(run_id, assignment["task_id"], assignment["agent_id"], assignment["lease_id"], "verifying", fence_digest=assignment.get("fence_digest"))
        candidate = CandidateRecord.from_dict(state["candidates"][candidate_id])
        if candidate.task_id != assignment["task_id"] or candidate.lease_id != assignment["lease_id"]:
            raise GateError("gate candidate belongs to another task lease")
        check_ids, result_ids = [], []
        for check in checks:
            clean = self.redactor.value({key: value for key, value in check.items() if key != "artifact_refs"})
            command = next((item for item in reversed(list(state["evidence"].values())) if item.get("kind") == "command" and item.get("producer") == "verifier" and item.get("epistemic_status") == "EXECUTED" and item.get("lease_id") == candidate.lease_id and item.get("data") == clean), None)
            if command is None:
                raise GateError("gate requires persisted independent verifier commands")
            check_ids.append(command["evidence_id"])
            result_id = self.record_observed_evidence(run_id, assignment, kind="test_result", epistemic_status="EXECUTED", producer="verifier",
                                                       data={"check_evidence_id": command["evidence_id"], "check_digest": canonical_digest(clean), "passed": clean.get("passed") is True, "phase": phase})
            result_ids.append(result_id)
        state = self.state(run_id)
        now = self._now()
        assessment = _assess(state, candidate, phase, revision, check_ids, result_ids, now)
        payload = dict(task_id=candidate.task_id, candidate_id=candidate_id, phase=phase, revision=revision,
                                                 integration_receipt=integration_receipt.to_dict() if integration_receipt is not None else None,
                                                 check_evidence_ids=check_ids, result_evidence_ids=result_ids,
                                                 checks_digest=canonical_digest([state["evidence"][identifier]["data"] for identifier in check_ids]),
                                                 assessed_at=now, assessment=assessment.to_dict(), assessment_digest=assessment.digest())
        apply_gate_event(state, {"type": "GATE_ASSESSED", "payload": payload})
        self._emit(run_id, "GATE_ASSESSED", payload, expected_seq=state["last_seq"])
        return assessment

    def approve_task_gate(self, run_id, task_id, approved_by, assessment_digest):
        state = self._require_status(run_id, "running")
        pending = state["tasks"][task_id].get("gate_wait")
        if not pending or assessment_digest != pending["assessment_digest"]:
            raise GateError("approval requires the exact pending gate assessment")
        if approved_by != state["approved_by"] or approved_by in state["agents"]:
            raise GateError("gate approval requires the authorized human owner")
        now = self._now()
        approval = GateApproval("gate-approval-" + uuid4().hex, GateBinding.from_dict(pending["binding"]), pending["policy_digest"], approved_by,
                                now, (datetime.fromisoformat(now) + timedelta(hours=1)).isoformat())
        self._emit(run_id, "GATE_APPROVED", dict(task_id=task_id, candidate_id=pending["candidate_id"], phase=pending["phase"],
                                                assessment_digest=assessment_digest, approval=approval.to_dict()), actor_id=approved_by, expected_seq=state["last_seq"])

    def accept_run(self, run_id, approved_by, outcome_digest):
        state = self._require_status(run_id, "awaiting_acceptance")
        require_identifier(approved_by, "final approved_by")
        require_digest(outcome_digest, "final outcome digest")
        if approved_by != state["approved_by"] or approved_by in state["agents"]:
            raise GateError("final acceptance requires the authorized human owner")
        if outcome_digest != acceptance_digest(state) or outcome_digest != state["acceptance"]["outcome_digest"]:
            raise GateError("final acceptance names a stale outcome")
        self._emit(run_id, "RUN_ACCEPTED", {"approved_by": approved_by, "outcome_digest": outcome_digest}, actor_id=approved_by, expected_seq=state["last_seq"])
