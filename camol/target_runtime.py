"""Measured local runtime reports bound to adopted identities, not launch grants."""

import os
import platform
import socket
import time
from copy import deepcopy
from datetime import timedelta

from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .targets import TargetError, _fields, _owner, _text


EVENT = "TARGET_RUNTIME_OBSERVED"
MAX_OBSERVATIONS = 1024


def validate_profile(value):
    _fields(value, {"schema", "schema_version", "kind", "hostname", "os", "architecture", "software", "digest"})
    if value["schema"] != "camol.local_target_profile" or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != "local":
        raise TargetError("invalid local target profile schema")
    for name in ("hostname", "os", "architecture"):
        _text(value[name], name)
    software = value["software"]
    _fields(software, {"camol_version", "python_executable", "python_sha256", "package_sha256", "control_version"})
    _text(software["camol_version"], "Camol version")
    path = software["python_executable"]
    if not isinstance(path, str) or not path.startswith("/") or not path.isprintable() or not 1 <= len(path) <= 4096:
        raise TargetError("invalid observed Python executable path")
    for name in ("python_sha256", "package_sha256"):
        require_digest(software[name], name)
    if type(software["control_version"]) is not int or software["control_version"] != 3:
        raise TargetError("unsupported observed local control protocol")
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise TargetError("local profile digest differs from its measured fields")
    return deepcopy(value)


def local_profile():
    # The shared file measurer bounds inputs and rejects changes during each read.
    # This is installed-software/host self-observation, not hardware attestation.
    from .ssh_bridge import bridge_identity
    from .ssh_protocol import SSHTransportError
    try:
        software = bridge_identity()
    except SSHTransportError as error:
        raise TargetError("local installed runtime identity could not be measured safely") from error
    result = dict(schema="camol.local_target_profile", schema_version=1, kind="local",
        hostname=socket.gethostname(), os=platform.system().lower(), architecture=platform.machine().lower(),
        software=software)
    result["digest"] = canonical_digest(result)
    validate_profile(result)
    if Redactor().value(result) != result:
        raise TargetError("local profile contains protected material; cannot publish a different review digest")
    return result


def _binding(state, generation, adoption_digest):
    require_identifier(generation, "target generation")
    require_digest(adoption_digest, "target adoption digest")
    record = state.get("execution_targets", {}).get(generation)
    if record is None or record["status"] != "adopted" or record["proposal"]["digest"] != adoption_digest:
        raise TargetError("runtime observation requires the exact active target adoption")
    if record["proposal"]["descriptor"]["transport"]["kind"] != "local":
        raise TargetError("local observations cannot stand in for remote targets")
    if state.get("terminal") is not None:
        raise TargetError("cannot record new runtime observations in a terminal run")
    return record


def _ttl(value):
    if type(value) is not int or not 1 <= value <= 300:
        raise TargetError("runtime observation TTL must be an integer 1..300 seconds")


def apply(state, event):
    _owner(state, event["actor_id"])
    value = event["payload"]
    _fields(value, {"schema", "schema_version", "request_id", "run_id", "plan_digest", "generation", "adoption_digest",
        "profile", "cpu_count", "started_at", "finished_at", "expires_at", "ttl_seconds", "elapsed_ms", "meaning", "digest"})
    if value["schema"] != "camol.target_runtime_observation" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise TargetError("invalid target runtime observation schema")
    if (value["run_id"], value["plan_digest"]) != (state["run_id"], state["plan_digest"]) or event["run_id"] != state["run_id"]:
        raise TargetError("runtime observation differs from run or plan")
    require_identifier(value["request_id"], "runtime observation request")
    if len(value["request_id"]) > 128:
        raise TargetError("runtime request identifier exceeds its bound")
    record = _binding(state, value["generation"], value["adoption_digest"])
    profile = validate_profile(value["profile"])
    if profile["digest"] != record["proposal"]["descriptor"]["transport"]["profile_digest"]:
        raise TargetError("observed runtime differs from the reviewed local target profile")
    cpu = value["cpu_count"]
    if cpu is not None and (type(cpu) is not int or not 1 <= cpu < 2**31):
        raise TargetError("invalid observed CPU count")
    _ttl(value["ttl_seconds"])
    started = parse_timestamp(value["started_at"], "runtime observation start")
    finished = parse_timestamp(value["finished_at"], "runtime observation finish")
    expiry = parse_timestamp(value["expires_at"], "runtime observation expiry")
    if (finished < started or expiry != finished + timedelta(seconds=value["ttl_seconds"])
            or value["finished_at"] != event["occurred_at"]
            or started < parse_timestamp(record["adopted_at"], "target adoption time")):
        raise TargetError("invalid runtime observation time interval")
    if type(value["elapsed_ms"]) is not int or not 0 <= value["elapsed_ms"] < 2**63:
        raise TargetError("invalid runtime observation duration")
    if value["meaning"] != "local_self_observation_not_attestation_readiness_or_reserved_capacity":
        raise TargetError("runtime observation cannot claim readiness or capacity")
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise TargetError("runtime observation digest is invalid")
    records = state.get("target_runtime_observations", {})
    if value["request_id"] in records or len(records) >= MAX_OBSERVATIONS:
        raise TargetError("runtime request reused or observation history full")
    records[value["request_id"]] = deepcopy(value)
    state["target_runtime_observations"] = records
    record["latest_runtime_observation"] = value["request_id"]


