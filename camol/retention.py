"""Bounded, non-mutating retention *inspection*, never deletion authority.

Only event-referenced ordinary CAS objects are enumerated. Shared/control stores
are named, not opened. Cold checkpointed SQLite is deliberately required: using
ordinary mode=ro on a live WAL database can create or update its shared memory.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Optional, Tuple

from .artifacts import artifact_refs
from .events import EVENT_TYPES
from .json_contracts import decode_contract
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest, require_digest
from .store import SQLiteEventStore


class RetentionError(ValueError):
    """A safe bounded snapshot could not be established; retain everything."""


CLASSES = ("transcripts", "audio", "model_inputs", "tool_results", "artifacts", "recovery", "unknown")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise RetentionError(label + " must be bounded non-empty text")
    return value


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise RetentionError(label + " has missing or unknown fields")
    return value


@dataclass(frozen=True)
class RetentionRule:
    content_class: str
    max_age_seconds: Optional[int]
    disposition: str

    def __post_init__(self):
        if not isinstance(self.content_class, str) or not isinstance(self.disposition, str) or self.content_class not in CLASSES or self.disposition not in {"retain", "hold"}:
            raise RetentionError("unsupported retention class or disposition")
        if self.max_age_seconds is not None and (type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 315360000):
            raise RetentionError("retention age must be null or within 1..315360000 seconds")
        if self.content_class == "unknown" and (self.disposition != "hold" or self.max_age_seconds is not None):
            raise RetentionError("unknown content must remain on indefinite hold")

    @classmethod
    def from_dict(cls, value):
        return cls(**_exact(value, ("content_class", "max_age_seconds", "disposition"), "retention rule"))

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RetentionPolicy:
    """Owner-matched inspection policy; this is NOT a recorded human approval."""

    run_id: str
    plan_digest: str
    owner: str
    rules: Tuple[RetentionRule, ...]

    def __post_init__(self):
        _text(self.run_id, "run id")
        _text(self.owner, "owner")
        require_digest(self.plan_digest, "retention plan digest")
        if not isinstance(self.rules, tuple) or any(not isinstance(rule, RetentionRule) for rule in self.rules):
            raise RetentionError("retention rules must be typed immutable records")
        if len(self.rules) != len(CLASSES) or {rule.content_class for rule in self.rules} != set(CLASSES):
            raise RetentionError("retention policy needs exactly one rule for every content class")
        object.__setattr__(self, "rules", tuple(sorted(self.rules, key=lambda rule: rule.content_class)))

    @classmethod
    def from_dict(cls, value):
        _exact(value, ("schema", "schema_version", "run_id", "plan_digest", "owner", "rules"), "retention policy")
        if value["schema"] != "camol.retention_policy" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise RetentionError("unsupported retention policy version")
        if not isinstance(value["rules"], list):
            raise RetentionError("retention rules must be an array")
        return cls(value["run_id"], value["plan_digest"], value["owner"], tuple(RetentionRule.from_dict(rule) for rule in value["rules"]))

    def to_dict(self):
        return dict(schema="camol.retention_policy", schema_version=1, run_id=self.run_id,
                    plan_digest=self.plan_digest, owner=self.owner, rules=[rule.to_dict() for rule in self.rules])

    def digest(self):
        return canonical_digest(self.to_dict())


@dataclass(frozen=True)
class InventoryLimits:
    max_database_bytes: int = 128 << 20
    max_events: int = 20000
    max_streams: int = 256
    max_event_bytes: int = 1 << 20
    max_total_event_bytes: int = 32 << 20
    max_objects: int = 10000
    max_blob_bytes: int = 8 << 20
    max_total_blob_bytes: int = 64 << 20
    max_root_entries: int = 256
    max_json_nodes: int = 100000

    def __post_init__(self):
        ceilings = (512 << 20, 100000, 1024, 8 << 20, 128 << 20, 50000, 64 << 20, 256 << 20, 1024, 1000000)
        for (name, value), ceiling in zip(asdict(self).items(), ceilings):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise RetentionError(name + " exceeds its supported positive inspection bound")


@dataclass(frozen=True)
class StreamCut:
    run_id: str
    event_count: int
    last_seq: int
    events_digest: str

    def __post_init__(self):
        _text(self.run_id, "stream id")
        if type(self.event_count) is not int or self.event_count < 1 or type(self.last_seq) is not int or self.last_seq != self.event_count:
            raise RetentionError("stream cut requires an exact contiguous nonempty event sequence")
        require_digest(self.events_digest, "stream cut digest")


@dataclass(frozen=True)
class ArtifactInventory:
    digest: str
    relative_path: str
    stored_bytes: int
    reference_owners: Tuple[str, ...]
    reference_count: int
    ownership: str
    purpose: str
    content_class: str
    bytes_verified: bool
    full_source_retained: bool
    action: str
    holds: Tuple[str, ...]

    def __post_init__(self):
        require_digest(self.digest, "inventory artifact digest")
        expected = "artifacts/sha256/" + self.digest[7:9] + "/" + self.digest[9:]
        if self.relative_path != expected or type(self.stored_bytes) is not int or self.stored_bytes < 0:
            raise RetentionError("artifact inventory requires an exact CAS path and byte count")
        if not isinstance(self.reference_owners, tuple) or not self.reference_owners or tuple(sorted(set(self.reference_owners))) != self.reference_owners:
            raise RetentionError("artifact reference owners must be unique sorted identities")
        for owner in self.reference_owners:
            _text(owner, "reference owner")
        if type(self.reference_count) is not int or self.reference_count < len(self.reference_owners):
            raise RetentionError("artifact reference count is invalid")
        if self.ownership not in {"run_exclusive", "state_shared", "unknown"} or self.action != "hold" or self.purpose != "ordinary_content" or self.content_class != "unknown":
            raise RetentionError("unsupported artifact inventory classification")
        if type(self.bytes_verified) is not bool or type(self.full_source_retained) is not bool:
            raise RetentionError("artifact verification flags must be booleans")
        _holds(self.holds)


@dataclass(frozen=True)
class ProtectedRoot:
    name: str
    ownership: str
    purpose: str
    observed: bool
    action: str = "hold"

    def __post_init__(self):
        _text(self.name, "protected root name")
        _text(self.purpose, "protected root purpose")
        if self.ownership not in {"state_shared", "project_shared", "external", "control_or_credential", "unknown"} or self.action != "hold" or type(self.observed) is not bool:
            raise RetentionError("protected roots must remain held")


def _holds(value):
    if not isinstance(value, tuple) or not value or tuple(sorted(set(value))) != value:
        raise RetentionError("inventory holds must be unique sorted immutable reasons")
    for reason in value:
        _text(reason, "hold reason")


@dataclass(frozen=True)
class RetentionInventory:
    run_id: str
    plan_digest: str
    owner: str
    policy_digest: str
    state_dir: str
    database_relative_path: str
    source_identity: Tuple[int, int]
    database_digest: str
    stream_cuts: Tuple[StreamCut, ...]
    artifacts: Tuple[ArtifactInventory, ...]
    protected_roots: Tuple[ProtectedRoot, ...]
    reference_scan_complete: bool
    holds: Tuple[str, ...]

    def __post_init__(self):
        _text(self.run_id, "inventory run id")
        _text(self.owner, "inventory owner")
        for digest in (self.plan_digest, self.policy_digest, self.database_digest):
            require_digest(digest, "inventory binding digest")
        if not Path(self.state_dir).is_absolute() or Path(self.database_relative_path).is_absolute() or ".." in Path(self.database_relative_path).parts:
            raise RetentionError("invalid inventory root binding")
        if not isinstance(self.source_identity, tuple) or len(self.source_identity) != 2 or any(type(value) is not int or value < 0 for value in self.source_identity):
            raise RetentionError("invalid source directory identity")
        for values, kind in ((self.stream_cuts, StreamCut), (self.artifacts, ArtifactInventory), (self.protected_roots, ProtectedRoot)):
            if not isinstance(values, tuple) or any(not isinstance(value, kind) for value in values):
                raise RetentionError("inventory entries must be immutable typed records")
        if type(self.reference_scan_complete) is not bool:
            raise RetentionError("reference coverage must be boolean")
        _holds(self.holds)

    def to_dict(self):
        # JSON-normalized output is independent of the frozen records.
        value = asdict(self)
        value.update(schema="camol.retention_inventory", schema_version=1,
                     ownership_scope="event_references_in_this_database_snapshot",
                     filesystem_inventory_complete=False, deletion_authorized=False,
                     policy_approval="not_recorded_by_inspection")
        return decode_contract(json.dumps(value), max_bytes=64 << 20)

    def digest(self):
        return canonical_digest(self.to_dict())


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Root:
    """Descriptor-anchored reads; no traversal through symlinks or special files."""

    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise RetentionError("state directory must be an explicit absolute canonical path")
        self.path = path
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.fd = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = child
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise RetentionError("state root must be owner-controlled")
            self.identity = (info.st_dev, info.st_ino)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def verify(self):
        with _RootContext(self.path) as current:
            if current.identity != self.identity:
                raise RetentionError("state root changed during inspection")

    def file(self, relative, maximum):
        return self._read_file(relative, maximum)

    def _read_file(self, relative, maximum, sink=None):
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(part in {"..", "."} for part in parts):
            raise RetentionError("inventory object escaped its explicit root")
        directory = os.dup(self.fd)
        descriptor = None
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise RetentionError("inventory file must be an owner-controlled single-link regular file")
            if info.st_size > maximum:
                raise RetentionError("inventory file exceeds its byte ceiling")
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise RetentionError("inventory file grew beyond its byte ceiling")
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
            after = os.fstat(descriptor)
            named = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if _identity(info) != _identity(after) or _identity(info) != _identity(named) or size != info.st_size:
                raise RetentionError("inventory file changed during inspection")
            return _identity(info), "sha256:" + digest.hexdigest(), size
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    def entries(self, maximum):
        result = {}
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if len(result) >= maximum:
                    raise RetentionError("state root exceeds its entry ceiling")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise RetentionError("state root contains an unsupported or linked entry")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise RetentionError("state root contains a hard-linked file")
                result[entry.name] = _identity(info)
        return result


class _RootContext(_Root):
    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


PROTECTED = {
    "control": ("control_or_credential", "process_authority"),
    "packets": ("state_shared", "invocation_accounting_and_recovery"),
    "provider-budget.lock": ("state_shared", "invocation_accounting_and_recovery"),
    "records": ("state_shared", "workspace_authority"),
    "workspaces": ("state_shared", "workspace_authority"),
    "worktrees": ("state_shared", "recovery_required"),
    "salvage": ("state_shared", "raw_recovery_required"),
    "provider-preflights": ("state_shared", "control_accounting_unknown_effects"),
    "models": ("state_shared", "model_catalog_and_lifecycle"),
    "model-hosts": ("state_shared", "model_lifecycle_and_credentials"),
    "ssh": ("state_shared", "remote_control_unknown_effects"),
    "watchers": ("state_shared", "watcher_control_and_cursor"),
    "transcript.jsonl": ("project_shared", "transcripts_unattributed"),
    "planning-calls.jsonl": ("project_shared", "accounting"),
    "session.json": ("project_shared", "session_authority"),
}


def _cold_database(root, relative, limits):
    proof = root.file(relative, limits.max_database_bytes)
    _no_journal(root, relative, limits)
    return proof


def _no_journal(root, relative, limits):
    for suffix in ("-wal", "-journal"):
        try:
            sidecar = root.file(relative + suffix, limits.max_database_bytes)
        except FileNotFoundError:
            continue
        if sidecar[2]:
            raise RetentionError("UNSUPPORTED_LIVE_SQLITE: checkpointed cold database required; retain everything")


def _bounded_nodes(value, maximum):
    stack, count = [(value, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > maximum or depth > 64:
            raise RetentionError("event JSON exceeds its node or depth ceiling")
        if isinstance(node, dict):
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)


def inspect_retention(*, state_dir, database, run_id, policy, limits=None):
    """Inspect an explicit, cold state directory; never create/modify source data.

    Unknown event types make all inferred exclusivity unknown. Missing/corrupt
    bodies are holds. Unsafe paths or incomplete bounded snapshots raise, which
    means retain everything. No paths found inside payloads are followed.
    """
    if not isinstance(policy, RetentionPolicy):
        raise RetentionError("a validated RetentionPolicy is required")
    if run_id != policy.run_id:
        raise RetentionError("policy does not bind the selected run")
    limits = InventoryLimits() if limits is None else limits
    if not isinstance(limits, InventoryLimits):
        raise RetentionError("validated InventoryLimits required")
    database = Path(database)
    if not database.is_absolute():
        raise RetentionError("database must be an explicit absolute path")
    try:
        relative = str(database.relative_to(Path(state_dir)))
    except ValueError as error:
        raise RetentionError("database must be contained in the explicit state directory") from error
    try:
        with _RootContext(state_dir) as root:
            return _inspect(root, database, relative, run_id, policy, limits)
    except RetentionError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        # Never include database payloads, credentials or raw parser errors.
        raise RetentionError("unsafe or malformed inventory source; retain everything") from error


def _inspect(root, database, relative, run_id, policy, limits):
    before = root.entries(limits.max_root_entries)
    _no_journal(root, relative, limits)
    # SQLite reopens paths itself. Copy ONLY from our validated nofollow source
    # descriptor; SQLite never receives the source pathname or an external path.
    temporary_parent = Path("/tmp").resolve()
    if temporary_parent == root.path or root.path in temporary_parent.parents:
        raise RetentionError("diagnostic snapshot directory must be outside the selected state root")
    temporary = tempfile.TemporaryDirectory(prefix="camol-retention-", dir=str(temporary_parent))
    connection = None
    # A malformed database cannot consume an unbounded number of SQLite VM steps.
    progress = [0]
    def tick():
        progress[0] += 1
        return int(progress[0] > 20000)
    references, cuts, selected, holds = {}, [], [], {"INSPECTION_ONLY", "UNSCANNED_STORAGE_HELD", "CONTENT_CLASSIFICATION_UNKNOWN"}
    complete = True
    total_bytes = 0
    try:
        snapshot = Path(temporary.name) / "snapshot.sqlite3"
        descriptor = os.open(str(snapshot), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as sink:
            db_proof = root._read_file(relative, limits.max_database_bytes, sink=sink)
            sink.flush()
            os.fsync(sink.fileno())
        _no_journal(root, relative, limits)
        connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0.1)
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(tick, 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        metadata = ("run_id", "event_id", "event_type", "actor_id", "occurred_at", "causation_id", "correlation_id")
        oversized_metadata = " OR ".join("(typeof({0}) NOT IN ('text','null') OR length(CAST({0} AS BLOB))>256)".format(name) for name in metadata)
        oversized = connection.execute("SELECT 1 FROM events WHERE typeof(payload_json)!='text' OR length(CAST(payload_json AS BLOB)) > ? OR " + oversized_metadata + " LIMIT 1", (limits.max_event_bytes,)).fetchone()
        if oversized:
            raise RetentionError("event payload or metadata exceeds its supported type/byte ceiling")
        streams = connection.execute("SELECT run_id, COUNT(*) AS n, MAX(seq) AS last FROM events GROUP BY run_id ORDER BY run_id LIMIT ?", (limits.max_streams + 1,)).fetchall()
        if len(streams) > limits.max_streams or sum(row["n"] for row in streams) > limits.max_events:
            raise RetentionError("event snapshot exceeds its stream or event ceiling")
        for stream in streams:
            identifier = _text(stream["run_id"], "stream id")
            digest = hashlib.sha256()
            seq = 0
            created = 0
            for row in connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (identifier,)):
                seq += 1
                if type(row["seq"]) is not int or row["seq"] != seq:
                    raise RetentionError("event snapshot contains noncontiguous sequences")
                # SQLite length checked before decoding; Python memory stays bounded.
                raw = row["payload_json"]
                if not isinstance(raw, str) or len(raw) > limits.max_event_bytes:
                    raise RetentionError("event payload exceeds its byte ceiling")
                total_bytes += len(raw.encode("utf-8"))
                if total_bytes > limits.max_total_event_bytes:
                    raise RetentionError("event snapshot exceeds its aggregate byte ceiling")
                payload = decode_contract(raw, max_bytes=limits.max_event_bytes)
                _bounded_nodes(payload, limits.max_json_nodes)
                if not isinstance(payload, dict):
                    raise RetentionError("event payload must be an object")
                # Reuse the store's public row normalization after strict decoding.
                event = SQLiteEventStore._row_to_event(row)
                event["payload"] = payload
                for field in ("event_id", "actor_id", "type", "occurred_at"):
                    _text(event[field], "event metadata")
                if event["type"] not in EVENT_TYPES:
                    complete = False
                    holds.add("UNKNOWN_EVENT_TYPE")
                if event["type"] == "RUN_CREATED":
                    created += 1
                    document = validate_runbook(payload["runbook"])
                    if document["run"]["id"] != identifier or runbook_digest(document) != payload["plan_digest"]:
                        raise RetentionError("stream creation does not bind its exact frozen plan")
                digest.update(canonical_digest(event).encode("ascii") + b"\n")
                if identifier == run_id:
                    selected.append(event)
                try:
                    refs = artifact_refs(payload)
                except (ValueError, RuntimeError, TypeError):
                    complete = False
                    holds.add("UNSUPPORTED_ARTIFACT_REFERENCE")
                    continue
                for ref in refs:
                    entry = references.setdefault(ref.digest, {"refs": [], "owners": set()})
                    entry["refs"].append(ref)
                    entry["owners"].add(identifier)
                    if len(references) > limits.max_objects:
                        raise RetentionError("reference snapshot exceeds its object ceiling")
            if created != 1:
                complete = False
                holds.add("UNKNOWN_STREAM_CONTRACT")
            cuts.append(StreamCut(identifier, seq, stream["last"], "sha256:" + digest.hexdigest()))
        connection.execute("COMMIT")
    finally:
        if connection is not None:
            connection.close()
        temporary.cleanup()
    starts = [event for event in selected if event["type"] == "RUN_CREATED"]
    approvals = [event for event in selected if event["type"] == "PLAN_APPROVED"]
    if len(starts) != 1 or starts[0]["payload"]["plan_digest"] != policy.plan_digest:
        raise RetentionError("policy does not bind the selected frozen plan")
    workers = {item["id"] for item in starts[0]["payload"]["runbook"]["agents"]}
    if not approvals or policy.owner in workers or any(event["payload"].get("approved_by") != policy.owner or event["payload"].get("plan_digest") != policy.plan_digest or event["actor_id"] in workers for event in approvals):
        raise RetentionError("policy owner does not match exact recorded human plan approval")
    artifacts, total_blob_bytes, blob_proofs = [], 0, {}
    for digest, entry in sorted(references.items()):
        refs, owners = entry["refs"], tuple(sorted(entry["owners"]))
        sizes = {ref.stored_bytes for ref in refs}
        if max(sizes) > limits.max_blob_bytes:
            raise RetentionError("artifact reference exceeds its byte ceiling")
        path = "artifacts/sha256/" + digest[7:9] + "/" + digest[9:]
        reasons = {"INSPECTION_ONLY", "CONTENT_CLASSIFICATION_UNKNOWN"}
        verified = False
        if len(sizes) != 1:
            reasons.add("CONFLICTING_REFERENCE_SIZES")
        else:
            size = next(iter(sizes))
            if size > limits.max_blob_bytes or total_blob_bytes + size > limits.max_total_blob_bytes:
                raise RetentionError("artifact snapshot exceeds its byte ceiling")
            try:
                proof = root.file(path, limits.max_blob_bytes)
            except FileNotFoundError:
                reasons.add("ARTIFACT_BODY_MISSING")
            else:
                total_blob_bytes += proof[2]
                if total_blob_bytes > limits.max_total_blob_bytes:
                    raise RetentionError("artifact snapshot exceeds its aggregate byte ceiling")
                blob_proofs[path] = proof
                verified = proof[1] == digest and proof[2] == size
                if not verified:
                    reasons.add("ARTIFACT_BODY_MISMATCH")
        ownership = "unknown" if not complete else ("run_exclusive" if len(owners) == 1 else "state_shared")
        if not complete:
            reasons.add("INCOMPLETE_REFERENCE_SCAN")
        if len(owners) > 1:
            reasons.add("SHARED_REFERENCE")
        artifacts.append(ArtifactInventory(digest, path, max(sizes), owners, len(refs), ownership,
                                           "ordinary_content", "unknown", verified,
                                           all(not ref.truncated and ref.stored_bytes == ref.source_bytes and ref.digest == ref.source_sha256 for ref in refs),
                                           "hold", tuple(sorted(reasons))))
    known = dict(PROTECTED)
    known[relative.split("/")[0]] = ("state_shared", "replay_approval_accounting")
    known["artifacts"] = ("state_shared", "referenced_cas_only_unreferenced_content_unscanned")
    for suffix in ("-wal", "-shm", "-journal"):
        known[relative.split("/")[0] + suffix] = ("state_shared", "database_control")
    roots = [ProtectedRoot(name, *known.get(name, ("unknown", "unknown")), observed=True) for name in sorted(before)]
    if any(item.ownership == "unknown" for item in roots):
        holds.add("UNKNOWN_ROOT_HELD")
    # Explicit non-observation: never chase workspace, model or watcher paths.
    roots.append(ProtectedRoot("external_references_not_followed", "external", "source_models_credentials_watch_journals_remote_targets", False))
    for path, proof in blob_proofs.items():
        if root.file(path, limits.max_blob_bytes) != proof:
            raise RetentionError("artifact snapshot changed during inspection")
    if before != root.entries(limits.max_root_entries) or db_proof != _cold_database(root, relative, limits):
        raise RetentionError("snapshot changed during inspection; re-inspect before relying on reference ownership")
    root.verify()
    return RetentionInventory(run_id, policy.plan_digest, policy.owner, policy.digest(), str(root.path), relative,
                              root.identity, db_proof[1], tuple(cuts), tuple(artifacts), tuple(roots), complete, tuple(sorted(holds)))
