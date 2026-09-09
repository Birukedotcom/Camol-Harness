"""Durable controller capture of delivered worker reports, never result acceptance."""

from copy import deepcopy

from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier, parse_timestamp
from .worker_delivery import DeliveryError, WorkerDelivery, authorize_state, key_redactor, _bytes, _fields, KINDS, ZERO, MAX_BATCH, MAX_RECORDS
from .worker_enrollment import _owner


EVENT = "WORKER_STREAM_IMPORTED"
MEANING = "captured_unverified_worker_reports_not_task_success_or_billing"
MAX_CAPTURE_BYTES = 8 << 20
MAX_CAPTURE_RECORD_BYTES = 36 << 10


def _stream(state, scope):
    require_digest(scope, "worker import scope")
    record = state.get("worker_streams", {}).get(scope)
    if record is None:
        raise DeliveryError("unknown worker import stream")
    return record


def _position(state, scope):
    return state.get("worker_imports", {}).get("streams", {}).get(scope, dict(cursor=0, digest=ZERO, records=[]))


def _capture(source, redactor):
    value = dict(seq=source["seq"], source_digest=source["digest"], previous=source["previous"],
        kind=source["kind"], occurred_at=source["occurred_at"],
        source_data_digest=canonical_digest(source["data"]), data=redactor.value(source["data"]))
    return dict(value, capture_digest=canonical_digest(value))


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise DeliveryError("worker import belongs to another run")
    value = event["payload"]
    fields = {"schema", "schema_version", "request_id", "scope", "proposal_digest", "limit",
              "base_cursor", "base_digest", "source_count", "records", "meaning"}
    if isinstance(value, dict) and value.get("schema_version") == 2:
        fields.add("gateway_policy_digest")
    _fields(value, fields)
    if value["schema"] != "camol.worker_import" or type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2}:
        raise DeliveryError("invalid worker import schema")
    require_identifier(value["request_id"], "worker import request")
    if value["meaning"] != MEANING or type(value["limit"]) is not int or not 1 <= value["limit"] <= MAX_BATCH:
        raise DeliveryError("invalid worker import meaning or limit")
    enrollment = _stream(state, value["scope"])
    if enrollment["status"] != "active" or value["proposal_digest"] != enrollment["proposal"]["digest"]:
        raise DeliveryError("worker import requires the current active enrollment")
    authorize_state(state, enrollment["proposal"]["stream"], now=event["occurred_at"])
    if value["schema_version"] == 2:
        require_digest(value["gateway_policy_digest"], "worker import gateway policy")
        gateway = state.get("worker_gateway", {})
        configured = gateway.get("configurations", {}).get(gateway.get("active_id"))
        if (configured is None or not configured["enabled"] or configured["policy_digest"] != value["gateway_policy_digest"]
                or value["scope"] not in configured["policy"]["scopes"]
                or parse_timestamp(configured["policy"]["expires_at"], "gateway expiry") <= parse_timestamp(event["occurred_at"], "gateway capture time")):
            raise DeliveryError("worker capture gateway authority is absent, changed or expired")
    imports = state.get("worker_imports", dict(count=0, bytes=0, streams={}, requests={}))
    if value["request_id"] in imports["requests"] or len(imports["requests"]) >= MAX_RECORDS:
        raise DeliveryError("worker import request was reused or history is full")
    previous = _position(state, value["scope"])
    if (type(value["base_cursor"]) is not int or value["base_cursor"] != previous["cursor"]
            or value["base_digest"] != previous["digest"]):
        raise DeliveryError("worker import cursor or predecessor changed")
    source_count = value["source_count"]
    if type(source_count) is not int or not previous["cursor"] <= source_count <= MAX_RECORDS:
        raise DeliveryError("worker source cursor rolled back or exceeds its bound")
    records = value["records"]
    if not isinstance(records, list) or len(records) != min(value["limit"], source_count - previous["cursor"]):
        raise DeliveryError("worker import is not the exact bounded next page")
    cursor, digest, size = previous["cursor"], previous["digest"], 0
    for row in records:
        _fields(row, {"seq", "source_digest", "previous", "kind", "occurred_at", "source_data_digest", "data", "capture_digest"})
        if type(row["seq"]) is not int or row["seq"] != cursor + 1 or row["previous"] != digest:
            raise DeliveryError("worker import source chain is discontinuous")
        if not isinstance(row["kind"], str) or row["kind"] not in KINDS or not isinstance(row["data"], dict):
            raise DeliveryError("worker import contains an invalid report")
        parse_timestamp(row["occurred_at"], "worker reported time")
        for field in ("source_digest", "source_data_digest", "capture_digest"):
            require_digest(row[field], field)
        if row["capture_digest"] != canonical_digest({key: val for key, val in row.items() if key != "capture_digest"}):
            raise DeliveryError("worker captured report changed")
        row_size = len(_bytes(row))
        if row_size > MAX_CAPTURE_RECORD_BYTES:
            raise DeliveryError("worker captured report exceeds its byte ceiling")
        size += row_size
        cursor, digest = row["seq"], row["source_digest"]
    if imports["count"] + len(records) > MAX_RECORDS or imports["bytes"] + size > MAX_CAPTURE_BYTES:
        raise DeliveryError("run worker capture allowance is exhausted; no records were dropped")
    # No task, gate, artifact, heartbeat, execution grant or usage record is made.
    # A worker's report of those things stays a report in this separate namespace.
    state["worker_imports"] = imports
    imports["count"] += len(records)
    imports["bytes"] += size
    imports["streams"][value["scope"]] = dict(cursor=cursor, digest=digest,
        records=previous["records"] + deepcopy(records))
    imports["requests"][value["request_id"]] = dict(scope=value["scope"], request_id=value["request_id"],
        limit=value["limit"], base_cursor=value["base_cursor"], cursor=cursor, imported_count=len(records),
        more=cursor < source_count, batch_digest=canonical_digest(value), kernel_seq=event["seq"],
        captured_at=event["occurred_at"], meaning=MEANING, verified=False, execution_authority=False)
    if value["schema_version"] == 2:
        imports["requests"][value["request_id"]]["gateway_policy_digest"] = value["gateway_policy_digest"]


