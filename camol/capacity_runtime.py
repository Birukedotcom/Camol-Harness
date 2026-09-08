"""Schema-6 capacity receipts and the bridge to real task execution."""

from copy import deepcopy
from pathlib import Path

from .capacity import CapacityBroker, CapacityError, TARGET_RESOURCES, validate_request, validate_supply, _fields
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier


CAPACITY_EVENTS = frozenset({"GLOBAL_CAPACITY_RESERVED", "GLOBAL_CAPACITY_RENEWED", "GLOBAL_CAPACITY_RELEASED", "GLOBAL_CAPACITY_WAITING", "CAPACITY_CALL_RESERVED"})


def default_capacity_path():
    from .session import default_state_root
    return default_state_root() / "capacity.sqlite3"


def controller_identity(state_dir):
    return "controller-" + canonical_digest(str(Path(state_dir).resolve())).split(":")[1][:32]


def enabled(state):
    return state.get("runbook", {}).get("schema_version", 0) >= 6


def request_for(state, controller_id, task_id, agent_id, *, attempt=None):
    task, agent = state["tasks"][task_id], state["agents"][agent_id]
    resources = task["resource_requirements"]
    pools = agent["capacity_pools"]
    needs = [dict(pool_id=pools["target"], kind="target", resources={name: (1 if name == "slots" else resources[name]) for name in sorted(TARGET_RESOURCES)}),
             dict(pool_id=pools["runtime"], kind="runtime", resources={"concurrency": 1})]
    if pools["provider"]:
        needs.append(dict(pool_id=pools["provider"], kind="provider", resources={"concurrency": 1}))
    policy = state["runbook"]["run"]["capacity_policy"]
    return validate_request(dict(schema="camol.capacity_request", schema_version=1, namespace=policy["namespace"], controller_id=controller_id,
                                 run_id=state["run_id"], plan_digest=state["plan_digest"], task_id=task_id, agent_id=agent_id,
                                 attempt=attempt if attempt is not None else task["attempts"] + 1, policy=policy, needs=needs,
                                 capabilities=task["capabilities"], placement=resources["placement"]))


def validate_reservation(value):
    _fields(value, {"schema", "schema_version", "reservation_id", "request_id", "request", "local_reservation_id", "snapshots", "reserved_at", "expires_at"}, "global reservation")
    if value["schema"] != "camol.global_reservation" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CapacityError("unsupported global reservation")
    for name in ("reservation_id", "local_reservation_id"):
        require_identifier(value[name], name)
    request = validate_request(value["request"])
    if value["request_id"] != canonical_digest(request):
        raise CapacityError("global reservation request digest is invalid")
    at = parse_timestamp(value["reserved_at"], "reservation start")
    expiry = parse_timestamp(value["expires_at"], "reservation expiry")
    if at >= expiry or (expiry - at).total_seconds() > request["policy"]["reservation_ttl_seconds"]:
        raise CapacityError("global reservation lifetime exceeds its policy")
    if not isinstance(value["snapshots"], dict) or set(value["snapshots"]) != {need["pool_id"] for need in request["needs"]}:
        raise CapacityError("global reservation needs every exact pool snapshot")
    for need in request["needs"]:
        supply = validate_supply(value["snapshots"][need["pool_id"]])
        if (supply["pool_id"], supply["namespace"], supply["kind"]) != (need["pool_id"], request["namespace"], need["kind"]):
            raise CapacityError("global reservation has a foreign pool snapshot")
        if (supply["status"] != "ready" or not (parse_timestamp(supply["observed_at"], "supply start") <= at < expiry <= parse_timestamp(supply["expires_at"], "supply expiry"))
                or (supply["provenance"] == "owner_declared" and not request["policy"]["allow_owner_declared_supply"])):
            raise CapacityError("global reservation relies on stale/unready/untrusted supply")
        if any(amount > supply["limits"][name] - supply["outside_usage"][name] for name, amount in need["resources"].items()):
            raise CapacityError("global reservation exceeds observed usable capacity")
        if need["kind"] in {"target", "runtime"} and set(request["capabilities"]) - set(supply["capabilities"]):
            raise CapacityError("global reservation lacks required capabilities")
        if need["kind"] == "target" and any(supply["attributes"].get(key) != item for key, item in request["placement"].items()):
            raise CapacityError("global reservation violates placement")
        if need["kind"] == "provider" and (supply["rate_limit"] is None or supply["rate_limit"]["scope"] != request["policy"]["rate_scope"]):
            raise CapacityError("global reservation lacks the requested rate observation scope")
    return deepcopy(value)


