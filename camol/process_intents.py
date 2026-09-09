"""Exclusive process-turn allocation shared by local and future target dispatch.

An intent is not proof of launch, termination, success or resolved external effects.
"""

import os
import fcntl
import stat
from contextlib import contextmanager
from pathlib import Path

from .adapter import ProcessTurnUncertain
from .invocations import _publish_once
from .json_contracts import decode_contract
from .schema import canonical_digest


@contextmanager
def turn_lock(state_dir, run_id, task_id, turn_number):
    if type(turn_number) is not int or turn_number < 1:
        raise ProcessTurnUncertain("process turn number must be a positive integer")
    root = Path(state_dir).resolve()
    directory = root / "packets" / run_id / task_id
    if directory.absolute() != directory.resolve() or root not in directory.resolve().parents:
        raise ProcessTurnUncertain("process turn directory is linked or outside its state root")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "turn-{:03d}.process-lock".format(turn_number)
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
            raise ProcessTurnUncertain("process turn lock is not a private owned file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProcessTurnUncertain("another controller call owns this process turn; inspect its pending result") from error
        yield
    finally:
        if fd is not None:
            os.close(fd)


class ProcessTurnIntent:
    def __init__(self, path, *, agent, assignment, run_id, turn_number, workspace, sandbox_policy):
        self.path = Path(path)
        self.subject = dict(run_id=run_id, task_id=assignment["task_id"], agent_id=assignment["agent_id"],
            lease_id=assignment["lease_id"], fence_digest=assignment.get("fence_digest"), turn_number=turn_number,
            workspace=str(Path(workspace).resolve()), configuration_digest=canonical_digest(dict(
                id=agent["id"], box=agent["box"], adapter=agent["adapter"],
                sandbox_policy_digest=sandbox_policy.digest() if sandbox_policy else None)))
        self.packet_sha256 = None

    def exists(self):
        return self.path.exists() or self.path.is_symlink()

    def _read(self, path=None, *, unlaunched=False):
        path = self.path if path is None else path
        try:
            if path.absolute() != path.resolve():
                raise ValueError("linked intent")
            fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                meta = os.fstat(source.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
                    raise ValueError("unsafe intent")
                value = decode_contract(source.read(8193), max_bytes=8192)
            if (not isinstance(value, dict) or set(value) != {"schema", "schema_version", "subject", "packet_sha256", "digest"}
                    or value["schema"] != ("camol.process_turn_unlaunched" if unlaunched else "camol.process_turn_intent")
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or not isinstance(value["subject"], dict) or set(value["subject"]) != set(self.subject)
                    or (not unlaunched and value["subject"] != self.subject)
                    or any(value["subject"][k] != self.subject[k] for k in ("run_id", "task_id", "turn_number"))
                    or value["digest"] != canonical_digest({k: v for k, v in value.items() if k != "digest"})):
                raise ValueError("foreign or invalid intent")
            return value
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ProcessTurnUncertain("process intent is unsafe or names another turn; inspect before retry") from error

    def _unlaunched_path(self, packet_sha256):
        return self.path.with_name(self.path.stem + "." + packet_sha256 + ".unlaunched.json")

    def proves_unlaunched(self, packet_sha256):
        path = self._unlaunched_path(packet_sha256)
        if not path.exists() and not path.is_symlink():
            return False
        return self._read(path, unlaunched=True)["packet_sha256"] == packet_sha256

    def record_unlaunched(self):
        value = dict(schema="camol.process_turn_unlaunched", schema_version=1,
            subject=self.subject, packet_sha256=self.packet_sha256)
        value["digest"] = canonical_digest(value)
        try:
            _publish_once(self._unlaunched_path(self.packet_sha256), value)
        except FileExistsError:
            if not self.proves_unlaunched(self.packet_sha256):
                raise ProcessTurnUncertain("unlaunched receipt differs from its frozen packet")

    def check_subject(self):
        if self.exists():
            self._read()

    def bind_packet(self, packet_sha256):
        self.packet_sha256 = packet_sha256
        if self.exists() and self._read()["packet_sha256"] != packet_sha256:
            raise ProcessTurnUncertain("process intent belongs to a different frozen packet")

    def reserve(self):
        value = dict(schema="camol.process_turn_intent", schema_version=1,
            subject=self.subject, packet_sha256=self.packet_sha256)
        value["digest"] = canonical_digest(value)
        try:
            _publish_once(self.path, value)
        except FileExistsError:
            self._read()
            raise ProcessTurnUncertain("process turn was already allocated without a recoverable result; reconcile before retry")
