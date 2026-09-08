"""Exact owner-reviewed push intent, bounded execution, and replayable outcome."""

from copy import deepcopy
from datetime import timedelta
import time

from . import git_push
from .evaluation import IntegrationReceipt
from .events import new_event
from .probes import Redactor
from .schema import canonical_digest, parse_timestamp, require_identifier
from .vcs import VCSError, _owner, _settled
from .vcs_observations import binding


EVENTS = frozenset({"VCS_PUSH_STARTED", "VCS_PUSH_FINISHED"})
MAX_PUSHES = 1000


def _eligible(state, by):
    _owner(state, by)
    _settled(state)
    if state["status"] != "completed":
        raise VCSError("push requires a completed run, including any frozen final human gate")


def propose(state, *, candidate_id, integration_id, target, expected_old, request_id,
            issued_at, expires_at, timeout_seconds=30, _target_identity=None):
    _eligible(state, state["approved_by"])
    subject = binding(state, candidate_id, integration_id)
    receipt = IntegrationReceipt.from_dict(next(row for row in state["integrations"] if row["integration_id"] == integration_id))
    require_identifier(request_id, "push request")
    if len(request_id) > 128 or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise VCSError("push request identity or timeout exceeds its bound")
    if expected_old is not None:
        git_push.sha(expected_old)
    start, expiry = parse_timestamp(issued_at, "push issued"), parse_timestamp(expires_at, "push expiry")
    if not start < expiry <= start + timedelta(hours=1):
        raise VCSError("push approval window must be positive and at most one hour")
    target = git_push.target(target)
    target_identity = git_push.identity(target) if _target_identity is None else git_push.validate_identity(_target_identity, target)
    value = dict(schema="camol.vcs_push_proposal", schema_version=1, request_id=request_id,
        binding=subject, source_workspace=receipt.workspace.path, target=target, target_identity=target_identity,
        expected_old=expected_old, owner=state["approved_by"], issued_at=issued_at, expires_at=expires_at,
        timeout_seconds=timeout_seconds, allow_non_fast_forward=False, include_reachable_history=True,
        automatic_retries=0)
    if Redactor().value(value) != value:
        raise VCSError("push proposal contains protected material")
    return dict(value, digest=canonical_digest(value))


def validate_proposal(state, value):
    try:
        expected = propose(state, candidate_id=value["binding"]["candidate_id"],
            integration_id=value["binding"]["integration_id"], target=value["target"],
            expected_old=value["expected_old"], request_id=value["request_id"],
            issued_at=value["issued_at"], expires_at=value["expires_at"], timeout_seconds=value["timeout_seconds"],
            _target_identity=value["target_identity"])
        if canonical_digest(value) != canonical_digest(expected):
            raise VCSError("push proposal differs from its exact owner/plan/code/target contract")
        return expected
    except (TypeError, KeyError) as error:
        raise VCSError("push proposal is incomplete") from error


def _unresolved(state, proposal):
    for record in state.get("vcs_pushes", {}).values():
        previous = record["proposal"]
        same = (previous["target_identity"] == proposal["target_identity"]
                and previous["target"]["branch"] == proposal["target"]["branch"])
        if same and (
                record["receipt"] is None or record["receipt"]["result"]["status"] == "effect_unknown"):
            raise VCSError("this target has an unresolved push; reconcile it before another request")


def _result(value, proposal):
    fields = {"status", "dispatch_attempted", "observed_before", "observed_after", "push_exit_code", "error_code"}
    if not isinstance(value, dict) or set(value) != fields or type(value["dispatch_attempted"]) is not bool:
        raise VCSError("invalid push result fields")
    for key in ("observed_before", "observed_after"):
        if value[key] is not None:
            git_push.sha(value[key])
    code = value["push_exit_code"]
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise VCSError("invalid push exit status")
    status = value["status"]
    if status in {"confirmed", "already_present"}:
        if (value["observed_before"] != proposal["expected_old"] or value["observed_after"] != proposal["binding"]["revision"]
                or value["error_code"] is not None):
            raise VCSError("successful push result lacks exact ref observations")
        if status == "confirmed" and (not value["dispatch_attempted"] or code != 0):
            raise VCSError("confirmed push requires a successful actual push command")
        if status == "already_present" and (value["dispatch_attempted"] or code is not None
                or value["observed_before"] != value["observed_after"]):
            raise VCSError("already-present observation cannot claim a push")
    elif status == "not_dispatched":
        if value["dispatch_attempted"] or code is not None or value["observed_after"] is not None or value["error_code"] != "PUSH_NOT_DISPATCHED":
            raise VCSError("pre-dispatch result cannot claim an effect")
    elif status == "effect_unknown":
        if not value["dispatch_attempted"] or value["error_code"] != "EFFECT_UNKNOWN":
            raise VCSError("unknown push result lacks an attempted dispatch")
    else:
        raise VCSError("unknown push outcome")
    return deepcopy(value)