def capacity_for_task(state, task_id, agent_id, *, now=None, local_reservation_id=None):
    if not enabled(state):
        return None
    task = state["tasks"][task_id]
    attempt = task["attempts"] if task["status"] in {"leased", "running", "verifying"} else task["attempts"] + 1
    matches = []
    for item in state.get("global_capacity", {}).values():
        receipt = item["reservation"]
        request = receipt["request"]
        if item["status"] == "active" and (request["task_id"], request["agent_id"], request["attempt"]) == (task_id, agent_id, attempt):
            if local_reservation_id is None or receipt["local_reservation_id"] == local_reservation_id:
                matches.append(receipt)
    if len(matches) != 1:
        raise CapacityError("task needs one exact shared global capacity reservation")
    receipt = matches[0]
    if now is not None and not (parse_timestamp(receipt["reserved_at"], "capacity start") <= parse_timestamp(now, "now") < parse_timestamp(receipt["expires_at"], "capacity expiry")):
        raise CapacityError("global capacity expired; execution must stop or reconcile")
    return receipt


def apply_capacity_event(state, event):
    if not enabled(state) or event.get("actor_id") != "capacity-broker":
        raise CapacityError("global capacity evidence requires schema6 and the broker control plane")
    kind, payload = event["type"], event["payload"]
    if kind in {"GLOBAL_CAPACITY_RESERVED", "GLOBAL_CAPACITY_RENEWED"}:
        _fields(payload, {"reservation"}, "capacity reservation event")
        receipt = validate_reservation(payload["reservation"])
        request = receipt["request"]
        if request["run_id"] != state["run_id"] or request["plan_digest"] != state["plan_digest"]:
            raise CapacityError("global capacity belongs to another run/plan")
        expected = request_for(state, request["controller_id"], request["task_id"], request["agent_id"], attempt=request["attempt"])
        if expected != request:
            raise CapacityError("global capacity requirements differ from the frozen plan")
        records = state.setdefault("global_capacity", {})
        prior = records.get(receipt["reservation_id"])
        if kind == "GLOBAL_CAPACITY_RENEWED":
            if not prior or prior["status"] != "active" or prior["reservation"]["request"] != request or prior["reservation"]["local_reservation_id"] != receipt["local_reservation_id"]:
                raise CapacityError("capacity renewal changed the owned reservation")
        elif prior:
            raise CapacityError("capacity reservation identity is already present")
        records[receipt["reservation_id"]] = {"reservation": receipt, "status": "active"}
        state.setdefault("capacity_waits", {}).pop(request["task_id"], None)
    elif kind == "GLOBAL_CAPACITY_RELEASED":
        _fields(payload, {"reservation_id", "reconciliation_digest"}, "capacity release")
        require_digest(payload["reconciliation_digest"], "capacity reconciliation")
        item = state.get("global_capacity", {}).get(payload["reservation_id"])
        if not item or item["status"] != "active":
            raise CapacityError("capacity release requires an active reservation")
        item.update(status="released", reconciliation_digest=payload["reconciliation_digest"])
    elif kind == "GLOBAL_CAPACITY_WAITING":
        _fields(payload, {"task_id", "decision"}, "capacity wait")
        if payload["task_id"] not in state["tasks"] or payload["decision"].get("status") not in {"waiting", "denied"}:
            raise CapacityError("invalid capacity waiting decision")
        state.setdefault("capacity_waits", {})[payload["task_id"]] = deepcopy(payload["decision"])
    elif kind == "CAPACITY_CALL_RESERVED":
        _fields(payload, {"task_id", "agent_id", "lease_id", "fence_digest", "turn_number", "call"}, "capacity call event")
        task = state["tasks"][payload["task_id"]]
        if (task["status"] != "running" or (task["agent_id"], task["lease_id"], task["fence_digest"]) != (payload["agent_id"], payload["lease_id"], payload["fence_digest"])
                or payload["turn_number"] != task["turn_count"] + 1):
            raise CapacityError("capacity call must bind the exact next worker turn")
        receipt = capacity_for_task(state, task["id"], task["agent_id"], now=event["occurred_at"])
        call = payload["call"]
        _fields(call, {"schema", "schema_version", "call_id", "reservation_id", "pool_id", "namespace", "max_requests", "max_tokens", "scope", "reserved_at", "expires_at", "supply_digest", "supply"}, "capacity call")
        if call["schema"] != "camol.capacity_call" or type(call["schema_version"]) is not int or call["schema_version"] != 1 or call["reservation_id"] != receipt["reservation_id"]:
            raise CapacityError("capacity call does not bind its reservation")
        if (call["scope"] != state["runbook"]["run"]["capacity_policy"]["rate_scope"] or type(call["max_requests"]) is not int or call["max_requests"] != 1
                or type(call["max_tokens"]) is not int or call["max_tokens"] != state["runbook"]["run"]["token_policy"]["max_tokens_per_turn"]):
            raise CapacityError("capacity call changed its frozen envelope")
        supply = validate_supply(call["supply"])
        provider = next((item["pool_id"] for item in receipt["request"]["needs"] if item["kind"] == "provider"), None)
        start, expiry, at = (parse_timestamp(call["reserved_at"], "call start"), parse_timestamp(call["expires_at"], "call expiry"), parse_timestamp(event["occurred_at"], "call event time"))
        if (provider is None or (call["pool_id"], call["namespace"]) != (provider, receipt["request"]["namespace"])
                or (supply["pool_id"], supply["namespace"], supply["kind"]) != (provider, call["namespace"], "provider")
                or canonical_digest(supply) != call["supply_digest"] or supply["status"] != "ready"
                or not parse_timestamp(supply["observed_at"], "supply start") <= start < parse_timestamp(supply["expires_at"], "supply expiry")
                or not start <= at < expiry or supply["rate_limit"] is None):
            raise CapacityError("capacity call has a foreign or stale provider observation")
        rate = supply["rate_limit"]
        if ((expiry - start).total_seconds() != rate["window_seconds"] or rate["scope"] != call["scope"]
                or call["max_requests"] > rate["max_requests"] or call["max_tokens"] > rate["max_tokens"]):
            raise CapacityError("capacity call exceeds its exact rolling-window envelope")
        identity = require_identifier(call["call_id"], "capacity call identity")
        calls = state.setdefault("capacity_calls", {})
        if identity in calls and calls[identity] != payload:
            raise CapacityError("capacity call identity has conflicting receipts")
        calls[identity] = deepcopy(payload)


