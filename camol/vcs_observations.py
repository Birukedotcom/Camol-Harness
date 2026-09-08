"""Owner-side read-only VCS observations with durable intent and honest gaps."""

import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from . import github_vcs
from .evaluation import CandidateRecord, IntegrationReceipt
from .schema import canonical_digest, require_digest, require_identifier, require_timestamp, parse_timestamp
from .vcs import VCSError, _owner


EVENTS = frozenset({"VCS_OBSERVATION_STARTED", "VCS_OBSERVATION_FINISHED"})
MAX_OBSERVATIONS = 1000


def binding(state, candidate_id, integration_id):
    require_identifier(candidate_id, "VCS candidate ID")
    require_identifier(integration_id, "VCS integration ID")
    candidate = state.get("candidates", {}).get(candidate_id)
    if candidate is None:
        raise VCSError("VCS observation requires an exact captured candidate")
    candidate = CandidateRecord.from_dict(candidate)
    matched = [row for row in state.get("integrations", []) if row["integration_id"] == integration_id]
    if len(matched) != 1:
        raise VCSError("VCS observation requires an exact accepted integration ID")
    receipt = IntegrationReceipt.from_dict(matched[0])
    if (receipt.run_id != state["run_id"] or receipt.candidate_id != candidate_id
            or receipt.task_id != candidate.task_id or receipt.evaluator_digest != candidate.evaluator_digest):
        raise VCSError("integration does not bind the requested candidate")
    return dict(run_id=state["run_id"], plan_digest=state["plan_digest"], candidate_id=candidate_id,
        candidate_digest=candidate.digest(), integration_id=integration_id, integration_digest=receipt.digest(),
        repository_id=receipt.workspace.repository_id, revision=github_vcs.sha(receipt.revision))


def _request(state, *, candidate_id, integration_id, target, request_id, timeout, transport):
    require_identifier(request_id, "VCS request ID")
    if len(request_id) > 128 or transport not in {"bounded_github_https_v1", "owner_embedding_callback"}:
        raise VCSError("invalid VCS request identity/transport")
    subject = binding(state, candidate_id, integration_id)
    policy = github_vcs.limits(timeout)
    policy["deadline_enforcement"] = "owned_process" if transport == "bounded_github_https_v1" else "embedding_owner"
    target = github_vcs.target(target)
    return dict(schema="camol.vcs_observation_request", schema_version=1, request_id=request_id,
        binding=subject, target=target, limits=policy, transport=transport,
        api_version=github_vcs.API_VERSION, planned_http_requests=len(github_vcs.requests(target, subject["revision"])),
        allow_network=True, approved_owner=state["approved_by"], execution_authority=False)


def _validate_start(state, request):
    try:
        expected = _request(state, candidate_id=request["binding"]["candidate_id"], integration_id=request["binding"]["integration_id"],
            target=request["target"], request_id=request["request_id"], timeout=request["limits"]["timeout_seconds"], transport=request["transport"])
        require_timestamp(request["started_at"], "VCS observation start")
        expected["started_at"] = request["started_at"]
        expected["digest"] = canonical_digest(expected)
        if canonical_digest(request) != canonical_digest(expected) or type(request["schema_version"]) is not int or type(request["allow_network"]) is not bool:
            raise VCSError("VCS observation intent fields or digest are invalid")
        return expected
    except (KeyError, TypeError) as error:
        raise VCSError("VCS observation intent lacks required fields") from error


def _validate_finish(request, receipt):
    fields = {"schema", "schema_version", "request_id", "request_digest", "status", "finished_at",
              "elapsed_ms", "clock_regressed", "result", "error_code", "digest"}
    if (not isinstance(receipt, dict) or set(receipt) != fields or receipt["schema"] != "camol.vcs_observation_receipt"
            or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["request_id"] != request["request_id"] or receipt["request_digest"] != request["digest"]
            or not isinstance(receipt["status"], str)):
        raise VCSError("VCS observation receipt does not bind the exact intent")
    require_timestamp(receipt["finished_at"], "VCS observation finish")
    if (type(receipt["elapsed_ms"]) is not int or not 0 <= receipt["elapsed_ms"] < 2**63
            or type(receipt["clock_regressed"]) is not bool
            or receipt["clock_regressed"] != (parse_timestamp(receipt["finished_at"], "finish") < parse_timestamp(request["started_at"], "start"))):
        raise VCSError("invalid VCS observation measurement")
    if receipt["status"] == "observed":
        if receipt["error_code"] is not None:
            raise VCSError("an observed VCS receipt cannot carry a transport failure")
        github_vcs.validate_result(receipt["result"], request["target"], request["binding"]["revision"])
    elif receipt["status"] in {"unavailable", "cancelled"}:
        if receipt["result"] is not None or receipt["error_code"] != "OBSERVATION_UNAVAILABLE":
            raise VCSError("unavailable VCS observations cannot claim readback")
    else:
        raise VCSError("unknown VCS observation terminal state")
    if receipt["digest"] != canonical_digest({key: value for key, value in receipt.items() if key != "digest"}):
        raise VCSError("VCS receipt digest is invalid")
    return deepcopy(receipt)


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise VCSError("VCS observation event belongs to another run")
    records = state.get("vcs_observations", {})
    value = event["payload"]
    if event["type"] == "VCS_OBSERVATION_STARTED":
        request = _validate_start(state, value)
        if request["request_id"] in records or len(records) >= MAX_OBSERVATIONS:
            raise VCSError("duplicate VCS observation request or history ceiling")
        state.setdefault("vcs_observations", {})[request["request_id"]] = dict(
            request=deepcopy(request), start_seq=event.get("seq", state["last_seq"] + 1), receipt=None)
    else:
        if not isinstance(value, dict) or value.get("request_id") not in records:
            raise VCSError("VCS receipt has no matching prior intent")
        record = records[value["request_id"]]
        if record["receipt"] is not None:
            raise VCSError("VCS observation already has a terminal receipt")
        record["receipt"] = _validate_finish(record["request"], value)


