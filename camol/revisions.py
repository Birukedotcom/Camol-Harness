"""Human-approved linked plan revisions with conservative re-verification.

The source ledger is sealed atomically with creation of its successor. Historical
evidence remains in that ledger; a revision reuses code, never an old green gate.
"""

from copy import deepcopy
from typing import Any, Dict, Optional
from uuid import uuid4
import re

from .events import new_event
from .effects import EffectRequest, EffectOutcome
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest, require_digest, require_identifier, require_string
from .usage import accounted_tokens, provider_cost_used, _trusted_receipts


class RevisionError(ValueError):
    """A revision lacks approval, safe quiescence, or reproducible lineage."""


REVISION_EVENTS = frozenset({"REVISION_PROPOSED", "RUN_SUPERSEDED", "REVISION_LINKED", "REVISION_EFFECT_REUSED"})
PROPOSAL_FIELDS = frozenset({"schema", "schema_version", "proposal_id", "source_run_id", "source_plan_digest", "source_state_digest",
                             "destination_run_id", "destination_plan_digest", "new_runbook", "reason", "mode", "impact",
                             "integration_head", "reusable_artifact_digests", "inherited_usage", "prior_effects", "effect_reruns", "proposal_digest"})


def execution_digest(state):
    return canonical_digest({key: value for key, value in state.items() if key not in {"last_seq", "revision_proposals"}})


def _impact(old, new):
    old_tasks = {item["id"]: item for item in old["tasks"]}
    new_tasks = {item["id"]: item for item in new["tasks"]}
    common = set(old_tasks) & set(new_tasks)
    changed = {identifier for identifier in common if old_tasks[identifier] != new_tasks[identifier]}
    added = set(new_tasks) - set(old_tasks)
    removed = set(old_tasks) - set(new_tasks)
    old_global = {key: value for key, value in old.items() if key != "tasks"}
    new_global = {key: value for key, value in new.items() if key != "tasks"}
    old_global["run"] = {key: value for key, value in old_global["run"].items() if key != "id"}
    new_global["run"] = {key: value for key, value in new_global["run"].items() if key != "id"}
    global_change = old_global != new_global
    invalidated = set(old_tasks) | set(new_tasks) if global_change else changed | added | removed
    while True:
        descendants = {identifier for identifier, item in {**old_tasks, **new_tasks}.items() if set(item["depends_on"]) & invalidated}
        extended = invalidated | descendants
        if extended == invalidated:
            break
        invalidated = extended
    return dict(changed_tasks=sorted(changed), added_tasks=sorted(added), removed_tasks=sorted(removed),
                invalidated_tasks=sorted(invalidated), unchanged_tasks=sorted(common - changed), global_contract_changed=global_change,
                resume_policy="all destination tasks restart pending and must pass new evaluator gates")


def _validate_proposal(value):
    if not isinstance(value, dict) or set(value) != PROPOSAL_FIELDS or value["schema"] != "camol.plan_revision" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RevisionError("revision proposal has missing, unknown, or unsupported fields")
    for name in ("proposal_id", "source_run_id", "destination_run_id"):
        require_identifier(value[name], "revision " + name)
    for name in ("source_plan_digest", "source_state_digest", "destination_plan_digest", "proposal_digest"):
        require_digest(value[name], "revision " + name)
    if value["mode"] != "reverify_all" or value["source_run_id"] == value["destination_run_id"]:
        raise RevisionError("revision requires a distinct successor and reverify_all mode")
    require_string(value["reason"], "revision reason")
    if value["integration_head"] is not None and (not isinstance(value["integration_head"], str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["integration_head"]) is None):
        raise RevisionError("revision integration head must be an exact git commit object ID")
    impact = value["impact"]
    if not isinstance(impact, dict) or set(impact) != {"changed_tasks", "added_tasks", "removed_tasks", "invalidated_tasks", "unchanged_tasks", "global_contract_changed", "resume_policy"}:
        raise RevisionError("revision impact has missing or unknown fields")
    for name in ("changed_tasks", "added_tasks", "removed_tasks", "invalidated_tasks", "unchanged_tasks"):
        _ordered_ids(impact[name], "impact " + name)
    if type(impact["global_contract_changed"]) is not bool or impact["resume_policy"] != "all destination tasks restart pending and must pass new evaluator gates":
        raise RevisionError("invalid revision impact policy")
    usage = value["inherited_usage"]
    if (not isinstance(usage, dict) or set(usage) != {"accounted_tokens", "provider_cost_usd_micros", "unknown_usage"}
            or any(type(usage[name]) is not int or usage[name] < 0 for name in ("accounted_tokens", "provider_cost_usd_micros"))
            or type(usage["unknown_usage"]) is not bool):
        raise RevisionError("revision usage must preserve non-negative integer token/cost charges")
    digests = value["reusable_artifact_digests"]
    if not isinstance(digests, list) or digests != sorted(set(digests)):
        raise RevisionError("reusable artifact digests must be unique and sorted")
    for digest in digests:
        require_digest(digest, "revision artifact")
    if not isinstance(value["prior_effects"], list):
        raise RevisionError("prior effects must be an array")
    seen_effects = set()
    for effect in value["prior_effects"]:
        _validate_prior_effect(effect)
        if effect["effect_id"] in seen_effects:
            raise RevisionError("prior effects contain duplicate identities")
        seen_effects.add(effect["effect_id"])
    document = validate_runbook(value["new_runbook"])
    if document["schema_version"] < 5 or document["run"]["id"] != value["destination_run_id"] or runbook_digest(document) != value["destination_plan_digest"] or document != value["new_runbook"]:
        raise RevisionError("revision does not bind an exact normalized V5 successor")
    _, policies = _effect_policy({"effects": {item["effect_id"]: item for item in value["prior_effects"]}}, document, value["effect_reruns"])
    if policies != value["effect_reruns"]:
        raise RevisionError("effect policies must be in canonical identity order")
    if canonical_digest({key: item for key, item in value.items() if key != "proposal_digest"}) != value["proposal_digest"]:
        raise RevisionError("revision proposal digest is invalid")
    return deepcopy(value)