def apply(state, event):
    _eligible(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise VCSError("push event belongs to another run")
    value = event["payload"]
    records = state.setdefault("vcs_pushes", {})
    if event["type"] == "VCS_PUSH_STARTED":
        if not isinstance(value, dict) or set(value) != {"proposal", "approved_digest", "started_at"}:
            raise VCSError("invalid push intent")
        proposal = validate_proposal(state, value["proposal"])
        if value["approved_digest"] != proposal["digest"] or value["started_at"] != event["occurred_at"]:
            raise VCSError("push intent approval or timestamp differs")
        now = parse_timestamp(value["started_at"], "push start")
        if not parse_timestamp(proposal["issued_at"], "issued") <= now < parse_timestamp(proposal["expires_at"], "expires"):
            raise VCSError("push proposal is expired or future issued")
        if proposal["request_id"] in records or len(records) >= MAX_PUSHES:
            raise VCSError("push request reused or history full")
        _unresolved(state, proposal)
        records[proposal["request_id"]] = dict(proposal=proposal, started_at=value["started_at"], receipt=None)
    else:
        fields = {"request_id", "proposal_digest", "finished_at", "elapsed_ms", "result", "digest"}
        if not isinstance(value, dict) or set(value) != fields or value["request_id"] not in records:
            raise VCSError("push outcome has no exact intent")
        record = records[value["request_id"]]
        if record["receipt"] is not None or value["proposal_digest"] != record["proposal"]["digest"]:
            raise VCSError("push outcome reused or mismatched")
        if (value["finished_at"] != event["occurred_at"] or type(value["elapsed_ms"]) is not int
                or not 0 <= value["elapsed_ms"] < 2**63
                or value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"})):
            raise VCSError("invalid push outcome measurement or digest")
        parse_timestamp(value["finished_at"], "push finish")
        _result(value["result"], record["proposal"])
        record["receipt"] = deepcopy(value)


class VCSPush:
    def __init__(self, orchestrator, run_id):
        self.orchestrator, self.run_id = orchestrator, run_id

    def propose(self, **kwargs):
        return propose(self.orchestrator.state(self.run_id), **kwargs)

    def _append(self, kind, value, by, when, state):
        from .state import apply_event
        event = new_event(self.run_id, kind, by, value, occurred_at=when)
        apply_event(state, dict(event, seq=state["last_seq"] + 1))
        self.orchestrator.store.append(event, expected_seq=state["last_seq"])

    def publish(self, proposal, *, by, review_digest, allow_write=False, allow_network=False, token=None, cancel_event=None):
        state = self.orchestrator.state(self.run_id)
        _eligible(state, by)
        proposal = validate_proposal(state, proposal)
        if review_digest != proposal["digest"]:
            raise VCSError("approve the exact push proposal digest")
        existing = state.get("vcs_pushes", {}).get(proposal["request_id"])
        if existing is not None:
            if existing["proposal"] != proposal:
                raise VCSError("push request ID names another proposal")
            return deepcopy(existing)  # Historical lookup never reissues an effect.
        if allow_write is not True or (proposal["target"]["kind"] == "github_https" and allow_network is not True):
            raise VCSError("push requires explicit write permission and network permission for GitHub")
        git_push.validate_credential(proposal["target"], token)
        now = self.orchestrator._now()
        self._append("VCS_PUSH_STARTED", dict(proposal=proposal,
            approved_digest=review_digest, started_at=now), by, now, state)
        started = time.monotonic_ns()

        def authorize():
            current = self.orchestrator.state(self.run_id)
            _eligible(current, by)
            validate_proposal(current, proposal)
            if not parse_timestamp(proposal["issued_at"], "issued") <= self.orchestrator.clock() < parse_timestamp(proposal["expires_at"], "expires"):
                raise VCSError("push approval expired before dispatch")
            retained = current["vcs_pushes"][proposal["request_id"]]
            if retained["proposal"] != proposal or retained["receipt"] is not None:
                raise VCSError("push intent changed before dispatch")

        outcome = git_push.publish(proposal, token=token, cancel_event=cancel_event, authorize=authorize)
        _result(outcome, proposal)
        finished = self.orchestrator._now()
        receipt = dict(request_id=proposal["request_id"], proposal_digest=proposal["digest"], finished_at=finished,
                       elapsed_ms=(time.monotonic_ns() - started) // 1000000, result=outcome)
        receipt["digest"] = canonical_digest(receipt)
        self._append("VCS_PUSH_FINISHED", receipt, by, finished, self.orchestrator.state(self.run_id))
        return deepcopy(self.orchestrator.state(self.run_id)["vcs_pushes"][proposal["request_id"]])