def import_received(service, scope, *, by, request_id, limit=MAX_BATCH, gateway_policy_digest=None):
    """Capture one immutable owner request; retry returns its original receipt."""
    from .state import apply_event
    state = service.orchestrator.state(service.run_id)
    _owner(state, by)
    require_identifier(request_id, "worker import request")
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise DeliveryError("worker import limit must be 1..6")
    enrollment = _stream(state, scope)
    prior = state.get("worker_imports", {}).get("requests", {}).get(request_id)
    if prior is not None:
        if (prior["scope"] != scope or prior["limit"] != limit
                or (gateway_policy_digest is not None and gateway_policy_digest != prior.get("gateway_policy_digest"))):
            raise DeliveryError("worker import request is bound to different arguments")
        return deepcopy(prior)  # No new import, even after expiry/revocation/key loss.
    if enrollment["status"] != "active":
        raise DeliveryError("worker stream was revoked")
    authorize_state(state, enrollment["proposal"]["stream"], now=service.orchestrator._now())
    directory, key = service._material(enrollment["proposal"])
    if key_redactor(key).text(request_id) != request_id:
        raise DeliveryError("worker import request identity contains protected material")
    receiver = WorkerDelivery(directory / "receiver", enrollment["proposal"]["stream"], key, role="receiver")
    previous = _position(state, scope)
    page = receiver.inspect(after=previous["cursor"], limit=limit)
    rows = [_capture(row, key_redactor(key)) for row in page["records"]]
    value = dict(schema="camol.worker_import", schema_version=1, scope=scope, request_id=request_id,
        proposal_digest=enrollment["proposal"]["digest"], limit=limit, base_cursor=previous["cursor"],
        base_digest=previous["digest"], source_count=page["count"], records=rows, meaning=MEANING)
    if gateway_policy_digest is not None:
        value.update(schema_version=2, gateway_policy_digest=gateway_policy_digest)
    event = new_event(service.run_id, EVENT, by, value, occurred_at=service.orchestrator._now())
    expected = apply_event(state, dict(event, seq=state["last_seq"] + 1))
    # The source spool is immutable input. Cursor + captured records + request
    # receipt live in ONE kernel event, with an optimistic append against the
    # complete run sequence. A concurrent revoke/reassignment invalidates it.
    service.orchestrator.store.append(event, expected_seq=state["last_seq"])
    return deepcopy(expected["worker_imports"]["requests"][request_id])


def snapshot(state, scope, *, after=0, limit=100):
    _stream(state, scope)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise DeliveryError("invalid worker capture page")
    position = _position(state, scope)
    records = [row for row in position["records"] if row["seq"] > after][:limit]
    end = records[-1]["seq"] if records else after
    result = dict(schema="camol.worker_capture_snapshot", schema_version=1, scope=scope,
        run_id=state["run_id"], plan_digest=state["plan_digest"], kernel_seq=state["last_seq"],
        imported_cursor=position["cursor"], records=deepcopy(records), next_seq=end,
        more=end < position["cursor"], meaning=MEANING, verified=False, live_connection_proven=False,
        view_transform="current_credential_redaction; capture_digest_names_original_capture")
    result = Redactor().value(result)
    return dict(result, snapshot_digest=canonical_digest(result))
