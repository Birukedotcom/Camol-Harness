"""Evidence-gated debugging, independent of a terminal or model provider.

The debugger never labels a hypothesis as an observation. Its events share the run
ledger so experiments, contradictions and promoted regressions survive restart.
"""

from copy import deepcopy
from typing import Any, Dict, Iterable, Optional

from .evidence import EvidenceRecord
from .debug_execution import EXECUTION_EVENTS, apply_execution_event, validate_executed_evidence
from .events import EVIDENCE_KINDS
from .schema import canonical_digest, require_identifier, require_string


class DebuggerError(ValueError):
    pass


DEBUG_EVENTS = frozenset({
    "DEBUG_CASE_CREATED", "DEBUG_REPRODUCED", "DEBUG_LOCALIZED",
    "DEBUG_EXPERIMENT_STARTED", "DEBUG_EXPERIMENT_FINISHED", "DEBUG_VERIFIED",
    "DEBUG_CONTRADICTED", "DEBUG_BLOCKED", "DEBUG_RESUMED", "DEBUG_EVAL_PROMOTED",
    "DEBUG_COUNTEREXAMPLE_LINKED",
}) | EXECUTION_EVENTS
UNRESOLVED_DEBUG_STATES = frozenset({
    "open", "reproduced", "localized", "experimenting", "blocked",
})


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 16384:
        raise DebuggerError(name + " must be non-empty text of at most 16384 characters")
    return value


def _strings(values: Any, name: str) -> list:
    if not isinstance(values, list) or not values or len(values) > 256:
        raise DebuggerError(name + " must be a non-empty bounded array")
    return [_text(item, name) for item in values]


