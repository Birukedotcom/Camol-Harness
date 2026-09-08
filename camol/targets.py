"""Run-scoped, owner-reviewed adoption identities; not execution or deletion grants."""

from copy import deepcopy

from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier, parse_timestamp


EVENTS = {"TARGET_ADOPTED", "TARGET_RETIRED"}
MAX_TARGETS = 1024


class TargetError(ValueError):
    pass


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise TargetError("target record has missing or unknown fields")


def _text(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise TargetError("invalid bounded target " + name)
    return value


def _owner(state, by):
    if not state.get("approved_by") or by != state["approved_by"] or by in state.get("agents", {}):
        raise TargetError("target changes require the exact approved human owner")


def descriptor(value):
    """Normalize an explicit review contract, never arbitrary provider JSON."""
    _fields(value, {"schema", "schema_version", "target_id", "generation", "control_plane_id",
                    "provider", "transport", "ownership", "label"})
    if value["schema"] != "camol.execution_target" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise TargetError("unsupported target descriptor schema")
    for name in ("target_id", "generation", "control_plane_id"):
        require_identifier(value[name], name)
        if len(value[name]) > 128:
            raise TargetError("target identifier exceeds its bound")
    _text(value["label"], "label")
    # Created infrastructure needs a provisioning receipt; adoption cannot invent one.
    if value["ownership"] != "adopted":
        raise TargetError("adoption cannot claim Camol created a target")
    provider = value["provider"]
    _fields(provider, {"kind", "account", "project", "location", "resource_id", "resource_name"})
    for name, item in provider.items():
        _text(item, "provider " + name)
    transport = value["transport"]
    _fields(transport, {"kind", "profile_digest"})
    if not isinstance(transport["kind"], str) or transport["kind"] not in {"local", "ssh", "worker_tls"}:
        raise TargetError("unsupported target transport kind")
    require_digest(transport["profile_digest"], "target transport profile")
    return deepcopy(value)


def _provider_identity(value):
    provider = value["provider"]
    # A GCP login/account label is reviewed access context, not a second resource
    # namespace inside the same project. Keep it in the approved descriptor but
    # do not let another credential label duplicate one machine in the registry.
    names = ("kind", "project", "location", "resource_id") if provider["kind"] == "gcp" else (
        "kind", "account", "project", "location", "resource_id")
    return tuple(provider[name] for name in names)


def proposal(state, value, *, expires_at):
    value = descriptor(value)
    parse_timestamp(expires_at, "target review expiry")
    result = dict(schema="camol.target_adoption", schema_version=1, run_id=state["run_id"],
                  plan_digest=state["plan_digest"], descriptor=value, expires_at=expires_at)
    result["digest"] = canonical_digest(result)
    return result


def _proposal(state, value):
    _fields(value, {"schema", "schema_version", "run_id", "plan_digest", "descriptor", "expires_at", "digest"})
    if type(value["schema_version"]) is not int or value != proposal(state, value["descriptor"], expires_at=value["expires_at"]):
        raise TargetError("target proposal must bind the exact run, plan and review digest")
    return deepcopy(value)


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise TargetError("target event belongs to another run")
    records = state.get("execution_targets", {})
    now = parse_timestamp(event["occurred_at"], "target event time")
    payload = event["payload"]
    if event["type"] == "TARGET_ADOPTED":
        _fields(payload, {"proposal", "approval_digest"})
        value = _proposal(state, payload["proposal"])
        target = value["descriptor"]
        if state.get("terminal") is not None:
            raise TargetError("cannot adopt targets into a terminal run")
        if payload["approval_digest"] != value["digest"] or now >= parse_timestamp(value["expires_at"], "target review expiry"):
            raise TargetError("target adoption requires an unexpired exact approval")
        if len(records) >= MAX_TARGETS or target["generation"] in records:
            raise TargetError("target generation reused or adoption history is full")
        for previous in records.values():
            other = previous["proposal"]["descriptor"]
            if other["control_plane_id"] != target["control_plane_id"]:
                raise TargetError("target adoption cannot change this run's control-plane identity")
            if previous["status"] == "adopted" and (other["target_id"] == target["target_id"] or _provider_identity(other) == _provider_identity(target)):
                raise TargetError("active target or provider resource already adopted")
        records[target["generation"]] = dict(proposal=value, status="adopted", approved_by=event["actor_id"],
            adopted_at=event["occurred_at"], retired_at=None, retirement_reason=None,
            readiness="unproven", execution_authority=False, deletion_authority=False)
    elif event["type"] == "TARGET_RETIRED":
        _fields(payload, {"generation", "adoption_digest", "reason"})
        require_identifier(payload["generation"], "target generation")
        _text(payload["reason"], "retirement reason")
        record = records.get(payload["generation"])
        if record is None or record["status"] != "adopted" or record["proposal"]["digest"] != payload["adoption_digest"]:
            raise TargetError("retirement requires the exact active adoption")
        if now < parse_timestamp(record["adopted_at"], "target adoption time"):
            raise TargetError("retirement predates adoption")
        target_id = record["proposal"]["descriptor"]["target_id"]
        # Expired but unreconciled leases still block retirement: expiry is not salvage.
        if any(task.get("status") in {"leased", "running", "verifying"} and
               (task.get("lease_fence") or {}).get("target_id") == target_id for task in state["tasks"].values()):
            raise TargetError("reconcile this run's target leases before retirement")
        record.update(status="retired", retired_at=event["occurred_at"], retirement_reason=payload["reason"])
    else:
        raise TargetError("unknown target event")
    state["execution_targets"] = records


def snapshot(state, *, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise TargetError("target inspection requires a nonnegative offset and limit 1..100")
    records = state.get("execution_targets", {})
    result = dict(schema="camol.target_snapshot", schema_version=1, run_id=state["run_id"],
        plan_digest=state["plan_digest"], cursor=state["last_seq"], count=len(records), offset=offset,
        targets=[deepcopy(records[key]) for key in sorted(records)[offset:offset + limit]],
        meaning="owner_reviewed_identity_not_host_attestation_or_execution_readiness")
    for target in result["targets"]:
        request_id = target.get("latest_runtime_observation")
        if request_id is not None:
            target["runtime_observation"] = deepcopy(state["target_runtime_observations"][request_id])
    # Inspection can redact newly protected values without rewriting historical approval.
    result = Redactor().value(result)
    result["snapshot_digest"] = canonical_digest(result)
    return result


class TargetRegistry:
    def __init__(self, orchestrator, run_id):
        self.orchestrator, self.run_id = orchestrator, run_id

    def propose(self, value, *, by, expires_at):
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        result = proposal(state, value, expires_at=expires_at)
        if Redactor().value(result) != result:
            raise TargetError("target review contract contains protected credential material")
        if parse_timestamp(expires_at, "target review expiry") <= parse_timestamp(self.orchestrator._now(), "target review time"):
            raise TargetError("target review has expired")
        return result  # Review only: no event, provider call or connection.

    def _append(self, state, kind, payload, by):
        from .state import apply_event
        event = new_event(self.run_id, kind, by, payload, occurred_at=self.orchestrator._now())
        expected = apply_event(state, dict(event, seq=state["last_seq"] + 1))
        self.orchestrator.store.append(event, expected_seq=state["last_seq"])
        return expected

    def adopt(self, value, *, by, approval_digest):
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        value = _proposal(state, value)
        if approval_digest != value["digest"]:
            raise TargetError("approve the exact target adoption digest")
        generation = value["descriptor"]["generation"]
        old = state.get("execution_targets", {}).get(generation)
        if old is not None:
            if old["proposal"] != value:
                raise TargetError("target generation is bound to another proposal")
            return deepcopy(old)  # Historical receipt, never revival or new authority.
        if Redactor().value(value) != value:
            raise TargetError("target review contract contains protected credential material")
        state = self._append(state, "TARGET_ADOPTED", dict(proposal=value, approval_digest=approval_digest), by)
        return deepcopy(state["execution_targets"][generation])

    def retire(self, generation, *, by, adoption_digest, reason):
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        require_identifier(generation, "target generation")
        _text(reason, "retirement reason")
        record = state.get("execution_targets", {}).get(generation)
        if record is None or record["proposal"]["digest"] != adoption_digest:
            raise TargetError("unknown or changed target retirement")
        if record["status"] == "retired":
            if record["retirement_reason"] != reason:
                raise TargetError("retirement retry changed its reason")
            return deepcopy(record)
        if Redactor().text(reason) != reason:
            raise TargetError("target retirement reason contains protected credential material")
        state = self._append(state, "TARGET_RETIRED", dict(generation=generation, adoption_digest=adoption_digest, reason=reason), by)
        return deepcopy(state["execution_targets"][generation])

    def inspect(self, *, offset=0, limit=50):
        return snapshot(self.orchestrator.state(self.run_id), offset=offset, limit=limit)

    def observe_local(self, generation, **kwargs):
        from .target_runtime import observe_local
        return observe_local(self, generation, **kwargs)