class VCSObservationMixin:
    def observe_vcs(self, run_id, *, candidate_id, integration_id, target, by, allow_network=False,
                    request_id=None, token=None, timeout=30, cancel_event=None, fetcher=None):
        state = self.state(run_id)
        _owner(state, by)
        if allow_network is not True:
            raise VCSError("explicit allow_network=True is required before any VCS request")
        if fetcher is not None and not callable(fetcher):
            raise VCSError("VCS embedding fetcher must be callable")
        core = _request(state, candidate_id=candidate_id, integration_id=integration_id, target=target,
                        request_id="vcs-read-" + uuid4().hex if request_id is None else request_id, timeout=timeout,
                        transport="owner_embedding_callback" if fetcher is not None else "bounded_github_https_v1")
        existing = state.get("vcs_observations", {}).get(core["request_id"])
        if existing is not None:
            if {key: value for key, value in existing["request"].items() if key not in {"digest", "started_at"}} != core:
                raise VCSError("VCS request ID already names another exact observation")
            return deepcopy(existing)  # Pending stays pending; no automatic reissue.
        if len(state.get("vcs_observations", {})) >= MAX_OBSERVATIONS:
            raise VCSError("VCS observation history reached its ceiling")
        request = dict(core, started_at=self._now())
        request["digest"] = canonical_digest(request)
        _validate_start(state, request)
        self._emit(run_id, "VCS_OBSERVATION_STARTED", request, actor_id=by, expected_seq=state["last_seq"])
        started = time.monotonic_ns()
        interrupted, result, error_code, status = None, None, None, "observed"
        try:
            result = (github_vcs.fetch if fetcher is None else fetcher)(deepcopy(core["target"]), core["binding"]["revision"],
                token=token, timeout=core["limits"]["timeout_seconds"], cancel_event=cancel_event)
            result = github_vcs.redact_result(result, core["target"], core["binding"]["revision"], token=token)
        except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:
            status = "cancelled" if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) or (cancel_event is not None and cancel_event.is_set()) else "unavailable"
            result, error_code = None, "OBSERVATION_UNAVAILABLE"
            if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
                interrupted = error
        finished = self._now()
        receipt = dict(schema="camol.vcs_observation_receipt", schema_version=1, request_id=core["request_id"],
            request_digest=request["digest"], status=status, finished_at=finished,
            elapsed_ms=max(0, (time.monotonic_ns() - started) // 1000000),
            clock_regressed=parse_timestamp(finished, "finish") < parse_timestamp(request["started_at"], "start"),
            result=result, error_code=error_code)
        receipt["digest"] = canonical_digest(receipt)
        _validate_finish(request, receipt)
        try:
            current = self.state(run_id)
            apply(deepcopy(current), dict(run_id=run_id, actor_id=by, type="VCS_OBSERVATION_FINISHED", payload=receipt))
            self._emit(run_id, "VCS_OBSERVATION_FINISHED", receipt, actor_id=by, expected_seq=current["last_seq"])
        except Exception as error:
            if interrupted is not None:
                interrupted.observation_recording_failed = True
                raise interrupted
            raise VCSError("VCS observation ended but receipt persistence failed; inspect the retained request before reissuing") from error
        if interrupted is not None:
            raise interrupted
        return deepcopy(self.state(run_id)["vcs_observations"][core["request_id"]])


def summaries(state, candidate_id=None):
    result = []
    for identity, record in sorted(state.get("vcs_observations", {}).items(), key=lambda row: row[1]["start_seq"]):
        request, receipt = record["request"], record["receipt"]
        if candidate_id is not None and request["binding"]["candidate_id"] != candidate_id:
            continue
        observed = receipt["result"] if receipt else None
        result.append(dict(request_id=identity, candidate_id=request["binding"]["candidate_id"], target=deepcopy(request["target"]), start_seq=record["start_seq"],
            status=receipt["status"] if receipt else "pending", started_at=request["started_at"],
            finished_at=receipt["finished_at"] if receipt else None, elapsed_ms=receipt["elapsed_ms"] if receipt else None,
            request_digest=request["digest"], receipt_digest=receipt["digest"] if receipt else None,
            branch_readback=deepcopy(observed["branch_readback"]) if observed else None,
            pull_request=deepcopy(observed["pull_request"]) if observed else None,
            review_state=observed["review_state"] if observed else "not_observed"))
    return result
