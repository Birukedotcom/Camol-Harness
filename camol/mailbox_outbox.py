"""Private immutable UI send intents; no automatic retries or retargeting."""

import json
import os
from pathlib import Path
import stat
from itertools import islice

from .invocations import _publish_once
from .json_contracts import decode_contract
from .schema import canonical_digest, require_digest, require_identifier


class OutboxError(ValueError):
    pass


def _private(info, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise OutboxError("outbox must use owner-private, unlinked regular records")


def _read(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        _private(before)
        if before.st_size > 32768:
            raise OutboxError("outbox record exceeds 32 KiB")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(32769)
        after, named = os.fstat(descriptor), path.lstat()
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            if getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(named, key):
                raise OutboxError("outbox record changed while reading")
        return decode_contract(raw, max_bytes=32768)
    finally:
        os.close(descriptor)


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "schema_version", "scope", "sender", "params", "digest"}
            or value["schema"] != "camol.ui_message_intent" or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise OutboxError("unsupported outbox intent")
    require_digest(value["scope"], "project scope")
    require_identifier(value["sender"], "message sender")
    params = value["params"]
    if not isinstance(params, dict) or set(params) != {"run_id", "plan_digest", "box_id", "request_id", "target", "body", "kind", "correlation_id", "ttl_seconds"}:
        raise OutboxError("outbox request has missing or unknown fields")
    for key in ("run_id", "box_id", "request_id", "correlation_id"):
        require_identifier(params[key], key)
    require_digest(params["plan_digest"], "plan digest")
    if (not isinstance(params["body"], str) or not 1 <= len(params["body"]) <= 2000
            or not isinstance(params["kind"], str) or params["kind"] not in {"information", "question", "proposal", "warning"}
            or type(params["ttl_seconds"]) is not int or not 1 <= params["ttl_seconds"] <= 3600):
        raise OutboxError("outbox body, kind or TTL is invalid")
    target = params["target"]
    if not isinstance(target, dict) or not isinstance(target.get("subject"), dict):
        raise OutboxError("outbox lacks an exact observed target")
    if any(target["subject"].get(key) != params[key] for key in ("run_id", "plan_digest", "box_id")):
        raise OutboxError("outbox target and request differ")
    if value["digest"] != canonical_digest({key: item for key, item in value.items() if key != "digest"}):
        raise OutboxError("outbox intent digest changed")
    return value


class Outbox:
    """Caller holds SessionStore.transaction; records survive client crashes."""
    def __init__(self, project_dir):
        self.parent = Path(project_dir)
        self.path = self.parent / "message-outbox"
        _private(self.parent.lstat(), True)

    def _directory(self, create=False):
        if create:
            self.path.mkdir(mode=0o700, exist_ok=True)
        if not self.path.exists() and not self.path.is_symlink():
            return False
        _private(self.path.lstat(), True)
        return True

    def get(self, request_id):
        require_identifier(request_id, "outbox request ID")
        if not self._directory():
            raise OutboxError("no saved message request")
        value = validate(_read(self.path / (request_id + ".json")))
        if value["params"]["request_id"] != request_id:
            raise OutboxError("outbox filename has a different request identity")
        return value

    def put(self, scope, sender, params):
        value = dict(schema="camol.ui_message_intent", schema_version=1, scope=scope, sender=sender, params=params)
        value["digest"] = canonical_digest(value)
        validate(value)
        if len(json.dumps(value).encode()) > 32768:
            raise OutboxError("outbox request exceeds 32 KiB")
        self._directory(create=True)
        path = self.path / (params["request_id"] + ".json")
        if path.exists() or path.is_symlink():
            if self.get(params["request_id"]) != value:
                raise OutboxError("request identity was already saved with different content")
            return value
        names = list(islice(self.path.iterdir(), 2001))
        present = {item.name for item in names}
        pending_receipts = sum(1 for item in names if item.name.endswith(".json")
                               and not item.name.endswith(".accepted.json")
                               and item.name[:-5] + ".accepted.json" not in present)
        # Reserve the eventual acceptance file even when the response is lost.
        if len(names) + pending_receipts + 2 > 2000:
            raise OutboxError("outbox retention limit reached; inspect before sending more")
        try:
            _publish_once(path, value)
        except FileExistsError:
            if self.get(params["request_id"]) != value:
                raise OutboxError("request identity was already saved with different content")
        return value

    def accepted(self, value, result):
        validate(value)
        params = value["params"]
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, dict) or any(message.get(key) != params[key] for key in ("request_id", "target", "body", "kind", "correlation_id")):
            raise OutboxError("response does not bind the saved message; outcome remains unknown")
        if not isinstance(message.get("sender"), dict) or message["sender"].get("id") != value["sender"] or message["sender"].get("kind") != "human":
            raise OutboxError("message response has a different sender")
        identity = require_identifier(message.get("message_id"), "message identity")
        if identity != "message-" + canonical_digest(dict(run_id=params["run_id"], request_id=params["request_id"])).split(":")[1]:
            raise OutboxError("response message ID differs from its request")
        receipt = dict(schema="camol.ui_message_accepted", schema_version=1, intent_digest=value["digest"], message_id=identity)
        path = self.path / (params["request_id"] + ".accepted.json")
        try:
            _publish_once(path, receipt)
        except FileExistsError:
            if _read(path) != receipt:
                raise OutboxError("conflicting saved message acceptance")
        return receipt

    def status(self, request_id):
        value = self.get(request_id)
        path = self.path / (request_id + ".accepted.json")
        receipt = None
        if path.exists() or path.is_symlink():
            receipt = _read(path)
            if (not isinstance(receipt, dict) or set(receipt) != {"schema", "schema_version", "intent_digest", "message_id"}
                    or receipt["schema"] != "camol.ui_message_accepted" or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                    or receipt["intent_digest"] != value["digest"]):
                raise OutboxError("invalid outbox acceptance")
            require_identifier(receipt["message_id"], "accepted message identity")
            expected = "message-" + canonical_digest(dict(run_id=value["params"]["run_id"], request_id=request_id)).split(":")[1]
            if receipt["message_id"] != expected:
                raise OutboxError("saved acceptance belongs to another message")
        return dict(intent=value, status="accepted_not_consumed" if receipt else "unknown_or_not_sent", receipt=receipt)

    def list(self):
        if not self._directory():
            return []
        names = list(islice(self.path.iterdir(), 2001))
        if len(names) > 2000:
            raise OutboxError("outbox exceeds bounded retention allowance")
        result = []
        for path in sorted(names):
            if path.name.endswith(".accepted.json"):
                if not path.with_name(path.name.replace(".accepted.json", ".json")).is_file():
                    raise OutboxError("outbox acceptance has lost its send intent")
                continue
            if path.name.startswith(".invocation-"):
                continue
            if not path.name.endswith(".json"):
                raise OutboxError("unexpected outbox entry; inspect before continuing")
            result.append(self.status(path.name[:-5]))
        return result
