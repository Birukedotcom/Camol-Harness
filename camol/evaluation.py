"""Frozen evaluator bundles, candidate identity, and integration receipts."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from .schema import (
    canonical_digest,
    reject_unknown_fields,
    require_digest,
    require_identifier,
    require_non_negative_int,
    require_schema_header,
    require_string,
    require_timestamp,
)
from .readiness import WorkspaceReceipt
from .workspace import SalvageReceipt


class EvaluationError(RuntimeError):
    """Evaluator material is missing, mutable, or incorrectly bound."""


class _FrozenDict(dict):
    """A JSON-compatible mapping that rejects every mutation surface."""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen evaluator data cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def evaluator_definition(runbook: Dict[str, Any]) -> Dict[str, Any]:
    """Return the human-approved normative evaluator surface."""
    return {
        "schema": "camol.evaluator_definition",
        "schema_version": 1,
        "run_id": runbook["run"]["id"],
        "completion": list(runbook["run"]["completion"]),
        "rules": [dict(rule) for rule in runbook["rules"]],
        "tasks": [
            {
                "id": task["id"],
                "acceptance": list(task["acceptance"]),
                "required_evidence": list(task["required_evidence"]),
                "verification": [
                    {"purpose": command["purpose"], "argv": list(command["argv"])}
                    for command in task["verification"]
                ],
                "evaluator_assets": list(task.get("evaluator_assets", [])),
            }
            for task in runbook["tasks"]
        ],
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _asset_records(runbook: Dict[str, Any], workspace: Path) -> Tuple[Dict[str, Any], ...]:
    root = Path(workspace).resolve()
    declared = sorted({path for task in runbook["tasks"] for path in task.get("evaluator_assets", [])})
    paths = []
    for value in declared:
        lexical = root / value
        if lexical.is_symlink() or not _inside(lexical, root):
            raise EvaluationError("evaluator asset escapes the source repository: {}".format(value))
        if lexical.is_dir():
            for candidate in sorted(lexical.rglob("*")):
                if ".git" in candidate.parts or candidate.is_symlink():
                    if candidate.is_symlink():
                        raise EvaluationError("evaluator assets cannot contain symlinks: {}".format(candidate))
                    continue
                if candidate.is_file():
                    paths.append(candidate)
        elif lexical.is_file():
            paths.append(lexical)
        else:
            raise EvaluationError("evaluator asset does not exist: {}".format(value))
    records = []
    seen = set()
    for path in sorted(paths):
        relative = path.resolve().relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        content = path.read_bytes()
        records.append({
            "path": relative,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        })
    return tuple(records)


def evaluator_identity(runbook: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    return {
        "definition": evaluator_definition(runbook),
        "assets": [dict(item) for item in _asset_records(runbook, workspace)],
    }


def evaluator_digest(runbook: Dict[str, Any], workspace: Path) -> str:
    return canonical_digest(evaluator_identity(runbook, workspace))


@dataclass(frozen=True)
class EvaluatorBundle:
    SCHEMA = "camol.evaluator_bundle"
    SCHEMA_VERSION = 1

    run_id: str
    plan_digest: str
    evaluator_digest: str
    definition: Dict[str, Any]
    assets: Tuple[Dict[str, Any], ...]
    compiled_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_identifier(self.run_id, "evaluator run id"))
        object.__setattr__(self, "plan_digest", require_digest(self.plan_digest, "evaluator plan digest"))
        object.__setattr__(self, "evaluator_digest", require_digest(self.evaluator_digest, "evaluator digest"))
        if not isinstance(self.definition, dict):
            raise EvaluationError("evaluator definition must be an object")
        if not isinstance(self.assets, tuple):
            raise EvaluationError("evaluator assets must be a tuple")
        normalized = []
        seen = set()
        for item in self.assets:
            if not isinstance(item, dict) or set(item) != {"path", "digest", "bytes"}:
                raise EvaluationError("evaluator asset record is malformed")
            path = require_string(item["path"], "evaluator asset path")
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or path in seen:
                raise EvaluationError("evaluator asset path must be unique and workspace-relative")
            seen.add(path)
            normalized.append({
                "path": path,
                "digest": require_digest(item["digest"], "evaluator asset digest"),
                "bytes": require_non_negative_int(item["bytes"], "evaluator asset bytes"),
            })
        if [item["path"] for item in normalized] != sorted(seen):
            raise EvaluationError("evaluator asset records must be sorted")
        object.__setattr__(self, "definition", _freeze_json(self.definition))
        object.__setattr__(self, "assets", tuple(_freeze_json(item) for item in normalized))
        object.__setattr__(self, "compiled_at", require_timestamp(self.compiled_at, "evaluator compiled_at"))
        if canonical_digest({"definition": self.definition, "assets": list(self.assets)}) != self.evaluator_digest:
            raise EvaluationError("evaluator digest does not bind its definition and assets")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "plan_digest": self.plan_digest,
            "evaluator_digest": self.evaluator_digest,
            "definition": _thaw_json(self.definition),
            "assets": [dict(item) for item in self.assets],
            "compiled_at": self.compiled_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluatorBundle":
        fields = (
            "schema", "schema_version", "run_id", "plan_digest", "evaluator_digest",
            "definition", "assets", "compiled_at",
        )
        if not isinstance(payload, dict):
            raise EvaluationError("evaluator bundle must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "evaluator bundle")
        reject_unknown_fields(payload, fields, "evaluator bundle")
        missing = sorted(set(fields) - set(payload))
        if missing or not isinstance(payload.get("assets"), list):
            raise EvaluationError("evaluator bundle is missing or has malformed fields")
        return cls(
            run_id=payload["run_id"], plan_digest=payload["plan_digest"],
            evaluator_digest=payload["evaluator_digest"], definition=payload["definition"],
            assets=tuple(payload["assets"]), compiled_at=payload["compiled_at"],
        )


class EvaluatorStore:
    """Content-addressed evaluator material outside every builder worktree."""

    def __init__(self, state_dir: Path):
        self.root = Path(state_dir).resolve() / "evaluators"

    @staticmethod
    def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[7:9] / digest[7:]

    def compile(self, runbook: Dict[str, Any], workspace: Path, plan_digest: str) -> EvaluatorBundle:
        identity = evaluator_identity(runbook, workspace)
        digest = canonical_digest(identity)
        bundle = EvaluatorBundle(
            run_id=runbook["run"]["id"], plan_digest=plan_digest,
            evaluator_digest=digest, definition=identity["definition"],
            assets=tuple(identity["assets"]),
            compiled_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        )
        for item in bundle.assets:
            content = (Path(workspace).resolve() / item["path"]).read_bytes()
            path = self._blob_path(item["digest"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != item["digest"]:
                    raise EvaluationError("stored evaluator asset blob is corrupt")
                continue
            descriptor, temporary = tempfile.mkstemp(prefix="asset.", dir=str(path.parent))
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(temporary_path), str(path))
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        record = self.root / bundle.run_id / (bundle.evaluator_digest[7:] + ".json")
        if record.exists():
            existing = EvaluatorBundle.from_dict(json.loads(record.read_text(encoding="utf-8")))
            if existing.to_dict() != bundle.to_dict():
                # Compilation time is non-normative; preserve the first record.
                if {**existing.to_dict(), "compiled_at": bundle.compiled_at} != bundle.to_dict():
                    raise EvaluationError("existing evaluator bundle conflicts with the approved identity")
            return existing
        self._atomic_json(record, bundle.to_dict())
        return bundle

    def load(self, run_id: str, digest: str) -> EvaluatorBundle:
        path = self.root / require_identifier(run_id, "evaluator run id") / (require_digest(digest, "evaluator digest")[7:] + ".json")
        if path.is_symlink() or not path.is_file():
            raise EvaluationError("frozen evaluator bundle is missing")
        bundle = EvaluatorBundle.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if bundle.evaluator_digest != digest:
            raise EvaluationError("frozen evaluator bundle has the wrong identity")
        for item in bundle.assets:
            blob = self._blob_path(item["digest"])
            if blob.is_symlink() or not blob.is_file():
                raise EvaluationError("frozen evaluator asset blob is missing")
            content = blob.read_bytes()
            if len(content) != item["bytes"] or "sha256:" + hashlib.sha256(content).hexdigest() != item["digest"]:
                raise EvaluationError("frozen evaluator asset blob is corrupt")
        return bundle

    def verify_assets(self, bundle: EvaluatorBundle, workspace: Path) -> None:
        root = Path(workspace).resolve()
        for item in bundle.assets:
            candidate = root / item["path"]
            if candidate.is_symlink() or not candidate.is_file() or not _inside(candidate, root):
                raise EvaluationError("candidate removed or redirected frozen evaluator asset {}".format(item["path"]))
            content = candidate.read_bytes()
            if len(content) != item["bytes"] or "sha256:" + hashlib.sha256(content).hexdigest() != item["digest"]:
                raise EvaluationError("candidate changed frozen evaluator asset {}".format(item["path"]))


@dataclass(frozen=True)
class CandidateRecord:
    SCHEMA = "camol.candidate_record"
    SCHEMA_VERSION = 1

    candidate_id: str
    run_id: str
    task_id: str
    agent_id: str
    lease_id: str
    fence_digest: str
    evaluator_digest: str
    salvage: SalvageReceipt
    captured_at: str

    def __post_init__(self) -> None:
        for name in ("candidate_id", "run_id", "task_id", "agent_id", "lease_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "candidate " + name))
        object.__setattr__(self, "fence_digest", require_digest(self.fence_digest, "candidate fence digest"))
        object.__setattr__(self, "evaluator_digest", require_digest(self.evaluator_digest, "candidate evaluator digest"))
        if not isinstance(self.salvage, SalvageReceipt):
            raise EvaluationError("candidate salvage must be a SalvageReceipt")
        object.__setattr__(self, "captured_at", require_timestamp(self.captured_at, "candidate captured_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
            "candidate_id": self.candidate_id, "run_id": self.run_id,
            "task_id": self.task_id, "agent_id": self.agent_id, "lease_id": self.lease_id,
            "fence_digest": self.fence_digest, "evaluator_digest": self.evaluator_digest,
            "salvage": self.salvage.to_dict(), "salvage_digest": self.salvage.digest(),
            "captured_at": self.captured_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateRecord":
        fields = (
            "schema", "schema_version", "candidate_id", "run_id", "task_id", "agent_id",
            "lease_id", "fence_digest", "evaluator_digest", "salvage", "salvage_digest", "captured_at",
        )
        if not isinstance(payload, dict):
            raise EvaluationError("candidate record must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "candidate record")
        reject_unknown_fields(payload, fields, "candidate record")
        if set(payload) != set(fields):
            raise EvaluationError("candidate record is missing fields")
        salvage = SalvageReceipt.from_dict(payload["salvage"])
        if salvage.digest() != require_digest(payload["salvage_digest"], "candidate salvage digest"):
            raise EvaluationError("candidate salvage digest is invalid")
        return cls(
            candidate_id=payload["candidate_id"], run_id=payload["run_id"], task_id=payload["task_id"],
            agent_id=payload["agent_id"], lease_id=payload["lease_id"],
            fence_digest=payload["fence_digest"], evaluator_digest=payload["evaluator_digest"],
            salvage=salvage, captured_at=payload["captured_at"],
        )


@dataclass(frozen=True)
class IntegrationReceipt:
    SCHEMA = "camol.integration_receipt"
    SCHEMA_VERSION = 1

    integration_id: str
    run_id: str
    task_id: str
    candidate_id: str
    evaluator_digest: str
    revision: str
    workspace: WorkspaceReceipt
    checks_digest: str
    accepted_at: str

    def __post_init__(self) -> None:
        for name in ("integration_id", "run_id", "task_id", "candidate_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "integration " + name))
        object.__setattr__(self, "evaluator_digest", require_digest(self.evaluator_digest, "integration evaluator digest"))
        object.__setattr__(self, "revision", require_string(self.revision, "integration revision"))
        if not isinstance(self.workspace, WorkspaceReceipt):
            raise EvaluationError("integration workspace must be a WorkspaceReceipt")
        object.__setattr__(self, "checks_digest", require_digest(self.checks_digest, "integration checks digest"))
        object.__setattr__(self, "accepted_at", require_timestamp(self.accepted_at, "integration accepted_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
            "integration_id": self.integration_id, "run_id": self.run_id,
            "task_id": self.task_id, "candidate_id": self.candidate_id,
            "evaluator_digest": self.evaluator_digest, "revision": self.revision,
            "workspace": self.workspace.to_dict(), "workspace_digest": self.workspace.digest(),
            "checks_digest": self.checks_digest, "accepted_at": self.accepted_at,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IntegrationReceipt":
        fields = (
            "schema", "schema_version", "integration_id", "run_id", "task_id", "candidate_id",
            "evaluator_digest", "revision", "workspace", "workspace_digest", "checks_digest", "accepted_at",
        )
        if not isinstance(payload, dict):
            raise EvaluationError("integration receipt must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "integration receipt")
        reject_unknown_fields(payload, fields, "integration receipt")
        if set(payload) != set(fields):
            raise EvaluationError("integration receipt is missing fields")
        workspace = WorkspaceReceipt.from_dict(payload["workspace"])
        if workspace.digest() != require_digest(payload["workspace_digest"], "integration workspace digest"):
            raise EvaluationError("integration workspace digest is invalid")
        return cls(
            integration_id=payload["integration_id"], run_id=payload["run_id"],
            task_id=payload["task_id"], candidate_id=payload["candidate_id"],
            evaluator_digest=payload["evaluator_digest"], revision=payload["revision"],
            workspace=workspace, checks_digest=payload["checks_digest"], accepted_at=payload["accepted_at"],
        )


@dataclass(frozen=True)
class CounterexampleRecord:
    SCHEMA = "camol.counterexample"
    SCHEMA_VERSION = 1

    counterexample_id: str
    run_id: str
    task_id: str
    candidate_id: str
    evaluator_digest: str
    phase: str
    checks_digest: str
    summary: str
    recorded_at: str

    def __post_init__(self) -> None:
        for name in ("counterexample_id", "run_id", "task_id", "candidate_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "counterexample " + name))
        if self.phase not in {"candidate", "integration"}:
            raise EvaluationError("counterexample phase is invalid")
        object.__setattr__(self, "evaluator_digest", require_digest(self.evaluator_digest, "counterexample evaluator digest"))
        object.__setattr__(self, "checks_digest", require_digest(self.checks_digest, "counterexample checks digest"))
        object.__setattr__(self, "summary", require_string(self.summary, "counterexample summary"))
        object.__setattr__(self, "recorded_at", require_timestamp(self.recorded_at, "counterexample recorded_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
            "counterexample_id": self.counterexample_id, "run_id": self.run_id,
            "task_id": self.task_id, "candidate_id": self.candidate_id,
            "evaluator_digest": self.evaluator_digest, "phase": self.phase,
            "checks_digest": self.checks_digest, "summary": self.summary,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CounterexampleRecord":
        fields = (
            "schema", "schema_version", "counterexample_id", "run_id", "task_id", "candidate_id",
            "evaluator_digest", "phase", "checks_digest", "summary", "recorded_at",
        )
        if not isinstance(payload, dict):
            raise EvaluationError("counterexample must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "counterexample")
        reject_unknown_fields(payload, fields, "counterexample")
        if set(payload) != set(fields):
            raise EvaluationError("counterexample is missing fields")
        return cls(**{key: payload[key] for key in fields if key not in {"schema", "schema_version"}})