def validate_experiment(value: Any) -> Dict[str, Any]:
    fields = {
        "experiment_id", "hypothesis", "change", "reproduction", "expected_delta",
        "guardrails", "rollback_ref", "max_attempts", "max_seconds", "max_cost_usd_micros",
        "environment_digest", "candidate_digest", "author",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise DebuggerError("experiment must declare exactly: " + ", ".join(sorted(fields)))
    from .schema import require_digest
    clean = deepcopy(value)
    require_identifier(clean["experiment_id"], "experiment_id")
    require_identifier(clean["author"], "experiment author")
    for name in ("hypothesis", "change", "expected_delta", "rollback_ref"):
        _text(clean[name], name)
    _strings(clean["reproduction"], "experiment reproduction")
    if not isinstance(clean["guardrails"], list) or len(clean["guardrails"]) > 256:
        raise DebuggerError("guardrails must be a bounded array of names")
    for name in clean["guardrails"]:
        require_identifier(name, "guardrail")
    if len(set(clean["guardrails"])) != len(clean["guardrails"]):
        raise DebuggerError("guardrails must be unique")
    for name in ("max_attempts", "max_seconds", "max_cost_usd_micros"):
        if type(clean[name]) is not int or clean[name] < (0 if name == "max_cost_usd_micros" else 1):
            raise DebuggerError(name + " has an invalid bound")
    for name in ("environment_digest", "candidate_digest"):
        require_digest(clean[name], name)
    return clean


def _evidence(state: dict, case: dict, ids: Iterable[str], *, require_gate: bool = True) -> list:
    if not isinstance(ids, (list, tuple)) or not ids or len(set(ids)) != len(ids):
        raise DebuggerError("evidence_ids must be a non-empty unique array")
    records = []
    for identity in ids:
        if identity not in case["evidence_ids"]:
            raise DebuggerError("evidence does not belong to this debug case")
        record = EvidenceRecord.from_dict(state["evidence"][identity])
        if require_gate and (not record.satisfies_gate() or record.kind == "claim"):
            raise DebuggerError("reported or inferred evidence cannot establish executed behavior")
        records.append(record)
    return records


def _outcome(state: dict, case: dict, identity: str, *, passed: bool) -> EvidenceRecord:
    record = _evidence(state, case, [identity])[0]
    if (record.kind != "test_result" or record.epistemic_status != "EXECUTED"
            or record.data.get("passed") is not passed
            or record.data.get("target_digest") != case["target_digest"]):
        raise DebuggerError("outcome must be an executed test bound to the declared target")
    validate_executed_evidence(state, case, record)
    if record.data.get("guardrail") is not None:
        raise DebuggerError("a guardrail cannot stand in for the target reproduction")
    return record


def _counterexample_evidence(state: dict, counterexample: dict) -> list:
    candidate = state.get("candidates", {}).get(counterexample["candidate_id"], {})
    matches = []
    for identity, record in state["evidence"].items():
        if (record.get("kind") != "test_result" or record.get("epistemic_status") != "EXECUTED"
                or record.get("producer") != "verifier" or record.get("data", {}).get("passed") is not False):
            continue
        if any(record.get(name) != candidate.get(name) for name in ("task_id", "agent_id", "lease_id", "fence_digest")):
            continue
        if canonical_digest(record["data"].get("checks")) == counterexample["checks_digest"]:
            matches.append(identity)
    return sorted(matches)


def apply_debug_event(state: dict, event: dict) -> None:
    """Validate transitions during ingestion AND replay; mutate only the projection."""
    kind, p = event["type"], event["payload"]
    if kind in EXECUTION_EVENTS:
        apply_execution_event(state, event)
        return
    if not isinstance(p, dict) or type(p.get("schema_version")) is not int or p.get("schema_version") != 2:
        raise DebuggerError("debug protocol requires schema_version 2")
    fields = {
        "DEBUG_CASE_CREATED": {"observed_behavior", "target_behavior", "target_authority", "reproduction",
                               "required_evidence", "plan_digest", "target_digest"},
        "DEBUG_REPRODUCED": {"evidence_id"}, "DEBUG_LOCALIZED": {"chain", "evidence_ids"},
        "DEBUG_EXPERIMENT_STARTED": {"experiment"},
        "DEBUG_EXPERIMENT_FINISHED": {"experiment_id", "evidence_ids", "attempts", "duration_seconds", "cost_usd_micros"},
        "DEBUG_VERIFIED": {"evidence_id", "verdict"}, "DEBUG_CONTRADICTED": {"evidence_id", "reason"},
        "DEBUG_BLOCKED": {"reason"}, "DEBUG_RESUMED": {"resolution"},
        "DEBUG_EVAL_PROMOTED": {"eval_id", "definition"},
        "DEBUG_COUNTEREXAMPLE_LINKED": {"counterexample_id", "verifier_evidence_ids"},
    }
    if kind not in fields or set(p) != fields[kind] | {"schema_version", "case_id"}:
        raise DebuggerError("debug event has missing or unknown fields")
    identity = require_identifier(p.get("case_id"), "case_id")
    cases = state["debug_cases"]
    if kind == "DEBUG_CASE_CREATED":
        if not state.get("approved_by"):
            raise DebuggerError("approve the plan before declaring an authoritative debug target")
        if identity in cases:
            raise DebuggerError("debug case already exists")
        for name in ("observed_behavior", "target_behavior", "target_authority"):
            _text(p.get(name), name)
        _strings(p.get("reproduction"), "reproduction")
        required = _strings(p.get("required_evidence"), "required_evidence")
        if set(required) - EVIDENCE_KINDS or "test_result" not in required:
            raise DebuggerError("debug evidence must include test_result and known evidence kinds")
        if p.get("plan_digest") != state["plan_digest"]:
            raise DebuggerError("debug case is bound to another plan")
        if p.get("target_digest") != canonical_digest({
            "target_behavior": p["target_behavior"], "target_authority": p["target_authority"],
        }):
            raise DebuggerError("target digest does not match the approved behavior")
        cases[identity] = dict(deepcopy(p), status="open", evidence_ids=[], experiments=[], history=[])
        return
    case = cases.get(identity)
    if case is None or case.get("schema_version") != 2:
        raise DebuggerError("unknown versioned debug case")
    current = case["status"]
    if kind == "DEBUG_COUNTEREXAMPLE_LINKED":
        if event.get("actor_id") != state["approved_by"]:
            raise DebuggerError("activating a counterexample requires the approving owner")
        matches = [item for item in state.get("counterexamples", []) if item["counterexample_id"] == p["counterexample_id"]]
        if len(matches) != 1 or case.get("counterexample_id"):
            raise DebuggerError("counterexample is missing or already linked")
        evidence_ids = _counterexample_evidence(state, matches[0])
        if not evidence_ids or p["verifier_evidence_ids"] != evidence_ids:
            raise DebuggerError("counterexample activation must link its exact original verifier evidence")
        case.update(counterexample_id=p["counterexample_id"], counterexample_evidence_ids=evidence_ids)
        return
    allowed = {
        "DEBUG_REPRODUCED": {"open"}, "DEBUG_LOCALIZED": {"reproduced"},
        "DEBUG_EXPERIMENT_STARTED": {"localized", "experimenting"},
        "DEBUG_EXPERIMENT_FINISHED": {"experimenting"}, "DEBUG_VERIFIED": {"experimenting"},
        "DEBUG_CONTRADICTED": {"verified", "eval_promoted"},
        "DEBUG_BLOCKED": UNRESOLVED_DEBUG_STATES - {"blocked"},
        "DEBUG_RESUMED": {"blocked"}, "DEBUG_EVAL_PROMOTED": {"verified"},
    }
    if kind not in allowed or current not in allowed[kind]:
        raise DebuggerError("{} cannot transition from {}".format(kind, current))
    if kind == "DEBUG_REPRODUCED":
        _outcome(state, case, p.get("evidence_id"), passed=False)
        case.update(status="reproduced", red_before=p["evidence_id"])
    elif kind == "DEBUG_LOCALIZED":
        chain = p.get("chain")
        fields = {"symptom", "first_divergent_event", "deciding_boundary", "violated_contract", "cause"}
        if not isinstance(chain, dict) or set(chain) != fields:
            raise DebuggerError("localization requires the complete divergence chain")
        for name in fields:
            _text(chain[name], name)
        _evidence(state, case, p.get("evidence_ids"))
        case.update(status="localized", localization=deepcopy(p))
    elif kind == "DEBUG_EXPERIMENT_STARTED":
        experiment = validate_experiment(p.get("experiment"))
        if experiment["reproduction"] != case["reproduction"]:
            raise DebuggerError("an experiment cannot change the frozen reproduction")
        if case["experiments"] and case["experiments"][-1]["status"] == "running":
            raise DebuggerError("finish the current experiment before starting another")
        if any(item["experiment_id"] == experiment["experiment_id"] for item in case["experiments"]):
            raise DebuggerError("experiment identity is already used")
        case["experiments"].append(dict(experiment, status="running", attempts=0))
        case["status"] = "experimenting"
    elif kind == "DEBUG_EXPERIMENT_FINISHED":
        if not case["experiments"]:
            raise DebuggerError("no experiment was started")
        experiment = case["experiments"][-1]
        if experiment["status"] != "running" or p.get("experiment_id") != experiment["experiment_id"]:
            raise DebuggerError("result does not identify the running experiment")
        for name, maximum in (("attempts", "max_attempts"), ("duration_seconds", "max_seconds"),
                              ("cost_usd_micros", "max_cost_usd_micros")):
            value = p.get(name)
            if type(value) is not int or value < (1 if name == "attempts" else 0):
                raise DebuggerError(name + " must be a bounded integer")
            if value > experiment[maximum]:
                raise DebuggerError("experiment exceeded " + maximum)
        records = _evidence(state, case, p.get("evidence_ids"))
        for record in records:
            if record.kind == "test_result":
                validate_executed_evidence(state, case, record)
            for name in ("experiment_id", "environment_digest", "candidate_digest"):
                if record.data.get(name) != experiment[name]:
                    raise DebuggerError("experiment evidence changed " + name)
        if not any(item.kind == "test_result" and type(item.data.get("passed")) is bool for item in records):
            raise DebuggerError("experiment needs an executed outcome")
        executions = [item for item in case.get("executions", {}).values()
                      if item["experiment_id"] == experiment["experiment_id"]]
        finished = [item for item in executions if item["status"] == "finished"]
        from math import ceil
        actual_seconds = ceil(sum(result["duration_ms"] for item in finished for result in item["receipt"]["results"]) / 1000)
        if p["attempts"] != len(executions) or p["duration_seconds"] < actual_seconds or p["cost_usd_micros"] != 0:
            raise DebuggerError("experiment accounting contradicts its execution receipts")
        experiment.update(status="finished", result=deepcopy(p), attempts=p["attempts"])
    elif kind == "DEBUG_VERIFIED":
        if not case["experiments"] or case["experiments"][-1]["status"] != "finished":
            raise DebuggerError("verification requires a completed bounded experiment")
        experiment = case["experiments"][-1]
        records = _evidence(state, case, experiment["result"]["evidence_ids"])
        missing = set(case["required_evidence"]) - {item.kind for item in records}
        if missing:
            raise DebuggerError("missing required evidence: " + ", ".join(sorted(missing)))
        green = _outcome(state, case, p.get("evidence_id"), passed=True)
        if green.evidence_id not in experiment["result"]["evidence_ids"]:
            raise DebuggerError("verification is not from the current experiment")
        if green.producer == experiment["author"]:
            raise DebuggerError("the experiment author cannot be its only verifier")
        guardrails = {item.data.get("guardrail") for item in records
                      if item.kind == "test_result" and item.data.get("passed") is True}
        if set(experiment["guardrails"]) - guardrails:
            raise DebuggerError("required guardrail evidence is missing")
        if any(item.kind == "test_result" and item.data.get("passed") is not True for item in records):
            raise DebuggerError("a failed experiment check prevents verification")
        _text(p.get("verdict"), "verdict")
        case.update(status="verified", verification=deepcopy(p), green_after=green.evidence_id)
    elif kind == "DEBUG_CONTRADICTED":
        red = _outcome(state, case, p.get("evidence_id"), passed=False)
        green = state["evidence"][case["green_after"]]
        execution = case["executions"][red.data["execution_id"]]
        verification_seq = max(item["seq"] for item in case["history"] if item["type"] == "DEBUG_VERIFIED")
        if red.data["candidate_digest"] != green["data"]["candidate_digest"] or execution["started_seq"] <= verification_seq:
            raise DebuggerError("contradiction must be newly executed against the verified candidate")
        _text(p.get("reason"), "contradiction reason")
        if case.get("eval_id") in state["evals"]:
            state["evals"][case["eval_id"]]["review_status"] = "contradicted"
        case.update(status="experimenting", contradiction=deepcopy(p))
    elif kind == "DEBUG_BLOCKED":
        from .readiness import WaitingReason
        reason = WaitingReason.from_dict(p.get("reason"))
        case.update(status="blocked", resume_state=current, blocker=reason.to_dict())
    elif kind == "DEBUG_RESUMED":
        _text(p.get("resolution"), "blocker resolution")
        case.update(status=case["resume_state"], blocker=None)
    elif kind == "DEBUG_EVAL_PROMOTED":
        definition = p.get("definition")
        fields = {"revision", "fixture", "oracle", "environment_digest", "owner", "reviewed_by"}
        if not isinstance(definition, dict) or set(definition) != fields:
            raise DebuggerError("eval definition requires exactly: " + ", ".join(sorted(fields)))
        if type(definition["revision"]) is not int or definition["revision"] < 1:
            raise DebuggerError("eval revision must be positive")
        for name in ("fixture", "oracle", "owner", "reviewed_by"):
            _text(definition[name], name)
        if definition["reviewed_by"] != state["approved_by"]:
            raise DebuggerError("eval promotion requires the run owner's review")
        if event.get("actor_id") != state["approved_by"]:
            raise DebuggerError("eval promotion event must be authored by the approving human")
        if definition["environment_digest"] != case["experiments"][-1]["environment_digest"]:
            raise DebuggerError("eval environment must match verified experiment")
        eval_id = require_identifier(p.get("eval_id"), "eval_id")
        if eval_id in state["evals"]:
            raise DebuggerError("eval identity is already used")
        state["evals"][eval_id] = dict(deepcopy(p), red_before=case["red_before"],
                                       green_after=case["green_after"], review_status="accepted")
        case.update(status="eval_promoted", eval_id=eval_id)
    case["history"].append({"type": kind, "seq": event.get("seq"), "payload": deepcopy(p)})


class Debugger:
    """An embeddable debugger backed by an existing authoritative Orchestrator."""

    def __init__(self, orchestrator: Any, run_id: str):
        self.orchestrator, self.run_id = orchestrator, run_id

    def _state(self) -> dict:
        state = self.orchestrator.state(self.run_id)
        if state["status"] == "missing":
            raise DebuggerError("debugger requires an existing run")
        return state

    def _commit(self, kind: str, case_id: str, **payload: Any) -> dict:
        state = self._state()
        value = self.orchestrator.redactor.value(dict(payload, schema_version=2, case_id=case_id))
        actor = value.get("definition", {}).get("reviewed_by") if kind == "DEBUG_EVAL_PROMOTED" else self.orchestrator.actor_id
        event = {"type": kind, "payload": value, "seq": state["last_seq"] + 1, "actor_id": actor}
        apply_debug_event(deepcopy(state), event)
        self.orchestrator._emit(self.run_id, kind, value, actor_id=actor, expected_seq=state["last_seq"])
        return self.inspect(case_id)

    def _execution_commit(self, kind: str, case_id: str, actor: str, **payload: Any) -> dict:
        state = self._state()
        value = dict(payload, schema_version=2, case_id=case_id)
        if self.orchestrator.redactor.value(value) != value:
            raise DebuggerError("execution authority contains secret-shaped data and cannot be changed by redaction")
        event = {"type": kind, "payload": value, "seq": state["last_seq"] + 1, "actor_id": actor}
        apply_debug_event(deepcopy(state), event)
        self.orchestrator._emit(self.run_id, kind, value, actor_id=actor, expected_seq=state["last_seq"])
        return self.inspect(case_id)

    def inbox(self) -> list:
        state = self._state()
        result = []
        for counterexample in state.get("counterexamples", []):
            linked = [identity for identity, case in state["debug_cases"].items()
                      if case.get("counterexample_id") == counterexample["counterexample_id"]]
            result.append(dict(counterexample, debug_case_ids=linked,
                               status="activated" if linked else "untriaged", blocking=False,
                               verifier_evidence_ids=_counterexample_evidence(state, counterexample)))
        return result

    def activate_counterexample(self, counterexample_id: str, *, case_id: str,
                               target_behavior: str, target_authority: str, reproduction: list,
                               contract: dict, approved_by: str) -> dict:
        """Elevate a real failed-evaluator record without inventing localization."""
        from .debug_execution import validate_contract
        state = self._state()
        if approved_by != state["approved_by"]:
            raise DebuggerError("activating a counterexample requires the approving owner")
        matches = [item for item in state.get("counterexamples", []) if item["counterexample_id"] == counterexample_id]
        if len(matches) != 1:
            raise DebuggerError("unknown counterexample")
        evidence_ids = _counterexample_evidence(state, matches[0])
        if not evidence_ids:
            raise DebuggerError("counterexample has no matching original verifier evidence")
        validate_contract(contract)
        target = self.orchestrator.redactor.value({"target_behavior": target_behavior, "target_authority": target_authority})
        if (contract["case_id"] != case_id or contract["run_id"] != self.run_id
                or contract["target_digest"] != canonical_digest(target)
                or contract["plan_digest"] != state["plan_digest"] or contract["reviewed_by"] != approved_by):
            raise DebuggerError("activation contract changed its case, plan, target or owner")
        records = [
            ("DEBUG_CASE_CREATED", dict(target, observed_behavior=matches[0]["summary"],
                 reproduction=reproduction, required_evidence=["test_result"], plan_digest=state["plan_digest"],
                 target_digest=canonical_digest(target))),
            ("DEBUG_COUNTEREXAMPLE_LINKED", dict(counterexample_id=counterexample_id, verifier_evidence_ids=evidence_ids)),
            ("DEBUG_REPRODUCTION_FROZEN", dict(contract=contract)),
        ]
        projection, approved = deepcopy(state), []
        for offset, (kind, payload) in enumerate(records, 1):
            value = dict(payload, schema_version=2, case_id=case_id)
            if self.orchestrator.redactor.value(value) != value:
                raise DebuggerError("activation requires sanitized declarations and command authority")
            apply_debug_event(projection, {"type": kind, "payload": value,
                                          "seq": state["last_seq"] + offset, "actor_id": approved_by})
            approved.append((kind, approved_by, value))
        self.orchestrator._emit_many(self.run_id, approved, expected_seq=state["last_seq"])
        return self.inspect(case_id)

    def freeze_reproduction(self, case_id: str, contract: dict, *, approved_by: str) -> dict:
        return self._execution_commit("DEBUG_REPRODUCTION_FROZEN", case_id, approved_by, contract=contract)

    def authorize_execution(self, case_id: str, execution_id: str, source: dict, *,
                            approved_by: str, experiment_id: Optional[str] = None) -> dict:
        case = self.inspect(case_id)
        if not case.get("execution_contract"):
            raise DebuggerError("freeze a reviewed reproduction first")
        return self._execution_commit("DEBUG_EXECUTION_AUTHORIZED", case_id, approved_by,
                                      execution_id=execution_id, source=source, experiment_id=experiment_id,
                                      contract_digest=canonical_digest(case["execution_contract"]))

    async def execute(self, case_id: str, execution_id: str, artifacts: Any) -> list:
        from .debug_execution import execute
        return await execute(self, case_id, execution_id, artifacts)

    def collect_execution(self, case_id: str, execution_id: str, artifacts: Any) -> list:
        """Recover evidence from a completed receipt without executing again."""
        from .debug_execution import collect_execution
        return collect_execution(self, case_id, execution_id, artifacts)

    def reconcile_execution(self, case_id: str, execution_id: str, artifacts: Any, *, approved_by: str, resolution: str) -> dict:
        """Owner acknowledges an interrupted/unknown attempt; never infers success."""
        from .debug_execution import reconcile
        return reconcile(self, case_id, execution_id, artifacts, approved_by=approved_by, resolution=resolution)

    def inspect(self, case_id: Optional[str] = None) -> dict:
        cases = self._state()["debug_cases"]
        if case_id is None:
            return cases
        if case_id not in cases:
            raise DebuggerError("unknown debug case: " + case_id)
        return cases[case_id]

    def open(self, case_id: str, observed_behavior: str, target_behavior: str,
             reproduction: list, required_evidence: list, *, target_authority: str) -> dict:
        state = self._state()
        if not state.get("approved_by"):
            raise DebuggerError("approve the plan before declaring an authoritative debug target")
        target = self.orchestrator.redactor.value({
            "target_behavior": target_behavior, "target_authority": target_authority,
        })
        return self._commit("DEBUG_CASE_CREATED", case_id, observed_behavior=observed_behavior,
                            reproduction=reproduction, required_evidence=required_evidence,
                            plan_digest=state["plan_digest"], target_digest=canonical_digest(target), **target)

    def observe(self, case_id: str, kind: str, data: dict, *, producer: str,
                epistemic_status: str = "HUMAN_REPORTED", artifact_refs: tuple = ()) -> str:
        from uuid import uuid4
        state = self._state()
        if case_id not in state["debug_cases"]:
            raise DebuggerError("unknown debug case")
        record = EvidenceRecord(
            evidence_id=uuid4().hex, run_id=self.run_id, task_id=None, debug_case_id=case_id,
            agent_id=None, lease_id=None, fence_digest=None, kind=kind,
            epistemic_status="HUMAN_REPORTED" if epistemic_status == "HUMAN_REPORTED" else "UNVERIFIED",
            producer=producer, observed_at=self.orchestrator._now(),
            data=self.orchestrator.redactor.value(data), artifact_refs=artifact_refs,
        )
        self.orchestrator._emit(self.run_id, "EVIDENCE_RECORDED", record.to_dict(),
                                actor_id=producer, expected_seq=state["last_seq"])
        return record.evidence_id

    def reproduce(self, case_id: str, evidence_id: str) -> dict:
        return self._commit("DEBUG_REPRODUCED", case_id, evidence_id=evidence_id)

    def localize(self, case_id: str, chain: dict, evidence_ids: list) -> dict:
        return self._commit("DEBUG_LOCALIZED", case_id, chain=chain, evidence_ids=evidence_ids)

    def experiment(self, case_id: str, contract: dict) -> dict:
        return self._commit("DEBUG_EXPERIMENT_STARTED", case_id, experiment=contract)

    def finish_experiment(self, case_id: str, experiment_id: str, evidence_ids: list,
                          *, attempts: int, duration_seconds: int, cost_usd_micros: int) -> dict:
        return self._commit("DEBUG_EXPERIMENT_FINISHED", case_id, experiment_id=experiment_id,
                            evidence_ids=evidence_ids, attempts=attempts, duration_seconds=duration_seconds,
                            cost_usd_micros=cost_usd_micros)

    def verify(self, case_id: str, evidence_id: str, verdict: str) -> dict:
        return self._commit("DEBUG_VERIFIED", case_id, evidence_id=evidence_id, verdict=verdict)

    def contradict(self, case_id: str, evidence_id: str, reason: str) -> dict:
        return self._commit("DEBUG_CONTRADICTED", case_id, evidence_id=evidence_id, reason=reason)

    def block(self, case_id: str, reason: Any) -> dict:
        return self._commit("DEBUG_BLOCKED", case_id, reason=reason.to_dict())

    def resume(self, case_id: str, resolution: str) -> dict:
        return self._commit("DEBUG_RESUMED", case_id, resolution=resolution)

    def promote(self, case_id: str, eval_id: str, definition: dict) -> dict:
        return self._commit("DEBUG_EVAL_PROMOTED", case_id, eval_id=eval_id, definition=definition)
