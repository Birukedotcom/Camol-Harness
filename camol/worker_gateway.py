"""Owner-reviewed supervisor listener and bounded worker-report capture pump."""

import asyncio
import time
from copy import deepcopy
from pathlib import Path

from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier, parse_timestamp
from .worker_delivery import DeliveryError, WorkerDelivery, _fields, authorize_state
from .worker_enrollment import WorkerEnrollment, _owner
from .worker_tls import WorkerTLSServer, _address, _seconds, server_context


CONFIGURE = "WORKER_GATEWAY_CONFIGURED"
STOP = "WORKER_GATEWAY_STOPPED"
EVENTS = {CONFIGURE, STOP}


def policy(value):
    _fields(value, {"schema", "schema_version", "configuration_id", "run_id", "plan_digest", "scopes",
        "address", "port", "allow_non_loopback", "certificate_file", "private_key_file", "certificate_digest",
        "max_connections", "timeout_seconds", "poll_seconds", "streams_per_tick", "expires_at"})
    if value["schema"] != "camol.worker_gateway" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("invalid worker gateway schema")
    require_identifier(value["configuration_id"], "gateway configuration")
    require_identifier(value["run_id"], "gateway run")
    require_digest(value["plan_digest"], "gateway plan")
    require_digest(value["certificate_digest"], "gateway certificate")
    ip = _address(value["address"])
    if type(value["allow_non_loopback"]) is not bool or (not ip.is_loopback and not value["allow_non_loopback"]):
        raise DeliveryError("non-loopback gateway requires explicit policy opt-in")
    for name, maximum in (("port", 65535), ("max_connections", 64), ("streams_per_tick", 16)):
        if type(value[name]) is not int or not 1 <= value[name] <= maximum:
            raise DeliveryError("invalid worker gateway bound: " + name)
    _seconds(value["timeout_seconds"])
    if type(value["poll_seconds"]) is not int or not 1 <= value["poll_seconds"] <= 60:
        raise DeliveryError("gateway poll interval must be 1..60 seconds")
    for name in ("certificate_file", "private_key_file"):
        path = value[name]
        if not isinstance(path, str) or not 1 <= len(path) <= 4096 or not path.isprintable() or not Path(path).is_absolute():
            raise DeliveryError("gateway TLS files require absolute paths")
    scopes = value["scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 128:
        raise DeliveryError("gateway requires 1..128 explicit stream scopes")
    for scope in scopes:
        require_digest(scope, "gateway stream")
    if len(set(scopes)) != len(scopes):
        raise DeliveryError("gateway scope list contains duplicates")
    parse_timestamp(value["expires_at"], "gateway expiry")
    return deepcopy(value)


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise DeliveryError("gateway belongs to another run")
    existing = state.get("worker_gateway", dict(active_id=None, configurations={}))
    payload = event["payload"]
    if event["type"] == CONFIGURE:
        _fields(payload, {"policy", "approval_digest"})
        value = policy(payload["policy"])
        digest = canonical_digest(value)
        if payload["approval_digest"] != digest or (value["run_id"], value["plan_digest"]) != (state["run_id"], state["plan_digest"]):
            raise DeliveryError("gateway requires exact run/plan/policy approval")
        if existing["active_id"] is not None or value["configuration_id"] in existing["configurations"] or len(existing["configurations"]) >= 128:
            raise DeliveryError("stop the active gateway and use a new bounded configuration identity")
        if parse_timestamp(value["expires_at"], "gateway expiry") <= parse_timestamp(event["occurred_at"], "gateway configure time"):
            raise DeliveryError("gateway policy has expired")
        for scope in value["scopes"]:
            record = state.get("worker_streams", {}).get(scope)
            if record is None or record["status"] != "active":
                raise DeliveryError("gateway may only allowlist active owner-enrolled streams")
            authorize_state(state, record["proposal"]["stream"], now=event["occurred_at"])
        existing["configurations"][value["configuration_id"]] = dict(policy=value, policy_digest=digest,
            approved_by=event["actor_id"], configured_at=event["occurred_at"], stopped_at=None, enabled=True)
        existing["active_id"] = value["configuration_id"]
    else:
        _fields(payload, {"configuration_id", "policy_digest"})
        record = existing["configurations"].get(payload["configuration_id"])
        if record is None or not record["enabled"] or record["policy_digest"] != payload["policy_digest"]:
            raise DeliveryError("gateway stop requires the exact active configuration")
        if parse_timestamp(event["occurred_at"], "gateway stop time") < parse_timestamp(record["configured_at"], "gateway configure time"):
            raise DeliveryError("gateway stop predates configuration")
        record.update(enabled=False, stopped_at=event["occurred_at"])
        existing["active_id"] = None
    state["worker_gateway"] = existing


class WorkerGateway:
    def __init__(self, orchestrator, run_id, state_dir):
        self.orchestrator, self.run_id = orchestrator, run_id
        self.state_dir, self._streams = state_dir, None
        self.server = None
        self.loaded_digest = None
        self.position = 0
        self.next_poll = 0
        self.error = None
        self.capture_errors = {}

    @property
    def streams(self):
        if self._streams is None:
            self._streams = WorkerEnrollment(self.orchestrator, self.run_id, self.state_dir)
        return self._streams

    def _active(self):
        state = self.orchestrator.state(self.run_id)
        values = state.get("worker_gateway", {})
        record = values.get("configurations", {}).get(values.get("active_id"))
        if record is None or not record["enabled"] or state["status"] not in {"running"}:
            return state, None
        if parse_timestamp(record["policy"]["expires_at"], "gateway expiry") <= parse_timestamp(self.orchestrator._now(), "gateway time"):
            return state, None
        return state, record

    def configure(self, value, *, by, approval_digest):
        from .state import apply_event
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        value = policy(value)
        if approval_digest != canonical_digest(value):
            raise DeliveryError("approve the exact worker gateway policy digest")
        if Redactor().value(value) != value:
            raise DeliveryError("gateway policy contains protected credential material")
        old = state.get("worker_gateway", {}).get("configurations", {}).get(value["configuration_id"])
        if old is not None:
            if old["policy"] != value or not old["enabled"]:
                raise DeliveryError("gateway retry cannot change or revive a stopped configuration")
            return deepcopy(old)
        event = new_event(self.run_id, CONFIGURE, by, dict(policy=value, approval_digest=approval_digest), occurred_at=self.orchestrator._now())
        expected = apply_event(state, dict(event, seq=state["last_seq"] + 1))
        self.streams  # Validate private control storage before approval publication.
        _, pin = server_context(value["certificate_file"], value["private_key_file"])
        if pin != value["certificate_digest"]:
            raise DeliveryError("gateway certificate differs from the reviewed pin")
        self.orchestrator.store.append(event, expected_seq=state["last_seq"])
        self.next_poll = 0
        return deepcopy(expected["worker_gateway"]["configurations"][value["configuration_id"]])

    def stop(self, *, by, configuration_id, policy_digest):
        from .state import apply_event
        state = self.orchestrator.state(self.run_id)
        _owner(state, by)
        record = state.get("worker_gateway", {}).get("configurations", {}).get(configuration_id)
        if record is None or record["policy_digest"] != policy_digest:
            raise DeliveryError("unknown or changed gateway stop target")
        if not record["enabled"]:
            return deepcopy(record)
        event = new_event(self.run_id, STOP, by, dict(configuration_id=configuration_id, policy_digest=policy_digest), occurred_at=self.orchestrator._now())
        expected = apply_event(state, dict(event, seq=state["last_seq"] + 1))
        self.orchestrator.store.append(event, expected_seq=state["last_seq"])
        return deepcopy(expected["worker_gateway"]["configurations"][configuration_id])

    def receive(self, scope, raw):
        self._authorize(scope)
        return self.streams.receive(scope, raw, additional_guard=lambda: self._authorize(scope))

    def _authorize(self, scope):
        _, record = self._active()
        if record is None or record["policy_digest"] != self.loaded_digest or scope not in record["policy"]["scopes"]:
            raise DeliveryError("gateway policy does not authorize this stream")
        return True

    async def close(self):
        server, self.server = self.server, None
        self.loaded_digest = None
        if server is not None:
            await server.close()

    async def tick(self):
        _, record = self._active()
        if record is None:
            await self.close()
            self.capture_errors = {}
            return
        value, digest = record["policy"], record["policy_digest"]
        if self.server is None or self.loaded_digest != digest or not self.server.status()["listening"]:
            if (self.server is None or self.loaded_digest == digest) and time.monotonic() < self.next_poll:
                return
            await self.close()
            # Closing old TLS connections yields; never bind a policy replaced
            # or stopped during that await.
            _, fresh = self._active()
            if fresh is None or fresh["policy_digest"] != digest:
                return
            self.next_poll = time.monotonic() + value["poll_seconds"]
            # A public ledger restored without operational enrollment material
            # must not open a listener merely because its policy says enabled.
            ready = False
            for _ in range(min(value["streams_per_tick"], len(value["scopes"]))):
                scope = value["scopes"][self.position % len(value["scopes"])]
                self.position += 1
                try:
                    state = self.orchestrator.state(self.run_id)
                    enrolled = state["worker_streams"][scope]
                    if enrolled["status"] != "active":
                        raise DeliveryError("worker enrollment is not active")
                    authorize_state(state, enrolled["proposal"]["stream"], now=self.orchestrator._now())
                    directory, key = self.streams._material(enrolled["proposal"])
                    WorkerDelivery(directory / "receiver", enrolled["proposal"]["stream"], key, role="receiver")
                    ready = True
                    break
                except (OSError, ValueError, RuntimeError) as error:
                    self.capture_errors[scope] = type(error).__name__
            if not ready:
                self.error = "NO_CURRENT_ENROLLED_STREAM"
                return
            server = WorkerTLSServer(self, certificate=value["certificate_file"], private_key=value["private_key_file"],
                address=value["address"], port=value["port"], allow_non_loopback=value["allow_non_loopback"],
                max_connections=value["max_connections"], timeout=value["timeout_seconds"])
            if server.certificate_digest != value["certificate_digest"]:
                raise DeliveryError("gateway certificate differs from the reviewed pin")
            state, fresh = self._active()
            if fresh is None or fresh["policy_digest"] != digest:
                return
            enrolled = state["worker_streams"][scope]
            if enrolled["status"] != "active":
                raise DeliveryError("worker enrollment changed before listener bind")
            authorize_state(state, enrolled["proposal"]["stream"], now=self.orchestrator._now())
            await server.start()
            self.server, self.loaded_digest = server, digest
            self.position, self.next_poll, self.capture_errors = 0, 0, {}
        if time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + value["poll_seconds"]
        scopes = value["scopes"]
        for _ in range(min(value["streams_per_tick"], len(scopes))):
            scope = scopes[self.position % len(scopes)]
            self.position += 1
            try:
                state = self.orchestrator.state(self.run_id)
                enrolled = state["worker_streams"][scope]
                if enrolled["status"] != "active":
                    raise DeliveryError("worker enrollment is not active")
                authorize_state(state, enrolled["proposal"]["stream"], now=self.orchestrator._now())
                directory, key = self.streams._material(enrolled["proposal"])
                receiver = WorkerDelivery(directory / "receiver", enrolled["proposal"]["stream"], key, role="receiver")
                cursor = state.get("worker_imports", {}).get("streams", {}).get(scope, {}).get("cursor", 0)
                pending = receiver.inspect(after=cursor, limit=1)
                if pending["count"] < cursor:
                    raise DeliveryError("worker source cursor regressed")
                if pending["count"] > cursor:
                    request_id = "gateway-" + canonical_digest(dict(policy_digest=digest, scope=scope, cursor=cursor)).split(":")[1]
                    self.streams.import_received(scope, by=record["approved_by"], request_id=request_id, gateway_policy_digest=digest)
                self.capture_errors.pop(scope, None)
            except (OSError, ValueError, RuntimeError) as error:
                self.capture_errors[scope] = type(error).__name__
        self.error = None

    def status(self):
        state, eligible = self._active()
        configured = state.get("worker_gateway", {})
        record = configured.get("configurations", {}).get(configured.get("active_id"))
        return dict(configured_policy=deepcopy(record), policy_currently_eligible=eligible is not None,
            listener=self.server.status() if self.server else None,
            error=self.error, capture_errors=dict(self.capture_errors), execution_authority=False,
            meaning="evidence_listener_and_unverified_capture_not_worker_execution")
