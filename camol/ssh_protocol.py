"""Strict records and bounded framing for optional SSH control transport."""

import hashlib
import json
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .schema import canonical_digest
from .json_contracts import decode_contract


REQUEST_LIMIT = 60 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024
DEFAULT_READ_COMMANDS = frozenset({"ping", "status", "boxes", "box", "plan", "events", "acceptance", "watch-inspect"})
READ_COMMANDS = DEFAULT_READ_COMMANDS | {"box-observe", "box-inbox", "target-local-profile"}
MUTATING_COMMANDS = frozenset({"approve", "drain", "resume", "stop", "force-stop", "accept", "gate-approve", "watch-create", "watch-schedule", "watch-stop", "watch-reopen", "box-message"})
ALL_COMMANDS = READ_COMMANDS | MUTATING_COMMANDS
REMOTE_COMMAND = "camol-ssh-bridge"


class SSHTransportError(RuntimeError):
    def __init__(self, code, message, *, request_id=None, outcome="not_dispatched"):
        super().__init__(message)
        self.code, self.request_id, self.outcome = code, request_id, outcome


def strict_json(raw):
    try:
        value = decode_contract(raw, max_bytes=RESPONSE_LIMIT)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SSHTransportError("PROTOCOL_DENIED", "invalid transport JSON") from error
    if not isinstance(value, dict):
        raise SSHTransportError("PROTOCOL_DENIED", "transport record must be an object")
    return value


def fields(value, required, *, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise SSHTransportError("PROTOCOL_DENIED", "transport record has missing or unknown fields")


def text(value, name, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise SSHTransportError("POLICY_DENIED", "invalid " + name)
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise SSHTransportError("POLICY_DENIED", "a pinned SHA256 digest is required")
    return value


def absolute_path(value, name):
    text(value, name, 4096)
    if not Path(value).is_absolute() or ".." in Path(value).parts:
        raise SSHTransportError("POLICY_DENIED", name + " must be an absolute non-traversing path")
    return value


def command_list(value):
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value) or len(value) != len(set(value)) or any(item not in ALL_COMMANDS for item in value):
        raise SSHTransportError("POLICY_DENIED", "invalid control command allowlist")
    return tuple(sorted(value))


def read_regular(path, *, maximum, owner=True, private=False):
    """Open public/control data once; never use this for a private key."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum
                    or (owner and metadata.st_uid != os.getuid())
                    or metadata.st_mode & 0o022 or (private and metadata.st_mode & 0o077)):
                raise OSError("unsafe file")
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise OSError("oversize file")
            return data
    except OSError as error:
        raise SSHTransportError("POLICY_DENIED", "transport file is missing, unsafe or oversized") from error


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encode_frame(value, *, limit=REQUEST_LIMIT):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise SSHTransportError("PROTOCOL_DENIED", "transport value is not finite JSON") from error
    if not raw or len(raw) > limit:
        raise SSHTransportError("PROTOCOL_DENIED", "transport frame exceeds its byte limit")
    return struct.pack("!I", len(raw)) + raw


async def read_frame(reader, *, limit=RESPONSE_LIMIT):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size < 2 or size > limit:
        raise SSHTransportError("PROTOCOL_DENIED", "transport frame exceeds its byte limit")
    return strict_json(await reader.readexactly(size))


def validate_identity(value):
    fields(value, {"camol_version", "python_executable", "python_sha256", "package_sha256", "control_version"})
    text(value["camol_version"], "Camol version")
    absolute_path(value["python_executable"], "remote Python executable")
    digest(value["python_sha256"])
    digest(value["package_sha256"])
    if type(value["control_version"]) is not int or value["control_version"] != 3:
        raise SSHTransportError("POLICY_DENIED", "remote bridge must support bound control V3")
    return dict(value)


@dataclass(frozen=True)
class SSHTarget:
    name: str
    host: str
    port: int
    login: str
    known_hosts: str
    known_hosts_sha256: str
    identity_file: str
    target_id: str
    target_digest: str
    run_id: str
    plan_digest: str
    owner: str
    bridge_identity: dict
    allowed_commands: tuple = tuple(sorted(DEFAULT_READ_COMMANDS))

    def __post_init__(self):
        for field in ("name", "target_id", "run_id", "owner"):
            text(getattr(self, field), field)
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9:][A-Za-z0-9.:-]{0,252}", self.host):
            raise SSHTransportError("POLICY_DENIED", "host must be a literal hostname or IP address")
        if not isinstance(self.login, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", self.login):
            raise SSHTransportError("POLICY_DENIED", "invalid SSH login name")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise SSHTransportError("POLICY_DENIED", "invalid SSH port")
        absolute_path(self.known_hosts, "known_hosts")
        absolute_path(self.identity_file, "identity_file")
        for field in ("known_hosts_sha256", "target_digest", "plan_digest"):
            digest(getattr(self, field))
        identity = dict(self.bridge_identity) if isinstance(self.bridge_identity, MappingProxyType) else self.bridge_identity
        object.__setattr__(self, "bridge_identity", MappingProxyType(validate_identity(identity)))
        object.__setattr__(self, "allowed_commands", command_list(list(self.allowed_commands)))

    def to_dict(self):
        return dict(schema="camol.ssh_target", schema_version=1, **{key: list(value) if key == "allowed_commands" else dict(value) if key == "bridge_identity" else value
                    for key, value in self.__dict__.items()})

    @classmethod
    def from_dict(cls, value):
        required = set(cls.__dataclass_fields__) - {"allowed_commands"}
        fields(value, required | {"schema", "schema_version"}, optional={"allowed_commands"})
        if value["schema"] != "camol.ssh_target" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise SSHTransportError("PROTOCOL_DENIED", "unsupported SSH target schema")
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})

    def digest(self):
        return canonical_digest(self.to_dict())


def load_bridge_policy(path):
    policy = strict_json(read_regular(path, maximum=REQUEST_LIMIT, private=True))
    fields(policy, {"schema", "schema_version", "targets"})
    if policy["schema"] != "camol.ssh_bridge_policy" or type(policy["schema_version"]) is not int or policy["schema_version"] != 1 or not isinstance(policy["targets"], dict) or len(policy["targets"]) > 100:
        raise SSHTransportError("POLICY_DENIED", "unsupported remote bridge policy")
    for name, target in policy["targets"].items():
        text(name, "remote target name")
        fields(target, {"state_dir", "run_id", "plan_digest", "owner", "allowed_commands"})
        absolute_path(target["state_dir"], "remote state directory")
        text(target["run_id"], "remote run ID")
        text(target["owner"], "remote owner")
        digest(target["plan_digest"])
        command_list(target["allowed_commands"])
    return policy
