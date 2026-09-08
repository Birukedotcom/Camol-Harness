"""Content-addressed, redacted artifact storage and portable run exports."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .probes import Redactor
from .archive_io import ArchiveRoot
from .json_contracts import decode_contract
from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_bool,
    require_digest,
    require_identifier,
    require_non_negative_int,
    require_object,
    require_schema_header,
    require_string,
    require_timestamp,
)


class ArtifactError(RuntimeError):
    """Artifact bytes or metadata could not be persisted or verified safely."""


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    try:
        _real(path).relative_to(_real(root))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class ArtifactRef:
    """A portable reference to redacted retained bytes and their raw-source hash."""

    SCHEMA = "camol.artifact_ref"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "digest", "stored_bytes", "source_sha256",
        "source_bytes", "media_type", "encoding", "redacted", "truncated",
        "truncation_policy", "producer", "created_at",
    )
    PRODUCER_FIELDS = frozenset(
        {"run_id", "task_id", "agent_id", "lease_id", "invocation_id", "channel", "role"}
    )

    digest: str
    stored_bytes: int
    source_sha256: str
    source_bytes: int
    media_type: str
    encoding: str
    redacted: bool
    truncated: bool
    truncation_policy: str
    producer: Dict[str, str]
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", require_digest(self.digest, "artifact digest"))
        object.__setattr__(self, "source_sha256", require_digest(self.source_sha256, "artifact source_sha256"))
        object.__setattr__(self, "stored_bytes", require_non_negative_int(self.stored_bytes, "artifact stored_bytes"))
        object.__setattr__(self, "source_bytes", require_non_negative_int(self.source_bytes, "artifact source_bytes"))
        object.__setattr__(self, "media_type", require_string(self.media_type, "artifact media_type"))
        object.__setattr__(self, "encoding", require_string(self.encoding, "artifact encoding"))
        object.__setattr__(self, "redacted", require_bool(self.redacted, "artifact redacted"))
        object.__setattr__(self, "truncated", require_bool(self.truncated, "artifact truncated"))
        object.__setattr__(self, "truncation_policy", require_string(self.truncation_policy, "artifact truncation_policy"))
        producer = require_object(self.producer, "artifact producer")
        reject_unknown_fields(producer, self.PRODUCER_FIELDS, "artifact producer")
        if not producer:
            raise ArtifactError("artifact producer must not be empty")
        normalized = {}
        for name, value in producer.items():
            normalized[name] = require_identifier(value, "artifact producer " + name)
        object.__setattr__(self, "producer", dict(sorted(normalized.items())))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, "artifact created_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "digest": self.digest,
            "stored_bytes": self.stored_bytes,
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "redacted": self.redacted,
            "truncated": self.truncated,
            "truncation_policy": self.truncation_policy,
            "producer": dict(self.producer),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactRef":
        data = require_object(payload, "artifact reference")
        require_schema_header(data, cls.SCHEMA, cls.SCHEMA_VERSION, "artifact reference")
        reject_unknown_fields(data, cls.FIELDS, "artifact reference")
        missing = sorted(set(cls.FIELDS) - set(data))
        if missing:
            raise ArtifactError("artifact reference is missing fields: {}".format(", ".join(missing)))
        return cls(**{name: data[name] for name in cls.FIELDS if name not in {"schema", "schema_version"}})


class ArtifactStore:
    """Immutable blob store rooted at ``STATE_DIR/artifacts``.

    Raw text is redacted before persistence. Large inputs are drained and
    hashed in full while retaining at most ``max_retained_bytes``.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        redactor: Optional[Redactor] = None,
        max_retained_bytes: int = 1 << 20,
    ):
        if not isinstance(max_retained_bytes, int) or max_retained_bytes <= 0:
            raise ArtifactError("max_retained_bytes must be positive")
        self.state_dir = _real(Path(state_dir))
        if not self.state_dir.is_dir() or Path(state_dir).is_symlink():
            raise ArtifactError("state directory must be a real existing directory")
        self.root = self.state_dir / "artifacts"
        if self.root.is_symlink():
            raise ArtifactError("artifact store must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir() or not _inside(self.root, self.state_dir):
            raise ArtifactError("artifact store escaped the state directory")
        self.redactor = redactor or Redactor()
        self.max_retained_bytes = max_retained_bytes

    def path_for(self, digest: str) -> Path:
        require_digest(digest, "artifact digest")
        hexadecimal = digest.split(":", 1)[1]
        path = self.root / "sha256" / hexadecimal[:2] / hexadecimal[2:]
        if not _inside(path, self.root) or path.is_symlink():
            raise ArtifactError("artifact path escaped its content-addressed root")
        return path

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise ArtifactError("artifact shard directory must not be a symlink")
        descriptor, temporary = tempfile.mkstemp(prefix=".camol-artifact-", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(str(temporary_path), 0o600)
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise ArtifactError("artifact digest collision or corrupt existing blob")
            else:
                os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def put_bytes(
        self,
        content: bytes,
        *,
        producer: Mapping[str, str],
        media_type: str = "text/plain",
        encoding: str = "utf-8",
        redact: bool = True,
        source_sha256: Optional[str] = None,
        source_bytes: Optional[int] = None,
        truncated: bool = False,
        created_at: Optional[str] = None,
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise ArtifactError("artifact content must be bytes")
        original_digest = source_sha256 or _sha256(content)
        original_bytes = len(content) if source_bytes is None else source_bytes
        require_digest(original_digest, "artifact source_sha256")
        require_non_negative_int(original_bytes, "artifact source_bytes")
        upstream_truncated = truncated or original_bytes > len(content)
        if redact:
            # Redact before applying our own bound: truncating a known secret
            # first would turn it into an unrecognizable, leaked prefix.
            text = content.decode(encoding, "replace")
            if upstream_truncated:
                # Incomplete input cannot establish that the final token/line
                # is harmless. Exclude that fragment from ordinary retention.
                text = text.rsplit("\n", 1)[0] + "\n" if "\n" in text else ""
                begin, end = text.rfind("-----BEGIN "), text.rfind("-----END ")
                if begin > end and "PRIVATE KEY-----" in text[begin:].splitlines()[0]:
                    text = text[:begin] + "[REDACTED]"
            clean = self.redactor.text(text, truncated=upstream_truncated).encode(encoding)
            stored = clean[: self.max_retained_bytes]
            truncated = upstream_truncated or len(clean) > len(stored)
        else:
            stored = content[: self.max_retained_bytes]
            truncated = upstream_truncated or len(content) > len(stored)
        digest = _sha256(stored)
        self._atomic_write(self.path_for(digest), stored)
        return ArtifactRef(
            digest=digest,
            stored_bytes=len(stored),
            source_sha256=original_digest,
            source_bytes=original_bytes,
            media_type=media_type,
            encoding=encoding if redact or media_type.startswith("text/") else "binary",
            redacted=redact,
            truncated=truncated,
            truncation_policy="head:{}".format(self.max_retained_bytes),
            producer=dict(producer),
            created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        )

    def put_file(
        self,
        path: Path,
        *,
        producer: Mapping[str, str],
        media_type: str = "application/octet-stream",
        redact: bool = False,
    ) -> ArtifactRef:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise ArtifactError("artifact source must be a regular non-symlink file")
        digest = hashlib.sha256()
        retained = bytearray()
        total = 0
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                if len(retained) < self.max_retained_bytes:
                    retained.extend(chunk[: self.max_retained_bytes - len(retained)])
        return self.put_bytes(
            bytes(retained),
            producer=producer,
            media_type=media_type,
            redact=redact,
            source_sha256="sha256:" + digest.hexdigest(),
            source_bytes=total,
            truncated=total > len(retained),
        )

    def read(self, reference: ArtifactRef) -> bytes:
        path = self.path_for(reference.digest)
        if path.is_symlink() or not path.is_file():
            raise ArtifactError("artifact blob is missing")
        content = path.read_bytes()
        if len(content) != reference.stored_bytes or _sha256(content) != reference.digest:
            raise ArtifactError("artifact blob failed hash or size verification")
        return content

    def verify(self, reference: ArtifactRef) -> None:
        self.read(reference)


def artifact_refs(value: Any) -> Tuple[ArtifactRef, ...]:
    """Find strict artifact references recursively in an event payload."""
    found: List[ArtifactRef] = []
    if isinstance(value, dict):
        if value.get("schema") == ArtifactRef.SCHEMA:
            found.append(ArtifactRef.from_dict(value))
        else:
            for item in value.values():
                found.extend(artifact_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(artifact_refs(item))
    return tuple(found)


class RunArchive:
    """Bounded event/ordinary-blob export, not a complete recovery backup.

    Hashes establish integrity, not authorship or permission to restore/run.
    Git objects, raw salvage and process authority are not included.
    """

    MANIFEST_SCHEMA = "camol.run_archive"
    MANIFEST_VERSION = 1
    MAX_MANIFEST_BYTES = 4 << 20
    MAX_EVENT_BYTES = 1 << 20
    MAX_EVENT_TOTAL = 32 << 20
    MAX_EVENTS = 20000
    MAX_LINEAGE = 256
    MAX_BLOBS = 10000
    MAX_BLOB_BYTES = 8 << 20
    MAX_BLOB_TOTAL = 64 << 20
    MAX_JSON_NODES = 1000000

    @classmethod
    def _json(cls, raw, maximum, budget=None):
        value = decode_contract(raw, max_bytes=maximum)
        pending = [(value, 0)]
        budget = [0] if budget is None else budget
        while pending:
            item, depth = pending.pop()
            budget[0] += 1
            if budget[0] > cls.MAX_JSON_NODES or depth > 64:
                raise ArtifactError("archive JSON exceeds its structural ceiling")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value

    @staticmethod
    def _lineage_path(identifier):
        require_identifier(identifier, "lineage run ID")
        if "/" in identifier or "\\" in identifier or identifier in {".", ".."}:
            raise ArtifactError("archive v2 lineage ID must be a single safe filename component")
        return "lineage/" + identifier + ".jsonl"

    @classmethod
    def _stream(cls, encoded, identifier, count, budget=None):
        require_identifier(identifier, "archive run ID")
        if type(count) is not int or not 0 <= count <= cls.MAX_EVENTS:
            raise ArtifactError("archive event count exceeds its supported bound")
        events = []
        for line in encoded.splitlines():
            if not line:
                continue
            if len(events) >= count:
                raise ArtifactError("archive event count is invalid")
            event = cls._json(line, cls.MAX_EVENT_BYTES, budget)
            if not isinstance(event, dict) or event.get("run_id") != identifier:
                raise ArtifactError("archive event identity or object shape is invalid")
            events.append(event)
        if len(events) != count:
            raise ArtifactError("archive event count is invalid")
        return events

    @classmethod
    def _encode_stream(cls, events, identifier, *, maximum=None, budget=None):
        require_identifier(identifier, "archive run ID")
        maximum = cls.MAX_EVENT_TOTAL if maximum is None else maximum
        if len(events) > cls.MAX_EVENTS:
            raise ArtifactError("archive event count exceeds its supported bound")
        chunks, normalized, total = [], [], 0
        for event in events:
            # Canonical validation also rejects ambiguous keys, cycles and NaN.
            canonical_digest(event)
            raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
            normalized.extend(cls._stream(raw, identifier, 1, budget))
            total += len(raw) + 1
            if total > maximum:
                raise ArtifactError("archive events exceed their total byte ceiling")
            chunks.append(raw + b"\n")
        return b"".join(chunks), normalized

    @classmethod
    def export(
        cls,
        run_id: str,
        events: Sequence[Dict[str, Any]],
        store: ArtifactStore,
        destination: Path,
        *,
        lineage_events: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        try:
            return cls._export(run_id, events, store, destination, lineage_events=lineage_events)
        except (OSError, ValueError, RecursionError, TypeError) as error:
            raise ArtifactError("run archive export refused: invalid, changed, unsafe or oversized input") from error

    @classmethod
    def _export(cls, run_id, events, store, destination, *, lineage_events=None):
        from .revisions import RevisionError, verify_revision_lineage
        lineage = dict(lineage_events or {})
        if len(lineage) > cls.MAX_LINEAGE or len(events) + sum(len(values) for values in lineage.values()) > cls.MAX_EVENTS:
            raise ArtifactError("archive lineage or total event count exceeds its ceiling")
        budget = [0]
        encoded_events, events = cls._encode_stream(events, run_id, budget=budget)
        encoded_lineage, frozen_lineage, total = {}, {}, len(encoded_events)
        for identifier, values in lineage.items():
            cls._lineage_path(identifier)
            encoded, normalized = cls._encode_stream(values, identifier,
                                                      maximum=cls.MAX_EVENT_TOTAL - total, budget=budget)
            encoded_lineage[identifier] = encoded
            frozen_lineage[identifier] = normalized
            total += len(encoded)
        lineage = frozen_lineage
        if lineage or any(event.get("type") == "REVISION_LINKED" for event in events):
            try:
                verify_revision_lineage(list(events), lineage)
            except (ValueError, RevisionError) as error:
                raise ArtifactError("revision archive needs its verified complete source lineage") from error
        all_events = list(events) + [event for values in lineage.values() for event in values]
        if store.redactor.value(all_events) != all_events:
            raise ArtifactError("event ledger contains material requiring redaction; export refused")
        all_references = [reference for event in all_events for reference in artifact_refs(event)]
        references = {reference.digest: reference for reference in all_references}
        if len(references) > cls.MAX_BLOBS or sum(ref.stored_bytes for ref in references.values()) > cls.MAX_BLOB_TOTAL:
            raise ArtifactError("archive blob inventory exceeds its ceiling")
        for reference in all_references:
            if reference.stored_bytes > cls.MAX_BLOB_BYTES or references[reference.digest].stored_bytes != reference.stored_bytes:
                raise ArtifactError("archive blob size verification failed")
        core = {
            "schema": cls.MANIFEST_SCHEMA,
            "schema_version": cls.MANIFEST_VERSION,
            "run_id": run_id,
            "event_count": len(events),
            "events_sha256": _sha256(encoded_events),
            "artifact_digests": sorted(references),
        }
        if lineage:
            inventory = {}
            for identifier, values in sorted(lineage.items()):
                encoded = encoded_lineage[identifier]
                inventory[identifier] = {"event_count": len(values), "events_sha256": _sha256(encoded)}
            core["schema_version"] = 2
            core["lineage"] = inventory
        manifest = dict(core, manifest_digest=canonical_digest(core))
        encoded_manifest = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        cls._json(encoded_manifest, cls.MAX_MANIFEST_BYTES, budget)
        with ArchiveRoot(store.state_dir) as source, ArchiveRoot(destination, create=True) as target:
            for digest, reference in references.items():
                hexadecimal = digest.split(":", 1)[1]
                suffix = "sha256/" + hexadecimal[:2] + "/" + hexadecimal[2:]
                content = source.read("artifacts/" + suffix, cls.MAX_BLOB_BYTES)
                if _sha256(content) != digest or len(content) != reference.stored_bytes:
                    raise ArtifactError("archive source blob verification failed")
                target.write("blobs/" + suffix, content)
            target.write("events.jsonl", encoded_events)
            for identifier, encoded in encoded_lineage.items():
                target.write(cls._lineage_path(identifier), encoded)
            source.verify()
            target.write("manifest.json", encoded_manifest)
        return manifest

    @classmethod
    def verify(cls, source: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        try:
            with ArchiveRoot(source) as root:
                return cls._verify_root(root)
        except (OSError, ValueError, RecursionError, TypeError) as error:
            raise ArtifactError("run archive verification refused: invalid, changed, unsafe or oversized input") from error

    @classmethod
    def _verify_root(cls, root):
        budget = [0]
        manifest = cls._json(root.read("manifest.json", cls.MAX_MANIFEST_BYTES), cls.MAX_MANIFEST_BYTES, budget)
        if not isinstance(manifest, dict):
            raise ArtifactError("run archive manifest must be an object")
        encoded_events = root.read("events.jsonl", cls.MAX_EVENT_TOTAL)
        expected_fields = {
            "schema", "schema_version", "run_id", "event_count", "events_sha256",
            "artifact_digests", "manifest_digest",
        }
        if manifest.get("schema_version") == 2:
            expected_fields.add("lineage")
        if set(manifest) != expected_fields or manifest.get("schema") != cls.MANIFEST_SCHEMA or type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in {1, 2}:
            raise ArtifactError("run archive manifest is invalid")
        core = {name: value for name, value in manifest.items() if name != "manifest_digest"}
        if canonical_digest(core) != manifest["manifest_digest"] or _sha256(encoded_events) != manifest["events_sha256"]:
            raise ArtifactError("run archive manifest or event stream digest is invalid")
        events = cls._stream(encoded_events, manifest["run_id"], manifest["event_count"], budget)
        total_event_bytes, total_events = len(encoded_events), len(events)
        lineage = {}
        inventory = manifest.get("lineage", {})
        if not isinstance(inventory, dict) or len(inventory) > cls.MAX_LINEAGE:
            raise ArtifactError("run archive lineage inventory is invalid")
        for identifier, record in inventory.items():
            if not isinstance(record, dict) or set(record) != {"event_count", "events_sha256"}:
                raise ArtifactError("run archive lineage entry is invalid")
            encoded = root.read(cls._lineage_path(identifier), cls.MAX_EVENT_TOTAL - total_event_bytes)
            total_event_bytes += len(encoded)
            values = cls._stream(encoded, identifier, record["event_count"], budget)
            total_events += len(values)
            if total_events > cls.MAX_EVENTS:
                raise ArtifactError("archive total event count exceeds its ceiling")
            if _sha256(encoded) != record["events_sha256"]:
                raise ArtifactError("run archive lineage hash, count, or identity is invalid")
            lineage[identifier] = values
        if lineage or any(event.get("type") == "REVISION_LINKED" for event in events):
            from .revisions import verify_revision_lineage
            try:
                verify_revision_lineage(events, lineage)
            except ValueError as error:
                raise ArtifactError("run archive lineage is not replayable") from error
        all_events = list(events) + [event for values in lineage.values() for event in values]
        all_references = [reference for event in all_events for reference in artifact_refs(event)]
        references = {reference.digest: reference for reference in all_references}
        if len(references) > cls.MAX_BLOBS or sum(ref.stored_bytes for ref in references.values()) > cls.MAX_BLOB_TOTAL:
            raise ArtifactError("archive blob inventory exceeds its ceiling")
        discovered = sorted(references)
        if discovered != manifest["artifact_digests"]:
            raise ArtifactError("run archive artifact inventory is invalid")
        for reference in all_references:
            if reference.stored_bytes > cls.MAX_BLOB_BYTES or reference.stored_bytes != references[reference.digest].stored_bytes:
                raise ArtifactError("archive artifact size verification failed")
        for reference in references.values():
            digest = reference.digest
            hexadecimal = digest.split(":", 1)[1]
            content = root.read("blobs/sha256/" + hexadecimal[:2] + "/" + hexadecimal[2:], cls.MAX_BLOB_BYTES)
            if (
                content is None
                or _sha256(content) != digest
                or len(content) != reference.stored_bytes
            ):
                raise ArtifactError("run archive artifact is missing or corrupt: {}".format(digest))
        return manifest, events

    @classmethod
    def replay(cls, source: Path) -> Dict[str, Any]:
        """Verify an archive and rebuild its authoritative projection."""
        from .state import project

        _, events = cls.verify(source)
        return project(events)
