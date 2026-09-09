"""Content-free attempt accounting; incomplete tool outcomes remain unknown."""

from copy import deepcopy
from datetime import datetime
import time
from uuid import uuid4

from .events import new_event
from .mailbox import MailboxError, fields
from .schema import canonical_digest, require_identifier, require_non_negative_int, require_digest


START = "BOX_PEER_CALL_STARTED"
FINISH = "BOX_PEER_CALL_FINISHED"
EVENTS = {START, FINISH}
ACTOR = "box-peer-tools"
OPERATIONS = {"list", "observe", "inbox", "send", "unsupported"}


def request_digest(operation, arguments):
    return canonical_digest(dict(operation=operation, arguments=arguments))


def reference(state, caller, turn, request_id, operation):
    from .peer_tools import _key
    key = _key(caller, turn, request_id)
    if operation in {"list", "observe", "inbox"} and key in state.get("peer_tool_reads", {}):
        return dict(kind="read", key=key)
    if operation == "send":
        identity = "message-" + canonical_digest(dict(run_id=state["run_id"], request_id="peer-" + key.split(":")[1])).split(":")[1]
        if identity in state.get("box_messages", {}):
            return dict(kind="message", key=identity)
    return None


def proof(state, start, ref):
    fields(ref, {"kind", "key"})
    expected = reference(state, start["subject"], start["turn_number"], start["request_id"], start["operation"])
    if ref != expected:
        raise MailboxError("tool completion has no matching recorded result")
    if ref["kind"] == "read":
        value = state["peer_tool_reads"][ref["key"]]
        if (value["subject"] != start["subject"] or value["turn_number"] != start["turn_number"]
                or value["request_id"] != start["request_id"] or value["operation"] != start["operation"]):
            raise MailboxError("tool completion has a foreign observation")
        digest = request_digest(value["operation"], value["arguments"])
    else:
        value = state["box_messages"][ref["key"]]["message"]
        if value["sender"]["kind"] != "worker" or value["sender"]["subject"] != start["subject"]:
            raise MailboxError("tool completion has a foreign message sender")
        args = {key: value[key] for key in ("target", "body", "kind", "correlation_id")}
        args["ttl_seconds"] = int((datetime.fromisoformat(value["expires_at"]) - datetime.fromisoformat(value["created_at"])).total_seconds())
        digest = request_digest("send", args)
    if digest != start["request_digest"]:
        raise MailboxError("tool completion differs from the started request")
    return canonical_digest(value)


def apply(state, event):
    from .peer_tools import _caller
    value, now = event["payload"], event["occurred_at"]
    if event["actor_id"] != ACTOR or event["run_id"] != state["run_id"]:
        raise MailboxError("tool attempt requires its owning bridge actor/run")
    if event["type"] == START:
        fields(value, {"call_id", "subject", "turn_number", "request_id", "operation", "request_digest", "prior_reference"})
        require_identifier(value["call_id"], "tool call ID")
        require_identifier(value["request_id"], "tool request ID")
        require_digest(value["request_digest"], "tool request digest")
        if not isinstance(value["operation"], str) or value["operation"] not in OPERATIONS:
            raise MailboxError("unsupported tool operation label")
        _caller(state, value["subject"], value["turn_number"], now)
        if value["prior_reference"] != reference(state, value["subject"], value["turn_number"], value["request_id"], value["operation"]):
            raise MailboxError("tool retry observation differs from its starting ledger cut")
        calls = state.setdefault("peer_tool_calls", {})
        count = sum(item["start"]["subject"] == value["subject"] and item["start"]["turn_number"] == value["turn_number"] for item in calls.values())
        if value["call_id"] in calls or count >= 128 or len(calls) >= 10000:
            raise MailboxError("duplicate call or bounded peer-call allowance exhausted")
        calls[value["call_id"]] = dict(start=deepcopy(value), started_at=now, finished_at=None, result=None)
    elif event["type"] == FINISH:
        fields(value, {"call_id", "outcome", "elapsed_ns", "reference", "result_digest", "error_kind", "reused"})
        require_identifier(value["call_id"], "tool call ID")
        require_non_negative_int(value["elapsed_ns"], "tool elapsed nanoseconds")
        if not isinstance(value["outcome"], str) or not isinstance(value["error_kind"], (str, type(None))):
            raise MailboxError("tool outcome and error kind must be scalar labels")
        record = state.get("peer_tool_calls", {}).get(value["call_id"])
        if not record or record["result"] is not None or datetime.fromisoformat(now) < datetime.fromisoformat(record["started_at"]):
            raise MailboxError("tool completion has no open ordered start")
        if type(value["reused"]) is not bool:
            raise MailboxError("tool reuse must be boolean")
        if value["outcome"] == "success":
            if (value["error_kind"] is not None or value["result_digest"] != proof(state, record["start"], value["reference"])
                    or value["reused"] != (record["start"]["prior_reference"] == value["reference"])):
                raise MailboxError("tool success/retry does not bind its recorded result")
        elif value["outcome"] in {"error", "interrupted"}:
            if (value["reference"] is not None or value["result_digest"] is not None or value["reused"]
                    or value["error_kind"] not in {"validation", "io", "interrupted", "other"}
                    or (value["outcome"] == "interrupted") != (value["error_kind"] == "interrupted")):
                raise MailboxError("failed tool call cannot claim a successful effect")
        else:
            raise MailboxError("unsupported tool outcome")
        record["result"], record["finished_at"] = deepcopy(value), now
    else:
        raise MailboxError("unsupported peer telemetry event")