def _ordered_ids(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or value != sorted(set(value)):
        raise RevisionError(name + " must be unique sorted identifiers")
    for item in value:
        require_identifier(item, name)


def _usage_snapshot(state):
    records = _trusted_receipts(state.get("evidence", {}).values())
    unknown = (state.get("revision") or {}).get("inherited_usage", {}).get("unknown_usage", False)
    return {"accounted_tokens": accounted_tokens(state), "provider_cost_usd_micros": provider_cost_used(state),
            "unknown_usage": unknown or any(item.total_tokens is None or item.cost_usd_micros is None for item in records.values())}


def _validate_prior_effect(effect):
    request_fields = set(EffectRequest.FIELDS)
    outcome_fields = {"outcome_digest", "readback_digest", "reconciled", "resolved_at"}
    if not isinstance(effect, dict) or set(effect) not in (request_fields, request_fields | outcome_fields):
        raise RevisionError("prior effect has missing or unknown fields")
    request = {name: effect[name] for name in EffectRequest.FIELDS}
    request["state"] = "EFFECT_REQUESTED"
    EffectRequest.from_dict(request)
    if set(effect) == request_fields:
        if effect["state"] != "EFFECT_REQUESTED":
            raise RevisionError("terminal prior effect lacks its outcome")
    else:
        EffectOutcome.from_dict(dict(schema=EffectOutcome.SCHEMA, schema_version=EffectOutcome.SCHEMA_VERSION,
                                     effect_id=effect["effect_id"], state=effect["state"],
                                     **{name: effect[name] for name in outcome_fields}))


def _quiescent(state):
    if state["approved_by"] is None or state["status"] in {"missing", "draft", "superseded"}:
        raise RevisionError("revision source must be an approved non-superseded run")
    if any(task.get("lease_id") or task.get("gate_wait") or task["status"] in {"leased", "running", "verifying"} for task in state["tasks"].values()):
        raise RevisionError("drain and fence every source task before applying a revision")
    if set(state.get("reservations", {})) - set(state.get("released_reservation_ids", ())):
        raise RevisionError("release all source reservations before migration")
    if any(item["status"] != "released" for item in state.get("global_capacity", {}).values()):
        raise RevisionError("reconcile and release shared capacity before migration")
    if any(effect["state"] in {"EFFECT_REQUESTED", "EFFECT_UNKNOWN"} for effect in state.get("effects", {}).values()):
        raise RevisionError("reconcile every uncertain external effect before migration")
    if any(watcher.get("status") not in {"completed", "stopped"} or watcher.get("active_poll") is not None for watcher in state.get("watchers", {}).values()):
        raise RevisionError("complete or explicitly stop source watchers before migration")
    for case in state.get("debug_cases", {}).values():
        if case.get("status") not in {"verified", "eval_promoted"} or any(item.get("status") in {"authorized", "running"} for item in case.get("executions", {}).values()):
            raise RevisionError("settle debug obligations and fence all debug executions before migration")
    accepted = {item["candidate_id"] for item in state.get("integrations", [])}
    if any(item.get("phase") == "integration" and item["candidate_id"] not in accepted for item in state.get("gate_assessments", {}).values()):
        raise RevisionError("settle provisional integrations before migration")


def _effect_policy(state, new_runbook, reruns):
    if not isinstance(reruns, list):
        raise RevisionError("effect_reruns must be an explicit array")
    destinations = {item["id"] for item in new_runbook["tasks"]}
    effects = list((state.get("revision") or {}).get("prior_effects", [])) + list(state.get("effects", {}).values())
    required = {effect["effect_id"]: effect for effect in effects if effect["task_id"] in destinations and effect["state"] == "EFFECT_CONFIRMED"}
    selected = {}
    for item in reruns:
        if not isinstance(item, dict) or set(item) != {"effect_id", "strategy", "request_digest", "readback_digest"}:
            raise RevisionError("effect rerun policy has missing or unknown fields")
        identifier = item["effect_id"]
        if identifier not in required or identifier in selected or item["strategy"] != "reuse_confirmed":
            raise RevisionError("effect reruns must name unique confirmed effects with reuse_confirmed strategy")
        previous = required[identifier]
        if not previous.get("readback_digest") or item["request_digest"] != previous["request_digest"] or item["readback_digest"] != previous["readback_digest"]:
            raise RevisionError("effect reuse needs the exact prior request and provider readback")
        selected[identifier] = item
    if set(selected) != set(required):
        raise RevisionError("confirmed effect tasks need explicit per-effect reuse approval; external replay is not automatic")
    return effects, [deepcopy(selected[key]) for key in sorted(selected)]


def apply_revision_event(state, event):
    payload = event["payload"]
    kind = event["type"]
    if kind == "REVISION_PROPOSED":
        proposal = _validate_proposal(payload)
        if proposal["source_run_id"] != state["run_id"] or proposal["source_plan_digest"] != state["plan_digest"] or proposal["source_state_digest"] != execution_digest(state):
            raise RevisionError("revision proposal does not bind its source snapshot")
        if proposal["impact"] != _impact(state["runbook"], proposal["new_runbook"]):
            raise RevisionError("revision impact is not reproducible")
        prior, policies = _effect_policy(state, proposal["new_runbook"], proposal["effect_reruns"])
        if proposal["prior_effects"] != prior or proposal["effect_reruns"] != policies or proposal["integration_head"] != state.get("integration_head"):
            raise RevisionError("revision artifact/effect lineage changed")
        artifacts = sorted({reference["digest"] for item in state["evidence"].values() for reference in item.get("artifact_refs", [])})
        artifacts = sorted(set(artifacts) | set((state.get("revision") or {}).get("reusable_artifact_digests", [])))
        if proposal["reusable_artifact_digests"] != artifacts:
            raise RevisionError("revision artifact provenance is not reproducible")
        expected_usage = _usage_snapshot(state)
        if proposal["inherited_usage"] != expected_usage:
            raise RevisionError("revision loses source usage accounting")
        state.setdefault("revision_proposals", {})[proposal["proposal_digest"]] = proposal
    elif kind == "RUN_SUPERSEDED":
        if set(payload) != {"proposal_digest", "destination_run_id", "approved_by"}:
            raise RevisionError("superseded event has missing or unknown fields")
        proposal = state.get("revision_proposals", {}).get(payload["proposal_digest"])
        _quiescent(state)
        if proposal is None or proposal["source_state_digest"] != execution_digest(state) or payload["destination_run_id"] != proposal["destination_run_id"]:
            raise RevisionError("revision source advanced; propose the migration again")
        if payload["approved_by"] != state["approved_by"] or payload["approved_by"] in state["agents"] or event["actor_id"] != payload["approved_by"]:
            raise RevisionError("revision requires the authorized human owner")
        state["status"] = "superseded"
        state["successor"] = deepcopy(payload)
        state["terminal"] = {"reason": "human-approved plan revision", **payload}
    elif kind == "REVISION_LINKED":
        if set(payload) != {"proposal", "source_ledger_digest", "approved_by"} or state["status"] != "draft" or state.get("revision"):
            raise RevisionError("revision lineage must be linked exactly once before successor approval")
        proposal = _validate_proposal(payload["proposal"])
        if any(effect["state"] in {"EFFECT_REQUESTED", "EFFECT_UNKNOWN"} for effect in proposal["prior_effects"]):
            raise RevisionError("successor cannot inherit unresolved external effects")
        require_digest(payload["source_ledger_digest"], "revision source ledger digest")
        if proposal["destination_run_id"] != state["run_id"] or proposal["destination_plan_digest"] != state["plan_digest"] or payload["approved_by"] in state["agents"] or payload["approved_by"] != event["actor_id"]:
            raise RevisionError("revision lineage does not bind this successor or its human owner")
        state["revision"] = dict(source_run_id=proposal["source_run_id"], source_plan_digest=proposal["source_plan_digest"],
                                 proposal_digest=proposal["proposal_digest"], source_ledger_digest=payload["source_ledger_digest"],
                                 impact=proposal["impact"], mode=proposal["mode"], inherited_usage=proposal["inherited_usage"],
                                 base_revision=proposal["integration_head"],
                                 reusable_artifact_digests=proposal["reusable_artifact_digests"], prior_effects=proposal["prior_effects"],
                                 effect_reruns=proposal["effect_reruns"], approved_by=payload["approved_by"])
        state["integration_head"] = proposal["integration_head"]
    elif kind == "REVISION_EFFECT_REUSED":
        if set(payload) != {"effect_id", "task_id", "lease_id", "fence_digest", "request_digest"}:
            raise RevisionError("effect reuse event has missing or unknown fields")
        task = state["tasks"][payload["task_id"]]
        if task["status"] != "running" or task["lease_id"] != payload["lease_id"] or task["fence_digest"] != payload["fence_digest"]:
            raise RevisionError("effect reuse does not bind a current lease")
        policy = next((item for item in state.get("revision", {}).get("effect_reruns", []) if item["effect_id"] == payload["effect_id"]), None)
        if policy is None or policy["request_digest"] != payload["request_digest"]:
            raise RevisionError("effect reuse was not approved in the plan revision")
        state.setdefault("revision_effect_reuses", []).append(deepcopy(payload))


class RevisionOrchestratorMixin:
    def propose_revision(self, source_run_id: str, new_runbook: Dict[str, Any], reason: str, *, effect_reruns: Optional[list] = None):
        state = self.state(source_run_id)
        if state["status"] in {"missing", "draft", "superseded"} or state["approved_by"] is None:
            raise RevisionError("revision source must be approved and not superseded")
        destination = validate_runbook(new_runbook)
        if destination["schema_version"] < 5 or destination["run"]["id"] == source_run_id:
            raise RevisionError("revision requires a distinct explicit V5 successor")
        if self.store.has_run(destination["run"]["id"]):
            raise RevisionError("revision destination already exists")
        require_string(reason, "revision reason")
        effects, policies = _effect_policy(state, destination, effect_reruns or [])
        artifacts = sorted({reference["digest"] for item in state["evidence"].values() for reference in item.get("artifact_refs", [])})
        artifacts = sorted(set(artifacts) | set((state.get("revision") or {}).get("reusable_artifact_digests", [])))
        proposal = dict(schema="camol.plan_revision", schema_version=1, proposal_id="revision-" + uuid4().hex,
                        source_run_id=source_run_id, source_plan_digest=state["plan_digest"], source_state_digest=execution_digest(state),
                        destination_run_id=destination["run"]["id"], destination_plan_digest=runbook_digest(destination), new_runbook=destination,
                        reason=self.redactor.text(reason), mode="reverify_all", impact=_impact(state["runbook"], destination),
                        integration_head=state.get("integration_head"), reusable_artifact_digests=artifacts,
                        inherited_usage=_usage_snapshot(state),
                        prior_effects=effects, effect_reruns=policies)
        proposal["proposal_digest"] = canonical_digest(proposal)
        if self.redactor.value(proposal) != proposal:
            raise RevisionError("revision metadata contains protected values")
        apply_revision_event(deepcopy(state), {"type": "REVISION_PROPOSED", "payload": proposal})
        self._emit(source_run_id, "REVISION_PROPOSED", proposal, expected_seq=state["last_seq"])
        return deepcopy(proposal)

    def apply_revision(self, source_run_id: str, proposal_digest: str, approved_by: str):
        from .state import apply_event, empty_state
        state = self.state(source_run_id)
        _quiescent(state)
        require_digest(proposal_digest, "revision proposal digest")
        proposal = state.get("revision_proposals", {}).get(proposal_digest)
        if proposal is None or proposal["source_state_digest"] != execution_digest(state):
            raise RevisionError("revision source advanced; propose the migration again")
        if approved_by != state["approved_by"] or approved_by in state["agents"] or approved_by in {agent["id"] for agent in proposal["new_runbook"]["agents"]}:
            raise RevisionError("revision requires the authorized human owner")
        destination_id = proposal["destination_run_id"]
        source_event = new_event(source_run_id, "RUN_SUPERSEDED", approved_by,
                                 dict(proposal_digest=proposal_digest, destination_run_id=destination_id, approved_by=approved_by), occurred_at=self._now())
        source_projection = apply_event(state, source_event)
        source_ledger = self.store.read(source_run_id) + [dict(source_event, seq=state["last_seq"] + 1)]
        destination_events = [
            new_event(destination_id, "RUN_CREATED", self.actor_id, {"runbook": proposal["new_runbook"], "plan_digest": proposal["destination_plan_digest"]}, occurred_at=self._now()),
            new_event(destination_id, "REVISION_LINKED", approved_by, {"proposal": proposal, "source_ledger_digest": canonical_digest(source_ledger), "approved_by": approved_by}, occurred_at=self._now()),
            new_event(destination_id, "PLAN_APPROVED", approved_by, {"approved_by": approved_by, "plan_digest": proposal["destination_plan_digest"]}, occurred_at=self._now()),
        ]
        destination_projection = empty_state()
        for event in destination_events:
            destination_projection = apply_event(destination_projection, event)
        self.store.append_transaction({source_run_id: [source_event], destination_id: destination_events},
                                      expected_sequences={source_run_id: state["last_seq"], destination_id: 0})
        return self.state(destination_id)


def prior_effect_reuse(state, task_id, *, provider, operation, target, request_digest, idempotency_key):
    """Return a confirmed prior result only for the exact human-reviewed reuse."""
    revision = state.get("revision") or {}
    all_prior = [item for item in revision.get("prior_effects", []) if item["state"] == "EFFECT_CONFIRMED"]
    prior = [item for item in all_prior if item["task_id"] == task_id]
    if not prior:
        if any(item["idempotency_key"] == idempotency_key or (item["provider"], item["operation"], item["target"]) == (provider, operation, target) for item in all_prior):
            raise RevisionError("renamed external-effect task cannot bypass prior effect identity")
        return None
    policies = {item["effect_id"] for item in revision.get("effect_reruns", [])}
    for item in prior:
        if item["effect_id"] in policies and all(item.get(name) == value for name, value in {
            "provider": provider, "operation": operation, "target": target,
            "request_digest": request_digest, "idempotency_key": idempotency_key,
        }.items()):
            return item
    raise RevisionError("revised external-effect task may only reuse its exact confirmed prior effect")


def collect_revision_lineage(store, run_id):
    """Collect exact source streams for a portable, self-contained revision export."""
    sources = {}
    current = run_id
    while True:
        events = store.read(current)
        linked = [event for event in events if event["type"] == "REVISION_LINKED"]
        if not linked:
            break
        if len(linked) != 1:
            raise RevisionError("revision has multiple parents")
        current = linked[0]["payload"]["proposal"]["source_run_id"]
        if current in sources or current == run_id:
            raise RevisionError("cyclic revision lineage")
        sources[current] = store.read(current)
    verify_revision_lineage(store.read(run_id), sources)
    return sources


def verify_revision_lineage(events, sources):
    """Replay and bind every ancestor; a bare source digest is not proof."""
    from .state import project
    if not isinstance(sources, dict):
        raise RevisionError("revision source streams must be a mapping")
    expected = set()
    current_events = events
    while True:
        current = project(current_events)
        linked = [event for event in current_events if event["type"] == "REVISION_LINKED"]
        if not linked:
            break
        if len(linked) != 1:
            raise RevisionError("revision has multiple parents")
        payload = linked[0]["payload"]
        proposal = payload["proposal"]
        source_id = proposal["source_run_id"]
        if source_id in expected or source_id == current["run_id"] or source_id not in sources:
            raise RevisionError("revision source stream is missing or cyclic")
        expected.add(source_id)
        prior_events = sources[source_id]
        if not isinstance(prior_events, list) or canonical_digest(prior_events) != payload["source_ledger_digest"]:
            raise RevisionError("revision source ledger digest is invalid")
        prior = project(prior_events)
        if (prior["run_id"] != source_id or prior["status"] != "superseded"
                or prior["plan_digest"] != proposal["source_plan_digest"]
                or prior["approved_by"] != payload["approved_by"]
                or prior.get("successor", {}).get("proposal_digest") != proposal["proposal_digest"]
                or prior.get("successor", {}).get("destination_run_id") != current["run_id"]):
            raise RevisionError("revision source does not authorize this successor")
        current_events = prior_events
    if set(sources) != expected:
        raise RevisionError("revision archive contains unrelated source streams")
