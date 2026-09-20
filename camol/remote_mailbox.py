"""Explicit observe / review / send over owner-pinned remote control.

Messages are data, not work assignments, plan edits or execution authority.
The remote kernel owns freshness, fencing, deduplication and delivery state.
"""

from copy import deepcopy
from datetime import timedelta

from .mailbox import SUBJECT
from .probes import Redactor
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .ssh_protocol import SSHTransportError, fields


def _version(value, schema, names):
    fields(value, {"schema", "schema_version"} | set(names))
    if value["schema"] != schema or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SSHTransportError("PROTOCOL_DENIED", "unsupported remote mailbox record")


def _checked_digest(value):
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise SSHTransportError("POLICY_DENIED", "remote mailbox record digest changed")


def _observation(value, target):
    _version(value, "camol.box_message_target", {"subject", "observed_cursor", "observed_at", "expires_at", "digest"})
    _checked_digest(value)
    fields(value["subject"], SUBJECT)
    subject = value["subject"]
    for name in SUBJECT - {"plan_digest", "fence_digest"}:
        require_identifier(subject[name], name)
    for name in ("plan_digest", "fence_digest"):
        require_digest(subject[name], name)
    if (subject["run_id"], subject["plan_digest"]) != (target.run_id, target.plan_digest):
        raise SSHTransportError("POLICY_DENIED", "mailbox observation belongs to another remote run or plan")
    start = parse_timestamp(value["observed_at"], "remote observation time")
    expiry = parse_timestamp(value["expires_at"], "remote observation expiry")
    if type(value["observed_cursor"]) is not int or value["observed_cursor"] < 1 or not start < expiry <= start + timedelta(seconds=60):
        raise SSHTransportError("PROTOCOL_DENIED", "invalid remote observation cursor or lifetime")
    # Do not compare clocks across hosts. The remote kernel checks current lease
    # and expiry on new post; an exact retry can retrieve an already queued message.
    return value


class RemoteMailbox:
    def __init__(self, client):
        self.client, self.target = client, client.target
        self.scope = self.target.digest()

    def _scope(self):
        if self.client.target.digest() != self.scope:
            raise SSHTransportError("POLICY_DENIED", "remote mailbox client target changed")

    def _params(self, box_id):
        self._scope()
        require_identifier(box_id, "remote box ID")
        return dict(run_id=self.target.run_id, plan_digest=self.target.plan_digest, box_id=box_id)

    async def observe(self, box_id):
        response = await self.client.request("box-observe", params=self._params(box_id))
        self._scope()
        if response.get("ok") is not True:
            raise SSHTransportError("REMOTE_REJECTED", "remote controller refused mailbox observation")
        observation = _observation(response.get("result"), self.target)
        if observation["subject"]["box_id"] != box_id:
            raise SSHTransportError("PROTOCOL_DENIED", "remote controller returned a different box")
        value = dict(schema="camol.remote_box_observation", schema_version=1,
                     target_profile_digest=self.scope, observation=deepcopy(observation))
        return dict(value, digest=canonical_digest(value))

    def prepare(self, observation, *, request_id, body, kind="information", correlation_id=None, ttl_seconds=300):
        """Pure local review object. Persist it privately before attempting send."""
        self._scope()
        _version(observation, "camol.remote_box_observation", {"target_profile_digest", "observation", "digest"})
        _checked_digest(observation)
        if observation["target_profile_digest"] != self.scope:
            raise SSHTransportError("POLICY_DENIED", "box observation belongs to another pinned target")
        observed = _observation(observation["observation"], self.target)
        params = dict(self._params(observed["subject"]["box_id"]), target=deepcopy(observed), request_id=request_id,
                      body=Redactor().text(body) if isinstance(body, str) else body, kind=kind,
                      correlation_id=correlation_id or request_id, ttl_seconds=ttl_seconds)
        value = dict(schema="camol.remote_message_intent", schema_version=1, target_profile_digest=self.scope,
                     sender=self.target.owner, params=params)
        value["digest"] = canonical_digest(value)
        return self._intent(value)

    def _intent(self, value):
        self._scope()
        _version(value, "camol.remote_message_intent", {"target_profile_digest", "sender", "params", "digest"})
        _checked_digest(value)
        if value["target_profile_digest"] != self.scope or value["sender"] != self.target.owner:
            raise SSHTransportError("POLICY_DENIED", "message intent belongs to another pinned target or owner")
        params = value["params"]
        fields(params, {"run_id", "plan_digest", "box_id", "target", "request_id", "body", "kind", "correlation_id", "ttl_seconds"})
        observation = _observation(params["target"], self.target)
        if any(params[key] != observation["subject"][key] for key in ("run_id", "plan_digest", "box_id")):
            raise SSHTransportError("POLICY_DENIED", "message intent and observed box differ")
        for name in ("request_id", "correlation_id"):
            require_identifier(params[name], name)
        body = params["body"]
        if (not isinstance(body, str) or not body.strip() or len(body) > 2000
                or any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in body)
                or not isinstance(params["kind"], str) or params["kind"] not in {"information", "question", "proposal", "warning"}
                or type(params["ttl_seconds"]) is not int or not 1 <= params["ttl_seconds"] <= 3600):
            raise SSHTransportError("POLICY_DENIED", "message body, kind or TTL is invalid")
        return deepcopy(value)

    async def send(self, intent, *, approved_by, approval_digest):
        frozen = self._intent(intent)
        if approved_by != self.target.owner or approval_digest != frozen["digest"]:
            raise SSHTransportError("APPROVAL_REQUIRED", "approve the exact remote message intent as its pinned owner")
        # Underlying transport journals possible dispatch and blocks mutations
        # after uncertain outcomes. Never refresh the observation or retry here.
        return await self.client.request("box-message", params=frozen["params"], requested_by=approved_by)

    async def inbox(self, box_id, *, offset=0, limit=100):
        params = self._params(box_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SSHTransportError("POLICY_DENIED", "inbox offset/limit is invalid")
        response = await self.client.request("box-inbox", params=dict(params, offset=offset, limit=limit))
        self._scope()
        if response.get("ok") is not True:
            raise SSHTransportError("REMOTE_REJECTED", "remote controller refused inbox inspection")
        result = response.get("result")
        if (not isinstance(result, dict) or result.get("schema") != "camol.box_inbox"
                or type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or any(result.get(key) != value for key, value in params.items())
                or not isinstance(result.get("messages"), list) or len(result["messages"]) > limit):
            raise SSHTransportError("PROTOCOL_DENIED", "remote inbox does not bind this request")
        if (type(result.get("total")) is not int or result["total"] < len(result["messages"])
                or type(result.get("more")) is not bool or result["more"] != (offset + len(result["messages"]) < result["total"])):
            raise SSHTransportError("PROTOCOL_DENIED", "remote inbox pagination is inconsistent")
        for record in result["messages"]:
            message = record.get("message") if isinstance(record, dict) else None
            observation = message.get("target") if isinstance(message, dict) else None
            subject = observation.get("subject") if isinstance(observation, dict) else None
            if not isinstance(subject, dict) or any(subject.get(key) != value for key, value in params.items()):
                raise SSHTransportError("PROTOCOL_DENIED", "remote inbox contains a foreign message")
        return deepcopy(result)
