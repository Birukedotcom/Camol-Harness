"""Interactive mailbox commands, shared by the line client and terminal UI."""

import getpass
import json
from uuid import uuid4

from .mailbox_outbox import Outbox, OutboxError
from .probes import Redactor
from .runbook import runbook_digest
from .schema import canonical_digest


def binding(controller):
    session = controller.session
    plan = session.get("plan")
    if not session.get("run_id") or not plan or not plan.get("runbook"):
        raise OutboxError("mailbox requires an exact executable run")
    core = dict(session_id=session["session_id"], workspace=str(controller.workspace), state_dir=session["state_dir"],
                run_id=session["run_id"], product_plan_digest=session["plan_digest"], plan_digest=runbook_digest(plan["runbook"]))
    return dict(run_id=core["run_id"], plan_digest=core["plan_digest"]), canonical_digest(core)


def inbox_view(controller, box_id, offset=0):
    common, _ = binding(controller)
    result = controller._control("box-inbox", dict(common, box_id=box_id, offset=offset, limit=100))
    if (not isinstance(result, dict) or result.get("schema") != "camol.box_inbox" or type(result.get("schema_version")) is not int or result["schema_version"] != 1
            or not isinstance(result.get("messages"), list) or len(result["messages"]) > 100
            or any(result.get(key) != value for key, value in dict(common, box_id=box_id).items())):
        raise OutboxError("inbox response belongs to a different run, plan or box")
    return result


def _send(controller, outbox, intent):
    from .supervisor import SupervisorError
    common, scope = binding(controller)
    if intent["scope"] != scope or any(intent["params"][key] != value for key, value in common.items()) or intent["sender"] != getpass.getuser():
        raise OutboxError("saved request belongs to another session, plan, run or human sender; it will not be retargeted")
    identity = intent["params"]["request_id"]
    if controller._cancel_event.is_set() or controller._closed_event.is_set():
        return controller._respond("Message not dispatched. Saved request {}; inspect /outbox before an explicit retry.".format(identity))
    try:
        result = controller._control("box-message", intent["params"])
        receipt = outbox.accepted(intent, result)
    except (SupervisorError, OSError, ValueError) as error:
        return controller._respond("Message outcome unconfirmed for {}: {}. The exact request is retained; /message retry {} never retargets it.".format(identity, error, identity))
    return controller._respond("Message {} accepted for box {}. Acceptance is not delivery, consumption or task success. Request: {}. Use /inbox {} to inspect.".format(
        receipt["message_id"], intent["params"]["box_id"], identity, intent["params"]["box_id"]))


def command(controller, name, arguments):
    from .app import CommandResponse
    outbox = Outbox(controller.store.project_dir)
    if name == "/outbox":
        if len(arguments) > 1:
            raise OutboxError("usage: /outbox [REQUEST_ID]")
        records = [outbox.status(arguments[0])] if arguments else outbox.list()
        lines = ["OUTBOX — saved requests; no automatic retry or delivery claim"]
        for record in records:
            params = record["intent"]["params"]
            lines.append("{} | {} | run={} box={}".format(params["request_id"], record["status"], params["run_id"], params["box_id"]))
            if arguments:
                lines.append(json.dumps(record, indent=2, sort_keys=True))
        return CommandResponse(messages=("\n".join(lines + ([] if records else ["No saved requests."])),))
    common, scope = binding(controller)
    if name == "/inbox":
        from .supervisor import SupervisorError
        if len(arguments) > 2:
            raise OutboxError("usage: /inbox [BOX [OFFSET]]")
        box = arguments[0] if arguments else controller.session.get("selected_box")
        if not box:
            raise OutboxError("select a box or use /inbox BOX")
        if box not in {worker["id"] for worker in controller.session["plan"]["runbook"]["agents"]}:
            raise OutboxError("inbox requires an exact box ID, not a shortcut or prefix")
        offset = int(arguments[1]) if len(arguments) == 2 else 0
        try:
            result = inbox_view(controller, box, offset)
            rendered = "BOX {} / inbox — data messages, not approval or task success\n{}".format(box, json.dumps(result, indent=2, sort_keys=True))
        except (SupervisorError, OSError):
            if offset:
                raise OutboxError("offline inbox paging requires camol box read/export; no live page substituted")
            rendered = controller.inspect_box(box, "inbox")
        controller.session = controller.store.update(controller.session, selected_box=box)
        return CommandResponse(messages=(rendered,), box_id=box, box_view="inbox")
    if name == "/message" and arguments and arguments[0] == "retry":
        if len(arguments) != 2:
            raise OutboxError("usage: /message retry REQUEST_ID")
        intent = outbox.get(arguments[1])
        return _send(controller, outbox, intent)
    if len(arguments) < 2:
        raise OutboxError("usage: /message BOX TEXT or /reply MESSAGE_ID TEXT")
    correlation, expected_reply = None, None
    if name == "/reply":
        state, basis = controller._pane_state()
        record = state.get("box_messages", {}).get(arguments[0])
        if not record:
            raise OutboxError("reply requires an exact message in the selected run ledger")
        expected_reply = record["message"]["reply_to"]
        if expected_reply is None:
            raise OutboxError("this message has no worker reply address; use the orchestrator composer to answer a human")
        box, correlation = expected_reply["box_id"], record["message"]["correlation_id"]
    else:
        box = arguments[0]
    body = Redactor().text(" ".join(arguments[1:]))
    if not body.strip() or len(body) > 2000:
        raise OutboxError("message must contain 1..2000 characters")
    target = controller._control("box-observe", dict(common, box_id=box))
    if (not isinstance(target, dict) or not isinstance(target.get("subject"), dict)
            or any(target["subject"].get(key) != value for key, value in dict(common, box_id=box).items())):
        raise OutboxError("observed message target differs from the selected run/plan/box")
    if expected_reply is not None and target["subject"] != expected_reply:
        raise OutboxError("reply address was reassigned; no message sent to the replacement task")
    if controller._cancel_event.is_set() or controller._closed_event.is_set():
        raise OutboxError("message cancelled before saving or dispatching")
    identity = "ui-message-" + uuid4().hex
    params = dict(common, box_id=box, request_id=identity, target=target, body=body,
                  kind="information", correlation_id=correlation or identity, ttl_seconds=300)
    intent = outbox.put(scope, getpass.getuser(), params)
    return _send(controller, outbox, intent)
