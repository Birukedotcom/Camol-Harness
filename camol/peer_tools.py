"""Turn-scoped tools for trusted embedding adapters; not an authentication layer."""

from copy import deepcopy
import json

from .events import new_event
from .mailbox import Mailbox, MailboxError, fields, inbox, observe, subject
from .schema import canonical_digest, require_identifier


EVENT = "BOX_PEER_READ_RECORDED"
ACTOR = "box-peer-tools"


def _caller(state, caller, turn, now):
    if (not isinstance(caller, dict) or caller != subject(state, caller.get("box_id"), now)
            or type(turn) is not int or turn != state["tasks"][caller["task_id"]]["turn_count"] + 1):
        raise MailboxError("peer tools require the exact current worker turn and lease")


def _page(arguments, maximum):
    fields(arguments, {"offset", "limit"})
    if type(arguments["offset"]) is not int or arguments["offset"] < 0 or type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= maximum:
        raise MailboxError("peer page requires a nonnegative offset and bounded positive limit")
    return arguments["offset"], arguments["limit"]


def _read(state, caller, operation, arguments, now):
    if operation == "observe":
        fields(arguments, {"box_id"})
        return observe(state, arguments["box_id"], now)
    if operation == "inbox":
        _page(arguments, 10)
        return inbox(state, caller["box_id"], now, **arguments)
    if operation == "list":
        offset, limit = _page(arguments, 50)
        names = sorted(state["agents"])
        rows = []
        for name in names[offset:offset + limit]:
            worker = state["agents"][name]
            task = state["tasks"].get(worker.get("task_id"), {})
            rows.append(dict(box_id=name, task_id=worker.get("task_id"),
                             status=task.get("status", worker["status"])))
        return dict(schema="camol.peer_list", schema_version=1, run_id=state["run_id"],
                    plan_digest=state["plan_digest"], observed_cursor=state["last_seq"],
                    observed_at=now, boxes=rows, total=len(names), more=offset + limit < len(names))
    raise MailboxError("peer tools allow list, observe, own inbox and scoped messages only")


def _key(caller, turn, request_id):
    return canonical_digest(dict(subject=caller, turn_number=turn, request_id=request_id))


def apply(state, event):
    if event["actor_id"] != ACTOR or event["run_id"] != state["run_id"]:
        raise MailboxError("peer read must be recorded by the owning tool bridge")
    value, now = event["payload"], event["occurred_at"]
    fields(value, {"subject", "turn_number", "request_id", "operation", "arguments", "result", "digest"})
    _caller(state, value["subject"], value["turn_number"], now)
    require_identifier(value["request_id"], "peer request ID")
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise MailboxError("peer read digest changed")
    expected = _read(state, value["subject"], value["operation"], value["arguments"], now)
    if canonical_digest(value["result"]) != canonical_digest(expected) or len(json.dumps(value).encode()) > 65536:
        raise MailboxError("peer read differs from its exact ledger observation or exceeds 64 KiB")
    reads = state.setdefault("peer_tool_reads", {})
    key = _key(value["subject"], value["turn_number"], value["request_id"])
    count = sum(item["subject"] == value["subject"] and item["turn_number"] == value["turn_number"] for item in reads.values())
    if key in reads or count >= 64 or len(reads) >= 5000:
        raise MailboxError("duplicate peer observation or bounded tool-read allowance exhausted")
    reads[key] = deepcopy(value)


class PeerTools:
    """Owner-side adapter API. Native subprocesses never receive this object/token.

    Every successful read is replayable; retries return the original observation,
    not a newly fresh receipt. A fresh read needs a new request ID.
    """
    def __init__(self, orchestrator, run_id, assignment, turn_number, *, active=None):
        self.control, self.run_id = orchestrator, run_id
        self.assignment, self.turn = deepcopy(assignment), turn_number
        self.active = active
        self.closed = False
        state, now = self.control.state(run_id), self.control._now()
        self.caller = subject(state, assignment["agent_id"], now)
        if any(self.caller[key] != assignment[key] for key in ("task_id", "lease_id", "fence_digest")):
            raise MailboxError("peer tool assignment differs from the active worker lease")
        _caller(state, self.caller, self.turn, now)

    def close(self):
        self.closed = True

    def _check(self):
        if self.closed or (self.active is not None and not self.active()):
            raise MailboxError("peer tool turn is no longer owned or has closed")
        state, now = self.control.state(self.run_id), self.control._now()
        _caller(state, self.caller, self.turn, now)
        return state, now

    def call(self, operation, arguments, *, request_id):
        from .peer_telemetry import attempt
        return attempt(self, operation, arguments, request_id,
                       lambda: self._call(operation, arguments, request_id=request_id))

    def _call(self, operation, arguments, *, request_id):
        state, now = self._check()
        require_identifier(request_id, "peer request ID")
        key = _key(self.caller, self.turn, request_id)
        previous = state.get("peer_tool_reads", {}).get(key)
        if previous:
            if previous["operation"] != operation or canonical_digest(previous["arguments"]) != canonical_digest(arguments):
                raise MailboxError("peer request ID was already used for different content")
            return deepcopy(previous)
        result = _read(state, self.caller, operation, arguments, now)
        value = dict(subject=self.caller, turn_number=self.turn, request_id=request_id,
                     operation=operation, arguments=arguments, result=result)
        value["digest"] = canonical_digest(value)
        event = new_event(self.run_id, EVENT, ACTOR, value, occurred_at=now)
        apply(deepcopy(state), dict(event, seq=state["last_seq"] + 1))
        self.control.store.append(event, expected_seq=state["last_seq"])
        return deepcopy(value)

    def send(self, target, *, request_id, body, kind="information", correlation_id=None, ttl_seconds=300):
        from .peer_telemetry import attempt
        require_identifier(request_id, "peer send request ID")
        identity = "peer-" + _key(self.caller, self.turn, request_id).split(":")[1]
        args = dict(target=target, body=self.control.redactor.text(body) if isinstance(body, str) else body,
                    kind=kind, correlation_id=identity if correlation_id is None else correlation_id, ttl_seconds=ttl_seconds)
        return attempt(self, "send", args, request_id, lambda: self._send(request_id=request_id, **args))

    def _send(self, target, *, request_id, body, kind="information", correlation_id=None, ttl_seconds=300):
        state, now = self._check()
        require_identifier(request_id, "peer send request ID")
        if correlation_id is not None:
            require_identifier(correlation_id, "peer correlation ID")
        if not any(item["subject"] == self.caller and item["turn_number"] == self.turn
                   and item["operation"] == "observe" and item["result"] == target
                   for item in state.get("peer_tool_reads", {}).values()):
            raise MailboxError("peer send requires this turn's recorded target observation")
        # Prevent two workers' local retry IDs from colliding in the run mailbox.
        identity = "peer-" + _key(self.caller, self.turn, request_id).split(":")[1]
        return Mailbox(self.control, self.run_id).post(target, request_id=identity,
            body=body, sender=self.assignment["agent_id"], assignment=self.assignment,
            kind=kind, correlation_id=correlation_id, ttl_seconds=ttl_seconds)
