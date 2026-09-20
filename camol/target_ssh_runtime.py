"""Owner-recorded remote supervisor runtime observations over pinned SSH.

This does not register an executor, attest provider ownership, or grant a lease.
"""

import time
from copy import deepcopy
from datetime import timedelta

from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .ssh_protocol import SSHTarget, SSHTransportError
from .ssh_transport import SSHControlClient
from .target_runtime import validate_profile, _ttl
from .targets import TargetError, _fields, _owner


EVENT = "TARGET_SSH_RUNTIME_OBSERVED"
MAX_OBSERVATIONS = 1024
COMMAND = "target-local-profile"
MEANING = "pinned_ssh_supervisor_self_observation_not_provider_attestation_readiness_or_capacity"


def _target(value):
    try:
        return SSHTarget.from_dict(value)
    except SSHTransportError as error:
        raise TargetError("invalid reviewed SSH runtime profile") from error


def _binding(state, generation, adoption_digest, target):
    require_identifier(generation, "target generation")
    require_digest(adoption_digest, "target adoption digest")
    record = state.get("execution_targets", {}).get(generation)
    if (state.get("terminal") is not None or record is None or record["status"] != "adopted"
            or record["proposal"]["digest"] != adoption_digest):
        raise TargetError("SSH measurement requires the exact active target adoption")
    if record["proposal"]["descriptor"]["transport"] != dict(kind="ssh", profile_digest=target.digest()):
        raise TargetError("SSH profile differs from the reviewed target transport")
    if COMMAND not in target.allowed_commands:
        raise TargetError("review explicit target-local-profile permission on both SSH endpoints")
    return record


def _request_id(value):
    require_identifier(value, "SSH runtime observation request")
    if len(value) > 128:
        raise TargetError("SSH runtime request identifier exceeds its bound")


def _response_digest(target, request_id, profile):
    return canonical_digest(dict(schema="camol.ssh_response", schema_version=1,
        request_id=request_id, target_id=target.target_id, target_digest=target.target_digest,
        run_id=target.run_id, plan_digest=target.plan_digest, outcome="completed",
        response=dict(ok=True, result=profile)))


def apply(state, event):
    _owner(state, event["actor_id"])
    value = event["payload"]
    _fields(value, {"schema", "schema_version", "request_id", "run_id", "plan_digest", "generation", "adoption_digest",
        "ssh_profile", "profile", "transport_request_id", "response_digest", "started_at", "finished_at", "expires_at",
        "ttl_seconds", "elapsed_ms", "meaning", "digest"})
    if (value["schema"] != "camol.target_ssh_runtime_observation" or type(value["schema_version"]) is not int
            or value["schema_version"] != 1):
        raise TargetError("invalid SSH runtime observation schema")
    if (value["run_id"], value["plan_digest"], event["run_id"]) != (state["run_id"], state["plan_digest"], state["run_id"]):
        raise TargetError("SSH runtime observation differs from run or plan")
    _request_id(value["request_id"])
    _request_id(value["transport_request_id"])
    target = _target(value["ssh_profile"])
    record = _binding(state, value["generation"], value["adoption_digest"], target)
    profile = validate_profile(value["profile"])
    if profile["software"] != dict(target.bridge_identity):
        raise TargetError("remote supervisor runtime differs from the reviewed bridge runtime")
    if value["response_digest"] != _response_digest(target, value["transport_request_id"], profile):
        raise TargetError("SSH observation differs from its bound transport response")
    _ttl(value["ttl_seconds"])
    started = parse_timestamp(value["started_at"], "SSH observation start")
    finished = parse_timestamp(value["finished_at"], "SSH observation finish")
    if (started < parse_timestamp(record["adopted_at"], "target adoption") or finished < started
            or value["finished_at"] != event["occurred_at"]
            or parse_timestamp(value["expires_at"], "SSH observation expiry") != finished + timedelta(seconds=value["ttl_seconds"])):
        raise TargetError("invalid SSH observation interval")
    if type(value["elapsed_ms"]) is not int or not 0 <= value["elapsed_ms"] < 2**63 or value["meaning"] != MEANING:
        raise TargetError("invalid SSH observation duration or authority claim")
    if value["digest"] != canonical_digest({k: v for k, v in value.items() if k != "digest"}):
        raise TargetError("invalid SSH observation digest")
    records = state.setdefault("target_ssh_runtime_observations", {})
    if value["request_id"] in records or len(records) >= MAX_OBSERVATIONS:
        raise TargetError("SSH runtime request reused or observation history full")
    records[value["request_id"]] = deepcopy(value)
    record["latest_ssh_runtime_observation"] = value["request_id"]