def emit(control, run_id, kind, value):
    state = control.state(run_id)
    event = new_event(run_id, kind, ACTOR, value, occurred_at=control._now())
    apply(deepcopy(state), dict(event, seq=state["last_seq"] + 1))
    control.store.append(event, expected_seq=state["last_seq"])


def attempt(tools, operation, arguments, request_id, invoke):
    state, now = tools._check()
    require_identifier(request_id, "peer request ID")
    # Request prose stays outside this log. Send bodies have already passed the
    # mailbox redactor; read arguments are bounded metadata on successful calls.
    safe = dict(operation=operation, arguments=arguments)
    label = operation if isinstance(operation, str) and operation in OPERATIONS else "unsupported"
    start = dict(call_id="peer-call-" + uuid4().hex, subject=tools.caller, turn_number=tools.turn,
                 request_id=request_id, operation=label, request_digest=canonical_digest(safe),
                 prior_reference=reference(state, tools.caller, tools.turn, request_id, label))
    emit(tools.control, tools.run_id, START, start)
    began = time.perf_counter_ns()
    try:
        result = invoke()
    except BaseException as error:
        kind = "validation" if isinstance(error, ValueError) else "io" if isinstance(error, OSError) else "other" if isinstance(error, Exception) else "interrupted"
        end = dict(call_id=start["call_id"], outcome="interrupted" if kind == "interrupted" else "error",
                   elapsed_ns=time.perf_counter_ns() - began, reference=None, result_digest=None, error_kind=kind, reused=False)
        try:
            emit(tools.control, tools.run_id, FINISH, end)
        except Exception:
            # Preserve the original exception; the open start remains unknown.
            pass
        raise
    elapsed = time.perf_counter_ns() - began
    state = tools.control.state(tools.run_id)
    ref = reference(state, tools.caller, tools.turn, request_id, label)
    end = dict(call_id=start["call_id"], outcome="success", elapsed_ns=elapsed, reference=ref,
               result_digest=proof(state, start, ref), error_kind=None, reused=start["prior_reference"] == ref)
    emit(tools.control, tools.run_id, FINISH, end)
    return result


def profile(state):
    rows, tasks = {}, {}
    def accumulate(row, record):
        row["attempts"] += 1
        result = record["result"]
        if result is None:
            row["unknown"] += 1
        else:
            row[result["outcome"]] += 1
            row["reused"] += int(result["reused"])
            row["measured_calls"] += 1
            row["observed_elapsed_ns"] += result["elapsed_ns"]
    def blank():
        return dict(attempts=0, success=0, error=0, interrupted=0, unknown=0,
                    reused=0, measured_calls=0, observed_elapsed_ns=0)
    for record in state.get("peer_tool_calls", {}).values():
        operation = record["start"]["operation"]
        accumulate(rows.setdefault(operation, dict(operation=operation, **blank())), record)
        task = record["start"]["subject"]["task_id"]
        accumulate(tasks.setdefault(task, dict(task_id=task, **blank())), record)
    for row in list(rows.values()) + list(tasks.values()):
        if row["measured_calls"] == 0:
            row["observed_elapsed_ns"] = None
    return dict(operations=[rows[key] for key in sorted(rows)], by_task=[tasks[key] for key in sorted(tasks)], timing_basis="local_monotonic_operation_elapsed_not_cpu",
                unknown_duration_is_not_zero=True, model_usage_in_provider_receipts=True,
                failed_call_does_not_prove_no_message_effect=True)
