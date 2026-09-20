"""Private, append-only preflight intents: a lost response never permits a retry.

This ledger covers capability probes, not worker costs. It deliberately has no
automatic recovery/reset/delete operation and never stores provider output.
"""

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .json_contracts import decode_contract
from .schema import canonical_digest, require_digest, require_identifier


class PreflightJournalError(ValueError):
    pass


def _private(info, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise PreflightJournalError("preflight records must be private owner-only regular files")


def _state_anchor(path):
    # Protect unknown holds across reopen, not only during an open descriptor's
    # lifetime. Otherwise another user can rename a private child from a writable
    # parent and make the next opener construct an apparently empty journal.
    for part in (path, *path.parents):
        info = part.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise PreflightJournalError("preflight state ancestry must be owner/root controlled; writable non-sticky parents are denied")
    if path.lstat().st_uid != os.getuid():
        raise PreflightJournalError("selected preflight state must belong to the current owner")


def _intent(value):
    fields = {"schema", "schema_version", "operation_id", "profile_digest", "target_id", "workspace",
              "request_digest", "max_usd_cents", "created_at"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.provider_preflight_intent" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PreflightJournalError("invalid preflight intent")
    require_identifier(value["operation_id"], "preflight operation")
    require_identifier(value["target_id"], "preflight target")
    for name in ("profile_digest", "request_digest"):
        require_digest(value[name], name)
    if not isinstance(value["workspace"], str) or not Path(value["workspace"]).is_absolute():
        raise PreflightJournalError("preflight workspace must be absolute")
    if type(value["max_usd_cents"]) is not int or not 1 <= value["max_usd_cents"] <= 2 ** 63 - 1:
        raise PreflightJournalError("invalid preflight spend reservation")
    from .providers import _timestamp
    _timestamp(value["created_at"], "preflight created_at")
    return value


def _outcome(value, intent):
    fields = {"schema", "schema_version", "intent_digest", "status", "reason", "usage", "receipt", "observed_at"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.provider_preflight_outcome" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PreflightJournalError("invalid preflight outcome")
    if value["intent_digest"] != canonical_digest(intent) or type(value["status"]) is not str or value["status"] not in {"succeeded", "failed", "unknown"}:
        raise PreflightJournalError("preflight outcome has a different subject or invalid status")
    if type(value["reason"]) is not str or value["reason"] not in {"accepted", "runtime_error", "timeout", "nonzero_exit", "invalid_response", "model_mismatch", "overspend", "provider_error", "interrupted"}:
        raise PreflightJournalError("unknown preflight outcome reason")
    usage = value["usage"]
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cost_usd_micros"}:
        raise PreflightJournalError("invalid preflight usage")
    if any(item is not None and (type(item) is not int or not 0 <= item <= 2 ** 63 - 1) for item in usage.values()):
        raise PreflightJournalError("invalid preflight usage count")
    if value["status"] in {"failed", "succeeded"} and usage["cost_usd_micros"] is None:
        raise PreflightJournalError("unmeasured cost must remain unknown")
    from .providers import ProviderCapabilityReceipt, _timestamp
    _timestamp(value["observed_at"], "preflight observed_at")
    if value["status"] == "succeeded":
        receipt = ProviderCapabilityReceipt.from_dict(value["receipt"])
        if (value["reason"] != "accepted" or receipt.profile_digest != intent["profile_digest"]
                or receipt.target_id != intent["target_id"] or receipt.request_digest != intent["request_digest"]
                or receipt.max_usd_cents != intent["max_usd_cents"]
                or any(usage[name] != getattr(receipt, name) for name in usage)):
            raise PreflightJournalError("preflight success does not match its exact intent")
    elif value["receipt"] is not None:
        raise PreflightJournalError("a failed or uncertain preflight cannot publish a green receipt")
    return value


class PreflightJournal:
    """Bounded owner-only ledger beneath one explicitly selected state directory."""

    def __init__(self, state_dir, *, read_only=False):
        candidate = Path(state_dir).absolute()
        if candidate.is_symlink():
            raise PreflightJournalError("preflight state cannot be a symlink")
        self.state_root = candidate.resolve()
        if not read_only:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _state_anchor(self.state_root)
        self.root = self.state_root / "provider-preflights"
        self.read_only, self.fd, self.lock_fd = read_only, None, None
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(self.fd)
            _private(info, True)
            self.identity = (info.st_dev, info.st_ino)
            flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
            self.lock_fd = os.open("ledger.lock", flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self.fd)
            _private(os.fstat(self.lock_fd))
            self._check()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for name in ("lock_fd", "fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def _check(self):
        _state_anchor(self.state_root)
        info = self.root.lstat()
        _private(info, True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise PreflightJournalError("preflight ledger root identity changed")
        actual = os.stat("ledger.lock", dir_fd=self.fd, follow_symlinks=False)
        opened = os.fstat(self.lock_fd)
        _private(actual)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise PreflightJournalError("preflight ledger lock identity changed")

    @contextmanager
    def _locked(self, write=False):
        if write and self.read_only:
            raise PreflightJournalError("preflight journal is read-only")
        self._check()
        try:
            fcntl.flock(self.lock_fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreflightJournalError("preflight ledger is busy; no request was dispatched") from error
        try:
            self._check()
            yield
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _read(self, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=self.fd)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            _private(before)
            raw = handle.read(65537)
            after = os.fstat(handle.fileno())
        if len(raw) > 65536 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PreflightJournalError("preflight record oversized or changed during read")
        return decode_contract(raw)

    def _write_once(self, name, value):
        raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(raw) > 65536:
            raise PreflightJournalError("preflight record exceeds byte ceiling")
        temporary = ".pending-" + uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._check()
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def _inventory(self):
        names = []
        with os.scandir(self.fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 8193:
                    raise PreflightJournalError("preflight ledger entry ceiling exceeded; no request dispatched")
        names = set(names)
        rows = []
        known = {"ledger.lock"}
        for name in sorted(names):
            if not name.endswith(".intent.json"):
                continue
            intent = _intent(self._read(name))
            stem = canonical_digest({"operation_id": intent["operation_id"]})[7:]
            if name != stem + ".intent.json":
                raise PreflightJournalError("preflight operation filename mismatch")
            outcome_name = stem + ".outcome.json"
            outcome = _outcome(self._read(outcome_name), intent) if outcome_name in names else None
            known.update((name, outcome_name))
            rows.append({"intent": intent, "outcome": outcome})
        # Even an abandoned temporary is a hold, not silently disposable state.
        if names - known:
            raise PreflightJournalError("preflight ledger contains unclassified records; inspect before spending")
        return rows

    def inventory(self):
        with self._locked():
            return self._inventory()

    def reserve(self, intent):
        intent = _intent(intent)
        with self._locked(write=True):
            rows = self._inventory()
            if any(row["outcome"] is None or row["outcome"]["status"] == "unknown" for row in rows):
                raise PreflightJournalError("unresolved preflight usage holds all requests in this state directory; no automatic retry")
            for row in rows:
                if row["intent"]["operation_id"] == intent["operation_id"]:
                    # Timestamps describe the original attempt, not a renewal.
                    comparable = {key: value for key, value in intent.items() if key != "created_at"}
                    if comparable != {key: value for key, value in row["intent"].items() if key != "created_at"}:
                        raise PreflightJournalError("preflight operation ID belongs to a different request")
                    if row["outcome"] is not None and row["outcome"]["status"] == "succeeded":
                        return row
                    raise PreflightJournalError("preflight operation is already spent or uncertain; no automatic retry")
            if len(rows) >= 4096:
                raise PreflightJournalError("preflight operation ceiling exceeded")
            stem = canonical_digest({"operation_id": intent["operation_id"]})[7:]
            self._write_once(stem + ".intent.json", intent)
            return None

    def record(self, intent, outcome):
        _intent(intent)
        _outcome(outcome, intent)
        with self._locked(write=True):
            stem = canonical_digest({"operation_id": intent["operation_id"]})[7:]
            if self._read(stem + ".intent.json") != intent:
                raise PreflightJournalError("preflight intent changed before outcome")
            self._write_once(stem + ".outcome.json", outcome)