async def observe_ssh(registry, generation, *, adoption_digest, request_id, by, ssh_profile,
                      state_dir, ttl_seconds=300, allow_network=False, before_publish=None):
    state = registry.orchestrator.state(registry.run_id)
    _owner(state, by)
    _request_id(request_id)
    _ttl(ttl_seconds)
    target = _target(ssh_profile)
    if Redactor().value(dict(profile=target.to_dict(), request_id=request_id)) != dict(profile=target.to_dict(), request_id=request_id):
        raise TargetError("SSH observation review contains protected material")
    prior = state.get("target_ssh_runtime_observations", {}).get(request_id)
    if prior is not None:
        if (prior["generation"], prior["adoption_digest"], prior["ttl_seconds"], prior["ssh_profile"]) != (
                generation, adoption_digest, ttl_seconds, target.to_dict()):
            raise TargetError("SSH runtime request names another observation")
        return deepcopy(prior)  # Historical read never refreshes or dispatches.
    _binding(state, generation, adoption_digest, target)
    if len(state.get("target_ssh_runtime_observations", {})) >= MAX_OBSERVATIONS:
        raise TargetError("SSH runtime observation history is full")
    if allow_network is not True:
        raise TargetError("SSH runtime measurement requires explicit network approval")
    # Per-registry guard; the live supervisor also guards across control requests.
    if getattr(registry, "_ssh_runtime_busy", False):
        raise TargetError("an SSH runtime observation is already in progress")
    registry._ssh_runtime_busy = True
    try:
        started_at, timer = registry.orchestrator._now(), time.monotonic_ns()
        try:
            client = SSHControlClient(target, state_dir=state_dir, timeout=20)
            response = await client.request(COMMAND, params={}, requested_by=target.owner)
        except SSHTransportError as error:
            raise TargetError("SSH runtime observation unavailable; no report recorded ({})".format(error.code)) from error
        if not response.get("ok"):
            raise TargetError("remote runtime observation was rejected")
        profile = validate_profile(response["result"])
        transport = response["transport"]
        if (transport["status"] != "completed" or transport["request_id"] != response["request_id"]
                or transport["target_digest"] != target.digest()
                or transport["response_digest"] != _response_digest(target, response["request_id"], profile)):
            raise TargetError("runtime response lacks its exact successful transport receipt")
        if before_publish is not None:
            before_publish()
        current = registry.orchestrator.state(registry.run_id)
        _owner(current, by)
        if (current["run_id"], current["plan_digest"]) != (state["run_id"], state["plan_digest"]):
            raise TargetError("run or plan changed during SSH runtime observation")
        _binding(current, generation, adoption_digest, target)
        finished_at = registry.orchestrator._now()
        value = dict(schema="camol.target_ssh_runtime_observation", schema_version=1,
            run_id=current["run_id"], plan_digest=current["plan_digest"], request_id=request_id,
            generation=generation, adoption_digest=adoption_digest, ssh_profile=target.to_dict(), profile=profile,
            transport_request_id=response["request_id"], response_digest=transport["response_digest"],
            started_at=started_at, finished_at=finished_at,
            expires_at=(parse_timestamp(finished_at, "SSH observation finish") + timedelta(seconds=ttl_seconds)).isoformat(),
            ttl_seconds=ttl_seconds, elapsed_ms=(time.monotonic_ns() - timer) // 1000000, meaning=MEANING)
        value["digest"] = canonical_digest(value)
        if Redactor().value(value) != value:
            raise TargetError("SSH observation contains protected material")
        event = new_event(registry.run_id, EVENT, by, value, occurred_at=finished_at)
        from .state import apply_event
        apply_event(current, dict(event, seq=current["last_seq"] + 1))
        registry.orchestrator.store.append(event, expected_seq=current["last_seq"])
        return deepcopy(value)
    finally:
        registry._ssh_runtime_busy = False
