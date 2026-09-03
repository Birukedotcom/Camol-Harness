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
        retained = content[: self.max_retained_bytes]
        truncated = truncated or len(content) > len(retained) or original_bytes > len(retained)
        if redact:
            text = retained.decode(encoding, "replace")
            stored = self.redactor.text(text).encode(encoding)
        else:
            stored = retained
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
    """Export and verify a self-contained event stream plus referenced blobs."""

    MANIFEST_SCHEMA = "camol.run_archive"
    MANIFEST_VERSION = 1

    @classmethod
    def export(
        cls,
        run_id: str,
        events: Sequence[Dict[str, Any]],
        store: ArtifactStore,
        destination: Path,
    ) -> Dict[str, Any]:
        target = Path(destination)
        if target.is_symlink() or (target.exists() and any(target.iterdir())):
            raise ArtifactError("archive destination must be absent or an empty real directory")
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ArtifactError("archive destination must not be a symlink")
        if any(event.get("run_id") != run_id for event in events):
            raise ArtifactError("archive events must all belong to the selected run")
        if store.redactor.value(list(events)) != list(events):
            raise ArtifactError("event ledger contains material requiring redaction; export refused")
        encoded_events = b"".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for event in events
        )
        references = {reference.digest: reference for event in events for reference in artifact_refs(event)}
        for digest, reference in references.items():
            content = store.read(reference)
            hexadecimal = digest.split(":", 1)[1]
            output = target / "blobs" / "sha256" / hexadecimal[:2] / hexadecimal[2:]
            cls._write_export_file(output, content)
        cls._write_export_file(target / "events.jsonl", encoded_events)
        core = {
            "schema": cls.MANIFEST_SCHEMA,
            "schema_version": cls.MANIFEST_VERSION,
            "run_id": run_id,
            "event_count": len(events),
            "events_sha256": _sha256(encoded_events),
            "artifact_digests": sorted(references),
        }
        manifest = dict(core, manifest_digest=canonical_digest(core))
        cls._write_export_file(
            target / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return manifest

    @staticmethod
    def _write_export_file(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            raise ArtifactError("archive output already exists: {}".format(path))
        with path.open("xb") as handle:
            handle.write(content)

    @classmethod
    def verify(cls, source: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        root = Path(source)
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            encoded_events = (root / "events.jsonl").read_bytes()
        except (OSError, json.JSONDecodeError) as error:
            raise ArtifactError("run archive manifest or event stream is unreadable") from error
        expected_fields = {
            "schema", "schema_version", "run_id", "event_count", "events_sha256",
            "artifact_digests", "manifest_digest",
        }
        if set(manifest) != expected_fields or manifest.get("schema") != cls.MANIFEST_SCHEMA or manifest.get("schema_version") != cls.MANIFEST_VERSION:
            raise ArtifactError("run archive manifest is invalid")
        core = {name: value for name, value in manifest.items() if name != "manifest_digest"}
        if canonical_digest(core) != manifest["manifest_digest"] or _sha256(encoded_events) != manifest["events_sha256"]:
            raise ArtifactError("run archive manifest or event stream digest is invalid")
        try:
            events = [json.loads(line) for line in encoded_events.splitlines() if line]
        except json.JSONDecodeError as error:
            raise ArtifactError("run archive event stream contains invalid JSON") from error
        if len(events) != manifest["event_count"] or any(event.get("run_id") != manifest["run_id"] for event in events):
            raise ArtifactError("run archive event count or run identity is invalid")
        references = {
            reference.digest: reference
            for event in events
            for reference in artifact_refs(event)
        }
        discovered = sorted(references)
        if discovered != manifest["artifact_digests"]:
            raise ArtifactError("run archive artifact inventory is invalid")
        for digest in discovered:
            hexadecimal = digest.split(":", 1)[1]
            blob = root / "blobs" / "sha256" / hexadecimal[:2] / hexadecimal[2:]
            content = blob.read_bytes() if blob.is_file() and not blob.is_symlink() else None
            if (
                content is None
                or _sha256(content) != digest
                or len(content) != references[digest].stored_bytes
            ):
                raise ArtifactError("run archive artifact is missing or corrupt: {}".format(digest))
        return manifest, events

    @classmethod
    def replay(cls, source: Path) -> Dict[str, Any]:
        """Verify an archive and rebuild its authoritative projection."""
        from .state import project

        _, events = cls.verify(source)
        return project(events)