class CapacityCoordinator:
    """Own shared reservations from the already-serialized runtime control plane."""

    def __init__(self, orchestrator, state_dir, *, broker=None):
        self.orchestrator = orchestrator
        self.controller_id = controller_identity(state_dir)
        self.broker = broker
        self.owns_broker = False

    def _broker(self):
        if self.broker is None:
            self.broker = CapacityBroker(default_capacity_path(), clock=self.orchestrator.clock)
            self.owns_broker = True
        return self.broker

    def close(self):
        if self.broker is not None and self.owns_broker:
            self.broker.close()
            self.broker = None

    def change_cursor(self):
        return self._broker().change_cursor()

    def _record(self, run_id, kind, payload):
        state = self.orchestrator.state(run_id)
        event = {"type": kind, "actor_id": "capacity-broker", "payload": payload, "occurred_at": self.orchestrator._now()}
        apply_capacity_event(deepcopy(state), event)
        self.orchestrator._emit(run_id, kind, payload, actor_id="capacity-broker", expected_seq=state["last_seq"])

    def _waiting(self, run_id, task_id, decision):
        state = self.orchestrator.state(run_id)
        if state.get("capacity_waits", {}).get(task_id) != decision:
            self._record(run_id, "GLOBAL_CAPACITY_WAITING", {"task_id": task_id, "decision": decision})

    def reserve_admission(self, run_id, bundle):
        state = self.orchestrator.state(run_id)
        if not enabled(state):
            return {"status": "granted", "reservation": None, "reasons": []}
        if not bundle.decision(now=self.orchestrator._now(), plan_digest=state["plan_digest"], plan_frozen=bool(state["approved_by"]), dependencies_green=True).ready:
            return {"status": "not_ready", "reservation": None, "reasons": []}
        request = request_for(state, self.controller_id, bundle.binding.task_id, bundle.binding.worker_id)
        if any(need["kind"] == "provider" for need in request["needs"]) and request["policy"]["rate_scope"] == "provider_request":
            decision = {"status": "denied", "reservation": None, "reasons": [{"reason": "PROVIDER_REQUEST_OBSERVATION_UNSUPPORTED"}]}
        else:
            decision = self._broker().reserve(request, local_reservation_id=bundle.reservation.reservation_id)
        if decision["status"] == "granted":
            receipt = decision["reservation"]
            existing = state.get("global_capacity", {}).get(receipt["reservation_id"])
            if existing is not None:
                if existing["status"] == "active" and existing["reservation"] == receipt:
                    return decision
                raise CapacityError("existing shared reservation must be reconciled, not overwritten")
            try:
                self._record(run_id, "GLOBAL_CAPACITY_RESERVED", {"reservation": receipt})
            except BaseException:
                self._broker().release(receipt["reservation_id"], controller_id=self.controller_id, run_id=run_id,
                                       reconciliation_digest=canonical_digest({"unpublished_admission": bundle.reservation.reservation_id}))
                raise
        else:
            self._waiting(run_id, bundle.binding.task_id, decision)
        return decision

    def before_turn(self, run_id, assignment, turn_number):
        state = self.orchestrator.state(run_id)
        if not enabled(state):
            return {"status": "granted", "call": None, "wake_at": None}
        receipt = capacity_for_task(state, assignment["task_id"], assignment["agent_id"], now=self.orchestrator._now())
        if not any(need["kind"] == "provider" for need in receipt["request"]["needs"]):
            return {"status": "granted", "call": None, "wake_at": None}
        call_id = "call-" + canonical_digest({"controller_id": self.controller_id, "run_id": run_id, "lease_id": assignment["lease_id"], "turn": turn_number}).split(":")[1]
        decision = self._broker().reserve_call(receipt["reservation_id"], call_id=call_id, max_requests=1,
                                             max_tokens=state["runbook"]["run"]["token_policy"]["max_tokens_per_turn"], scope=receipt["request"]["policy"]["rate_scope"])
        if decision["status"] == "granted":
            payload = {name: assignment[name] for name in ("task_id", "agent_id", "lease_id", "fence_digest")}
            payload.update(turn_number=turn_number, call=decision["call"])
            if call_id not in state.get("capacity_calls", {}):
                self._record(run_id, "CAPACITY_CALL_RESERVED", payload)
        else:
            self._waiting(run_id, assignment["task_id"], decision)
        return decision

    def renew_assignment(self, run_id, assignment, *, verification_reconciled=False):
        state = self.orchestrator.state(run_id)
        if not enabled(state):
            return
        receipt = capacity_for_task(state, assignment["task_id"], assignment["agent_id"])
        if verification_reconciled:
            if state["tasks"][assignment["task_id"]]["status"] != "verifying":
                raise CapacityError("only verification can restore stopped-process capacity")
            renewed = self._broker().restore_for_verification(receipt["reservation_id"], controller_id=self.controller_id, run_id=run_id,
                       reconciliation_digest=canonical_digest({"verification_only": assignment, "state_seq": state["last_seq"]}))
        else:
            renewed = self._broker().renew(receipt["reservation_id"], controller_id=self.controller_id, run_id=run_id)
        self._record(run_id, "GLOBAL_CAPACITY_RENEWED", {"reservation": renewed})

    def release_finished(self, run_id, *, processes_stopped):
        """Call only after the runner has reconciled or stopped owned processes."""
        state = self.orchestrator.state(run_id)
        if not enabled(state):
            return
        if processes_stopped is not True:
            raise CapacityError("capacity release needs an explicit process-stop reconciliation")
        for item in self._broker().reservations(namespace=state["runbook"]["run"]["capacity_policy"]["namespace"]):
            receipt = item["reservation"]
            request = receipt["request"]
            if item["status"] == "released" or (request["controller_id"], request["run_id"]) != (self.controller_id, run_id):
                continue
            task = state["tasks"].get(request["task_id"])
            if task and task["status"] in {"leased", "running", "verifying"} and task["attempts"] == request["attempt"]:
                continue
            local_id = receipt["local_reservation_id"]
            if task and task["attempts"] < request["attempt"] and local_id in state["reservations"] and local_id not in state["released_reservation_ids"]:
                continue
            proof = canonical_digest({"run_id": run_id, "sequence": state["last_seq"], "processes_stopped": True, "reservation_id": receipt["reservation_id"]})
            self._broker().release(receipt["reservation_id"], controller_id=self.controller_id, run_id=run_id, reconciliation_digest=proof)
            if state.get("global_capacity", {}).get(receipt["reservation_id"], {}).get("status") == "active":
                self._record(run_id, "GLOBAL_CAPACITY_RELEASED", {"reservation_id": receipt["reservation_id"], "reconciliation_digest": proof})
                state = self.orchestrator.state(run_id)