def prepare_local(registry, generation, *, adoption_digest, request_id, by, ttl_seconds=300):
    state = registry.orchestrator.state(registry.run_id)
    _owner(state, by)
    require_identifier(request_id, "runtime observation request")
    if len(request_id) > 128:
        raise TargetError("runtime request identifier exceeds its bound")
    _ttl(ttl_seconds)
    prior = state.get("target_runtime_observations", {}).get(request_id)
    if prior is not None:
        if (prior["generation"], prior["adoption_digest"], prior["ttl_seconds"]) != (generation, adoption_digest, ttl_seconds):
            raise TargetError("runtime request identity names a different observation")
        return dict(receipt=deepcopy(prior))  # Historical lookup does not refresh its time or expiry.
    _binding(state, generation, adoption_digest)
    if len(state.get("target_runtime_observations", {})) >= MAX_OBSERVATIONS:
        raise TargetError("runtime observation history is full")
    if Redactor().text(request_id) != request_id:
        raise TargetError("runtime request contains protected material")
    return dict(state=state, generation=generation, adoption_digest=adoption_digest, request_id=request_id,
                by=by, ttl_seconds=ttl_seconds, started_at=registry.orchestrator._now(), timer=time.monotonic_ns())


def finish_local(registry, prepared, profile):
    # Rebase unrelated run progress, but never a changed plan/owner/adoption.
    # The final synchronous append still compares the freshly validated cursor.
    state = registry.orchestrator.state(registry.run_id)
    _owner(state, prepared["by"])
    if (state["run_id"], state["plan_digest"]) != (prepared["state"]["run_id"], prepared["state"]["plan_digest"]):
        raise TargetError("runtime observation run or plan changed during measurement")
    generation, adoption_digest = prepared["generation"], prepared["adoption_digest"]
    prior = state.get("target_runtime_observations", {}).get(prepared["request_id"])
    if prior is not None:
        if (prior["generation"], prior["adoption_digest"], prior["ttl_seconds"]) != (generation, adoption_digest, prepared["ttl_seconds"]):
            raise TargetError("runtime request identity names a different observation")
        return deepcopy(prior)
    record = _binding(state, generation, adoption_digest)
    validate_profile(profile)
    if profile["digest"] != record["proposal"]["descriptor"]["transport"]["profile_digest"]:
        raise TargetError("runtime changed from the reviewed local profile; review a new adoption generation")
    cpu_count = os.cpu_count()
    finished_at = registry.orchestrator._now()
    value = dict(schema="camol.target_runtime_observation", schema_version=1, request_id=prepared["request_id"],
        run_id=state["run_id"], plan_digest=state["plan_digest"], generation=generation, adoption_digest=adoption_digest,
        profile=profile, cpu_count=cpu_count, started_at=prepared["started_at"], finished_at=finished_at,
        expires_at=(parse_timestamp(finished_at, "observation finish") + timedelta(seconds=prepared["ttl_seconds"])).isoformat(),
        ttl_seconds=prepared["ttl_seconds"], elapsed_ms=(time.monotonic_ns() - prepared["timer"]) // 1_000_000,
        meaning="local_self_observation_not_attestation_readiness_or_reserved_capacity")
    value["digest"] = canonical_digest(value)
    if Redactor().value(value) != value:
        raise TargetError("runtime report contains protected material")
    event = new_event(registry.run_id, EVENT, prepared["by"], value, occurred_at=finished_at)
    from .state import apply_event
    apply_event(state, dict(event, seq=state["last_seq"] + 1))
    registry.orchestrator.store.append(event, expected_seq=state["last_seq"])
    return deepcopy(value)


def observe_local(registry, generation, **kwargs):
    prepared = prepare_local(registry, generation, **kwargs)
    if "receipt" in prepared:
        return prepared["receipt"]
    return finish_local(registry, prepared, local_profile())
