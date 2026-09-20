"""Bounded, read-only repository graphs with inspectable static evidence.

Scanners parse text and syntax; they never import project code or run build tools.
Static declarations cannot establish runtime readiness or deployment success.
"""

import ast
import fnmatch
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from xml.etree import ElementTree

from .probes import GIT_SAFETY_ARGS, Redactor, sanitized_environment
from .git_safety import safe_git_argv
from .archive_io import ArchiveRoot, ArchiveIOError
from .preflight_process import bounded_preflight_run
from .schema import canonical_digest


class GraphError(ValueError):
    pass


@dataclass(frozen=True)
class CrawlPolicy:
    max_files: int = 10000
    max_file_bytes: int = 1 << 20
    max_total_bytes: int = 32 << 20
    excludes: Tuple[str, ...] = ("node_modules/*", ".venv/*", "venv/*", "dist/*", "build/*", "__pycache__/*")
    allow_hardlinked_source: bool = False

    def __post_init__(self):
        for name in ("max_files", "max_file_bytes", "max_total_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise GraphError(name + " must be a positive integer")
        if not isinstance(self.excludes, tuple) or any(not isinstance(value, str) for value in self.excludes):
            raise GraphError("excludes must be a tuple of relative glob strings")
        if type(self.allow_hardlinked_source) is not bool:
            raise GraphError("allow_hardlinked_source must be an explicit boolean")


@dataclass(frozen=True)
class RepositoryInventory:
    root: Path
    revision: str
    files: Mapping[str, bytes]
    warnings: Tuple[str, ...]
    dirty_digest: str


@dataclass
class GraphFragment:
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class RepositoryScanner(Protocol):
    name: str
    version: str
    def supports(self, inventory: RepositoryInventory) -> bool: ...
    def scan(self, context: "ScanContext") -> GraphFragment: ...


def _identity(kind: str, locator: str) -> str:
    return kind + ":" + hashlib.sha256(locator.encode()).hexdigest()[:24]


class ScanContext:
    def __init__(self, inventory: RepositoryInventory, scanner: str):
        self.inventory, self.scanner = inventory, scanner
        self.fragment = GraphFragment()

    def node(self, kind: str, locator: str, *, name: Optional[str] = None,
             layer: str = "source", attributes: Optional[dict] = None, status: str = "OBSERVED") -> str:
        locator = Redactor().text(locator)
        name = Redactor().text(name) if name is not None else None
        identity = _identity(kind, locator)
        self.fragment.nodes.append({
            "node_id": identity, "kind": kind, "locator": locator,
            "display_name": name or locator, "layer": layer,
            "attributes": attributes or {}, "evidence_status": status,
            "freshness_policy": "source_snapshot_only",
        })
        return identity

    def evidence(self, path: str, *, line: Optional[int] = None, basis: str = "manifest") -> str:
        source = self.inventory.files.get(path)
        record = {
            "path": path, "source_digest": "sha256:" + hashlib.sha256(source).hexdigest() if source is not None else None,
            "line": line, "basis": basis, "scanner": self.scanner,
            "source_revision": self.inventory.revision,
        }
        identity = _identity("evidence", json.dumps(record, sort_keys=True))
        self.fragment.evidence.append(dict(record, evidence_id=identity))
        return identity

    def edge(self, source: str, target: str, kind: str, evidence: str, *, status: str = "OBSERVED") -> None:
        self.fragment.edges.append({
            "edge_id": _identity("edge", "|".join((source, target, kind))),
            "source": source, "target": target, "kind": kind,
            "evidence_status": status, "evidence_refs": [evidence], "attributes": {},
        })

    def file_node(self, path: str) -> str:
        kind = "test" if Path(path).name.startswith("test_") or "/tests/" in "/" + path else "module"
        return self.node(kind, "file:" + path, name=path)


def _git(root: Path, *arguments: str) -> bytes:
    # A repository may be on PATH, including via an empty/relative entry. Only
    # installed system plumbing is eligible for this read-only observation.
    binary = shutil.which("git", path="/usr/bin:/bin")
    if not binary:
        raise GraphError("a trusted system Git is required for repository inventory")
    try:
        with tempfile.TemporaryDirectory(prefix="camol-graph-git-") as scratch:
            env = sanitized_environment({"PATH": "/usr/bin:/bin", "HOME": scratch})
            env.update(GIT_NO_LAZY_FETCH="1", GIT_LFS_SKIP_SMUDGE="1", GIT_ALLOW_PROTOCOL="")
            completed = bounded_preflight_run(
                safe_git_argv(binary, ["-C", str(root), *arguments], env=env),
                cwd=scratch, env=env, timeout=30, max_output_bytes=32 << 20,
            )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise GraphError("Git inventory exceeded its bounds or could not run safely") from error
    if completed.returncode:
        raise GraphError("Git inventory could not read a repository root and revision")
    if len(completed.stdout) > 32 << 20:
        raise GraphError("Git inventory output exceeded its 32 MiB ceiling")
    return completed.stdout


def _excluded(path: str, policy: CrawlPolicy) -> bool:
    parts = Path(path).parts
    if any(part in {".git", ".ssh", ".aws", ".azure", ".config", "node_modules", "__pycache__"} for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    name = Path(path).name.lower()
    if name in {"credentials", "credentials.json", "auth.json", "secrets.json", "id_rsa", "id_ed25519", ".npmrc", ".pypirc"}:
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in policy.excludes)


class _SourceRoot(ArchiveRoot):
    """Read-only chosen project input; never used to read/write recovery archives."""
    def __init__(self, path, policy):
        self._source_policy = policy
        super().__init__(path)

    def _link_count_allowed(self, count):
        return count >= 1 if self._source_policy.allow_hardlinked_source else count == 1


def inventory_repository(root: Path, policy: CrawlPolicy) -> RepositoryInventory:
    root = Path(root).resolve()
    try:
        with _SourceRoot(root, policy) as reader:
            return _inventory_repository(root, policy, reader)
    except (OSError, ArchiveIOError) as error:
        raise GraphError("repository files changed or became unsafe during inventory") from error


def _inventory_repository(root: Path, policy: CrawlPolicy, reader: ArchiveRoot) -> RepositoryInventory:
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != root:
        raise GraphError("crawl root must be the Git repository root")
    revision = _git(root, "rev-parse", "HEAD^{commit}").decode().strip()
    candidates = sorted(set(_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8", "surrogateescape").split("\0")) - {""})
    files, warnings, total, skipped_hardlinks = {}, [], 0, 0
    if policy.allow_hardlinked_source:
        warnings.append("hard-linked source explicitly allowed; other aliases may exist outside the selected root")
    for relative in candidates:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            warnings.append("refused unsafe inventory path")
            continue
        if _excluded(relative, policy):
            continue
        if len(files) >= policy.max_files:
            warnings.append("file-count ceiling reached")
            break
        target = root / path
        try:
            cursor = root
            for part in path.parts:
                cursor = cursor / part
                if stat.S_ISLNK(cursor.lstat().st_mode):
                    raise GraphError("symlink skipped: " + relative)
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                warnings.append("non-regular source skipped: " + relative)
                continue
            if metadata.st_nlink > 1 and not policy.allow_hardlinked_source:
                skipped_hardlinks += 1
                continue
            if metadata.st_size > policy.max_file_bytes:
                warnings.append("per-file byte ceiling: " + relative)
                continue
            if total + metadata.st_size > policy.max_total_bytes:
                warnings.append("total byte ceiling reached")
                break
            content = reader.read(relative, min(policy.max_file_bytes, policy.max_total_bytes - total))
            if len(content) > policy.max_file_bytes or total + len(content) > policy.max_total_bytes:
                warnings.append("file grew beyond the byte ceiling: " + relative)
                continue
            total += len(content)
            files[relative] = content
        except (OSError, GraphError, ArchiveIOError) as error:
            warnings.append(str(error) if isinstance(error, GraphError) else "unsafe or unreadable source skipped: " + relative)
    if skipped_hardlinks:
        warnings.append("{} hard-linked source files skipped; review aliases before choosing --allow-hardlinked-source".format(skipped_hardlinks))
    dirty = canonical_digest({name: "sha256:" + hashlib.sha256(content).hexdigest() for name, content in files.items()})
    if _git(root, "rev-parse", "HEAD^{commit}").decode().strip() != revision:
        warnings.append("source revision changed during crawl")
    return RepositoryInventory(root, revision, files, tuple(warnings), dirty)


class GitScanner:
    name, version = "git-files", "1"
    def supports(self, inventory):
        return True
    def scan(self, context):
        repo = context.node("repo", "repo:.", name=context.inventory.root.name)
        for path in context.inventory.files:
            node = context.file_node(path)
            context.edge(repo, node, "contains", context.evidence(path, basis="git-inventory"))
        return context.fragment


class PythonScanner:
    name, version = "python-ast", "1"
    def supports(self, inventory):
        return any(path.endswith(".py") or Path(path).name == "pyproject.toml" for path in inventory.files)
    def scan(self, context):
        inventory = context.inventory
        modules = {}
        for path in inventory.files:
            if path.endswith(".py"):
                module = path[:-3].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[:-9]
                modules[module] = path
                if module.startswith("src."):
                    modules[module[4:]] = path
        for path, content in inventory.files.items():
            if Path(path).name == "pyproject.toml":
                try:
                    tomllib = None
                    for parser in ("tomllib", "tomli"):
                        spec = importlib.util.find_spec(parser)
                        if spec is None or not spec.origin:
                            continue
                        try:
                            Path(spec.origin).resolve().relative_to(inventory.root)
                        except ValueError:
                            tomllib = importlib.import_module(parser)
                            break
                    if tomllib is None:
                        raise ImportError("no trusted TOML parser")
                    value = tomllib.loads(content.decode("utf-8"))
                    project = value.get("project", {})
                    name = project.get("name")
                    if isinstance(name, str):
                        package = context.node("package", "python-project:" + path, name=name)
                        context.edge(context.file_node(path), package, "declares", context.evidence(path))
                        for dependency in project.get("dependencies", []):
                            if not isinstance(dependency, str):
                                continue
                            match = re.match(r"[A-Za-z0-9_.-]+", dependency)
                            if match:
                                other = context.node("package", "python:" + match.group(0), name=match.group(0))
                                context.edge(package, other, "requires", context.evidence(path))
                except (ImportError, ValueError, TypeError, AttributeError):
                    context.fragment.warnings.append("Python manifest not parsed (tomllib/tomli or valid TOML required): " + path)
                continue
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(content, filename=path)
            except (SyntaxError, ValueError, UnicodeError, RecursionError):
                context.fragment.warnings.append("Python syntax not parsed: " + path)
                continue
            source = context.file_node(path)
            own_module = path[:-3].replace("/", ".")
            own_package = own_module.rsplit(".", 1)[0] if "." in own_module else ""
            if own_module.endswith(".__init__"):
                own_package = own_module[:-9]
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                ):
                    context.fragment.warnings.append("dynamic Python import unresolved: " + path)
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        components = own_package.split(".") if own_package else []
                        if node.level > len(components) + 1:
                            context.fragment.warnings.append("relative Python import outside package: " + path)
                            continue
                        prefix = components[:len(components) - node.level + 1]
                        base = ".".join(prefix + ([base] if base else []))
                    names = [base + "." + alias.name for alias in node.names if alias.name != "*"]
                    names = [name if name in modules else base for name in names] or [base]
                else:
                    continue
                for name in sorted(set(names) - {""}):
                    target = context.file_node(modules[name]) if name in modules else context.node("package", "python:" + name.split(".")[0], name=name.split(".")[0], status="INFERRED")
                    context.edge(source, target, "imports", context.evidence(path, line=node.lineno, basis="python-ast"), status="DERIVED" if name in modules else "INFERRED")
        return context.fragment


class PackageJsonScanner:
    name, version = "package-json", "1"
    def supports(self, inventory):
        return any(Path(path).name == "package.json" for path in inventory.files)
    def scan(self, context):
        local_packages = {}
        for path, content in context.inventory.files.items():
            if Path(path).name == "package.json":
                try:
                    value = json.loads(content)
                    if isinstance(value, dict) and isinstance(value.get("name"), str):
                        local_packages[value["name"]] = path
                except (ValueError, UnicodeError):
                    pass
        for path, content in context.inventory.files.items():
            if Path(path).name != "package.json":
                continue
            try:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError()
                name = value.get("name", str(Path(path).parent))
                if not isinstance(name, str):
                    raise ValueError()
                source = context.node("package", "npm-project:" + path, name=name)
                context.edge(context.file_node(path), source, "declares", context.evidence(path))
                for category in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    dependencies = value.get(category, {})
                    if not isinstance(dependencies, dict):
                        raise ValueError()
                    for package in dependencies:
                        if not isinstance(package, str):
                            continue
                        target = context.node("package", "npm-project:" + local_packages[package] if package in local_packages else "npm:" + package, name=package)
                        context.edge(source, target, "requires", context.evidence(path))
                scripts = value.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError()
                for script in scripts:
                    target = context.node("build_target", "npm-script:" + path + ":" + script, name=script)
                    context.edge(source, target, "declares", context.evidence(path))
                if "workspaces" in value:
                    patterns = value["workspaces"]
                    if isinstance(patterns, dict):
                        patterns = patterns.get("packages", [])
                    if isinstance(patterns, list):
                        for other_path in context.inventory.files:
                            if Path(other_path).name != "package.json" or other_path == path:
                                continue
                            prefix = str(Path(path).parent)
                            relative = str(Path(other_path).parent)
                            if prefix != "." and relative.startswith(prefix + "/"):
                                relative = relative[len(prefix) + 1:]
                            if any(isinstance(pattern, str) and fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                                target = context.node("package", "npm-project:" + other_path)
                                context.edge(source, target, "contains", context.evidence(path))
            except (ValueError, TypeError, AttributeError):
                context.fragment.warnings.append("package manifest not parsed: " + path)
        return context.fragment


class DockerScanner:
    name, version = "dockerfile", "1"
    def supports(self, inventory):
        return any(Path(path).name.startswith("Dockerfile") for path in inventory.files)
    def scan(self, context):
        for path, content in context.inventory.files.items():
            if not Path(path).name.startswith("Dockerfile"):
                continue
            container = context.node("container", "dockerfile:" + path, name=path)
            for number, line in enumerate(content.decode("utf-8", "replace").splitlines(), 1):
                match = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.IGNORECASE)
                if not match:
                    continue
                image = match.group(1)
                if "$" in image:
                    context.fragment.warnings.append("dynamic Docker base unresolved: " + path)
                    continue
                target = context.node("container", "image:" + image, name=image)
                context.edge(container, target, "requires", context.evidence(path, line=number, basis="dockerfile-from"))
        return context.fragment


class CamolScanner:
    name, version = "camol-runbook", "1"
    def supports(self, inventory):
        return any(path.endswith(".json") for path in inventory.files)
    def scan(self, context):
        from .runbook import validate_runbook
        for path, content in context.inventory.files.items():
            if not path.endswith(".json"):
                continue
            try:
                value = json.loads(content)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(value, dict) or not {"schema_version", "run", "agents", "tasks"}.issubset(value):
                continue
            try:
                runbook = validate_runbook(value)
            except ValueError:
                context.fragment.warnings.append("invalid Camol runbook: " + path)
                continue
            tasks = {task["id"]: context.node("task", "camol:" + path + ":task:" + task["id"], name=task["id"], layer="execution") for task in runbook["tasks"]}
            evidence = context.evidence(path, basis="frozen-runbook-declaration")
            for task in runbook["tasks"]:
                for dependency in task["depends_on"]:
                    context.edge(tasks[task["id"]], tasks[dependency], "requires", evidence)
                for index, verification in enumerate(task["verification"]):
                    node = context.node("test", "camol:" + path + ":eval:" + task["id"] + ":" + str(index), name=verification["purpose"], layer="execution")
                    context.edge(node, tasks[task["id"]], "verifies", evidence)
            for agent in runbook["agents"]:
                box = context.node("box", "camol:" + path + ":box:" + agent["id"], name=agent["id"], layer="execution", attributes={"runtime_state": "UNVERIFIED", "adapter_kind": agent["adapter"]["kind"]})
                context.edge(context.file_node(path), box, "declares", evidence)
        return context.fragment


BUILTIN_SCANNERS = (GitScanner(), PythonScanner(), PackageJsonScanner(), DockerScanner(), CamolScanner())


@dataclass(frozen=True)
class GraphSnapshot:
    data: Mapping[str, Any]
    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.data))
    @property
    def snapshot_id(self):
        return self.data["snapshot_id"]
    @classmethod
    def from_dict(cls, value):
        fields = {"schema", "schema_version", "snapshot_id", "root", "source_revision", "dirty_digest", "crawler_version", "scanner_versions", "configuration_digest", "started_at", "completed_at", "status", "warnings", "node_count", "edge_count", "content_hash", "nodes", "edges", "evidence"}
        if not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.graph_snapshot" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise GraphError("invalid graph snapshot schema")
        if not isinstance(value["status"], str) or value["status"] not in {"STATIC_OBSERVED", "OBSERVATION_INCOMPLETE"}:
            raise GraphError("invalid graph snapshot status")
        if not isinstance(value["warnings"], list) or (value["warnings"] and value["status"] != "OBSERVATION_INCOMPLETE"):
            raise GraphError("incomplete graph observations must be labeled")
        node_fields = {"node_id", "kind", "locator", "display_name", "layer", "attributes", "evidence_status", "freshness_policy"}
        edge_fields = {"edge_id", "source", "target", "kind", "evidence_status", "evidence_refs", "attributes"}
        evidence_fields = {"evidence_id", "path", "source_digest", "line", "basis", "scanner", "source_revision"}
        for category, required in (("nodes", node_fields), ("edges", edge_fields), ("evidence", evidence_fields)):
            if not isinstance(value[category], list) or any(not isinstance(item, dict) or set(item) != required for item in value[category]):
                raise GraphError("graph {} records have invalid fields".format(category))
        for node in value["nodes"]:
            if any(not isinstance(node[name], str) or not node[name] for name in ("node_id", "kind", "locator", "display_name", "layer", "evidence_status", "freshness_policy")):
                raise GraphError("graph node identity and scope must be text")
            if node["kind"] not in {"repo", "workspace", "package", "module", "symbol", "build_target", "test", "container", "service", "deployment", "infrastructure", "box", "task", "tool", "daemon", "endpoint", "credential_ref", "runtime", "model_artifact"}:
                raise GraphError("graph node kind is unsupported")
            if node["layer"] not in {"source", "environment", "execution"} or node["evidence_status"] not in {"OBSERVED", "DERIVED", "INFERRED"} or not isinstance(node["attributes"], dict):
                raise GraphError("graph node has invalid scope or evidence")
            if node["node_id"] != _identity(node["kind"], node["locator"]):
                raise GraphError("graph node identity does not match its locator")
        for edge in value["edges"]:
            if any(not isinstance(edge[name], str) or not edge[name] for name in ("edge_id", "source", "target", "kind", "evidence_status")):
                raise GraphError("graph edge identity must be text")
            if not isinstance(edge["evidence_refs"], list) or any(not isinstance(identity, str) for identity in edge["evidence_refs"]):
                raise GraphError("graph evidence references must be text")
            if edge["evidence_status"] not in {"OBSERVED", "DERIVED", "INFERRED"} or not isinstance(edge["attributes"], dict):
                raise GraphError("graph edge evidence is invalid")
            if edge["kind"] not in {"contains", "declares", "imports", "requires", "builds", "produces", "tests", "deploys", "activates", "reads", "writes", "generates", "configured_by", "available_on", "leased_to", "changes", "verifies", "guards", "observed_as"}:
                raise GraphError("graph edge kind is unsupported")
            if edge["edge_id"] != _identity("edge", "|".join((edge["source"], edge["target"], edge["kind"]))):
                raise GraphError("graph edge identity does not match its endpoints")
        for item in value["evidence"]:
            if any(not isinstance(item[name], str) or not item[name] for name in ("evidence_id", "path", "basis", "scanner", "source_revision")):
                raise GraphError("graph evidence identity must be text")
            if item["line"] is not None and (type(item["line"]) is not int or item["line"] < 1):
                raise GraphError("graph evidence line must be positive")
        if len({item["edge_id"] for item in value["edges"]}) != len(value["edges"]):
            raise GraphError("graph edges must be unique")
        nodes = {node["node_id"] for node in value["nodes"]}
        evidence = {item["evidence_id"] for item in value["evidence"]}
        if len(evidence) != len(value["evidence"]):
            raise GraphError("graph evidence must be unique")
        if any(type(value[name]) is not int or value[name] < 0 for name in ("node_count", "edge_count")):
            raise GraphError("graph counts must be non-negative integers")
        if len(nodes) != len(value["nodes"]) or value["node_count"] != len(nodes) or value["edge_count"] != len(value["edges"]):
            raise GraphError("graph counts or node identities are inconsistent")
        for edge in value["edges"]:
            if edge["source"] not in nodes or edge["target"] not in nodes or not edge["evidence_refs"] or set(edge["evidence_refs"]) - evidence:
                raise GraphError("graph edge has missing node or evidence")
        content = {key: item for key, item in value.items() if key not in {"snapshot_id", "content_hash", "started_at", "completed_at"}}
        if canonical_digest(content) != value["content_hash"] or value["snapshot_id"] != "graph-" + value["content_hash"].split(":")[1]:
            raise GraphError("graph snapshot content hash mismatch")
        return cls(json.loads(json.dumps(value)))


def crawl_repository(root: Path, *, policy: Optional[CrawlPolicy] = None, scanners: Optional[Sequence[RepositoryScanner]] = None) -> GraphSnapshot:
    policy = policy or CrawlPolicy()
    started = datetime.now(timezone.utc).isoformat()
    inventory = inventory_repository(root, policy)
    selected = tuple(BUILTIN_SCANNERS if scanners is None else scanners)
    nodes, edges, evidence, warnings = {}, {}, {}, list(inventory.warnings)
    versions = {scanner.name: scanner.version for scanner in selected}
    for scanner in selected:
        if not scanner.supports(inventory):
            continue
        fragment = scanner.scan(ScanContext(inventory, scanner.name))
        for node in fragment.nodes:
            existing = nodes.get(node["node_id"])
            if existing is None or (existing["display_name"] == existing["locator"] and node["display_name"] != node["locator"]):
                nodes[node["node_id"]] = node
        for edge in fragment.edges:
            existing = edges.setdefault(edge["edge_id"], edge)
            existing["evidence_refs"] = sorted(set(existing["evidence_refs"] + edge["evidence_refs"]))
        evidence.update((item["evidence_id"], item) for item in fragment.evidence)
        warnings.extend(fragment.warnings)
    value = {
        "schema": "camol.graph_snapshot", "schema_version": 1,
        "root": str(inventory.root), "source_revision": inventory.revision,
        "dirty_digest": inventory.dirty_digest, "crawler_version": "1",
        "scanner_versions": versions, "configuration_digest": canonical_digest(asdict(policy)),
        "status": "OBSERVATION_INCOMPLETE" if warnings else "STATIC_OBSERVED",
        "warnings": sorted(set(warnings)), "node_count": len(nodes), "edge_count": len(edges),
        "nodes": sorted(nodes.values(), key=lambda item: item["node_id"]),
        "edges": sorted(edges.values(), key=lambda item: item["edge_id"]),
        "evidence": sorted(evidence.values(), key=lambda item: item["evidence_id"]),
    }
    value = Redactor().value(value)
    digest = canonical_digest(value)
    value.update(snapshot_id="graph-" + digest.split(":")[1], content_hash=digest,
                 started_at=started, completed_at=datetime.now(timezone.utc).isoformat())
    return GraphSnapshot.from_dict(value)


class RepositoryGraph:
    def __init__(self, snapshot: GraphSnapshot):
        self.snapshot = GraphSnapshot.from_dict(snapshot.to_dict())
        self.nodes = {node["node_id"]: node for node in self.snapshot.data["nodes"]}
        self.edges = self.snapshot.data["edges"]

    def resolve(self, selector: str) -> str:
        if selector in self.nodes:
            return selector
        matches = [identity for identity, node in self.nodes.items() if selector in {node["locator"], node["display_name"]}]
        if len(matches) != 1:
            raise GraphError("node selector is {}: {}".format("ambiguous" if matches else "unknown", selector))
        return matches[0]

    def why(self, source: str, target: str, *, max_paths: int = 16, max_expansions: int = 10000) -> dict:
        if type(max_paths) is not int or max_paths <= 0 or type(max_expansions) is not int or max_expansions <= 0:
            raise GraphError("path-query bounds must be positive integers")
        source, target = self.resolve(source), self.resolve(target)
        adjacency = {node: [] for node in self.nodes}
        for edge in self.edges:
            adjacency[edge["source"]].append(edge)
        queue_items = deque([(source, [])])
        distances, paths, shortest = {source: 0}, [], None
        truncated, expansions = False, 0
        while queue_items:
            if expansions >= max_expansions:
                truncated = True
                break
            expansions += 1
            node, path = queue_items.popleft()
            if shortest is not None and len(path) > shortest:
                break
            if node == target:
                shortest = len(path)
                if len(paths) == max_paths:
                    truncated = True
                    break
                paths.append(path)
                continue
            for edge in adjacency[node]:
                neighbor = edge["target"]
                if distances.get(neighbor, len(path) + 1) < len(path) + 1:
                    continue
                distances[neighbor] = len(path) + 1
                queue_items.append((neighbor, path + [edge]))
                if len(queue_items) >= max_expansions:
                    truncated = True
                    break
        return {"source": source, "target": target, "paths": paths, "ambiguous": len(paths) > 1, "truncated": truncated, "basis": "static declarations; not runtime causality"}

    def impact(self, selector: str, *, edge_kinds: Sequence[str] = ("imports", "requires", "tests", "verifies", "configured_by")) -> dict:
        start = self.resolve(selector)
        visited, selected = {start}, {}
        incoming = {}
        for edge in self.edges:
            if edge["kind"] in edge_kinds:
                incoming.setdefault(edge["target"], []).append(edge)
        pending = deque([start])
        while pending:
            target = pending.popleft()
            for edge in incoming.get(target, []):
                selected[edge["edge_id"]] = edge
                if edge["source"] not in visited:
                    visited.add(edge["source"])
                    pending.append(edge["source"])
        return {"node_id": start, "direction": "dependents", "edge_kinds": list(edge_kinds), "nodes": [self.nodes[node] for node in sorted(visited - {start})], "edges": [selected[key] for key in sorted(selected)], "basis": "potential static impact; actual execution is unverified"}

    def _components(self) -> List[List[str]]:
        adjacency = {node: [] for node in self.nodes}
        reverse = {node: [] for node in self.nodes}
        for edge in self.edges:
            adjacency[edge["source"]].append(edge["target"])
            reverse[edge["target"]].append(edge["source"])
        seen, order = set(), []
        for root in sorted(adjacency):
            stack = [(root, False)]
            while stack:
                node, closing = stack.pop()
                if closing:
                    order.append(node)
                elif node not in seen:
                    seen.add(node)
                    stack.append((node, True))
                    stack.extend((other, False) for other in sorted(adjacency[node], reverse=True) if other not in seen)
        seen, components = set(), []
        for root in reversed(order):
            if root in seen:
                continue
            stack, component = [root], []
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                component.append(node)
                stack.extend(reverse[node])
            components.append(sorted(component))
        return sorted(components)

    def cycles(self) -> List[List[str]]:
        self_edges = {edge["source"] for edge in self.edges if edge["source"] == edge["target"]}
        return [part for part in self._components() if len(part) > 1 or part[0] in self_edges]

    def layers(self) -> List[List[List[str]]]:
        components = self._components()
        component_for = {node: index for index, component in enumerate(components) for node in component}
        incoming = {index: set() for index in range(len(components))}
        for edge in self.edges:
            source, target = component_for[edge["source"]], component_for[edge["target"]]
            if source != target:
                incoming[target].add(source)
        remaining, layers = set(incoming), []
        while remaining:
            layer = sorted(index for index in remaining if not incoming[index] & remaining)
            if not layer:
                raise GraphError("cycle condensation failed")
            layers.append([components[index] for index in layer])
            remaining.difference_update(layer)
        return layers

    def render_text(self, *, limit: int = 60) -> str:
        value = self.snapshot.data
        lines = ["REPOSITORY GRAPH {} — {} nodes / {} edges".format(value["status"], len(self.nodes), len(self.edges)), "Static source declarations; runtime capability and deployment readiness are unverified."]
        shown = 0
        for level, components in enumerate(self.layers()):
            for component in components:
                if shown >= limit:
                    break
                names = ", ".join(self.nodes[node]["display_name"] for node in component)
                lines.append("{}{}{}".format("  " * min(level, 8), "[cycle] " if len(component) > 1 else "", names))
                shown += len(component)
        if shown < len(self.nodes):
            lines.append("… {} nodes omitted; use impact/why or JSON export.".format(len(self.nodes) - shown))
        lines.extend("warning: " + warning for warning in value["warnings"])
        return "\n".join(lines)

    def export(self, format: str = "json") -> str:
        if format == "json":
            return json.dumps(self.snapshot.to_dict(), indent=2, sort_keys=True)
        if format == "dot":
            quote = json.dumps
            lines = ["digraph camol {"]
            lines.extend("  {} [label={}];".format(quote(identity), quote(node["display_name"])) for identity, node in sorted(self.nodes.items()))
            lines.extend("  {} -> {} [label={}];".format(quote(edge["source"]), quote(edge["target"]), quote(edge["kind"])) for edge in self.edges)
            return "\n".join(lines + ["}"])
        if format == "graphml":
            root = ElementTree.Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
            ElementTree.SubElement(root, "key", id="label", **{"for": "all", "attr.name": "label", "attr.type": "string"})
            graph = ElementTree.SubElement(root, "graph", id="camol", edgedefault="directed")
            for identity, node in sorted(self.nodes.items()):
                element = ElementTree.SubElement(graph, "node", id=identity)
                ElementTree.SubElement(element, "data", key="label").text = node["display_name"]
            for edge in self.edges:
                element = ElementTree.SubElement(graph, "edge", id=edge["edge_id"], source=edge["source"], target=edge["target"])
                ElementTree.SubElement(element, "data", key="label").text = edge["kind"]
            return ElementTree.tostring(root, encoding="unicode")
        raise GraphError("export format must be json, dot, or graphml")


def diff_snapshots(before: GraphSnapshot, after: GraphSnapshot) -> dict:
    before, after = GraphSnapshot.from_dict(before.to_dict()), GraphSnapshot.from_dict(after.to_dict())
    result = {"before": before.snapshot_id, "after": after.snapshot_id}
    for category, key in (("nodes", "node_id"), ("edges", "edge_id")):
        old, new = ({item[key]: item for item in snapshot.data[category]} for snapshot in (before, after))
        result[category] = {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()), "changed": sorted(identity for identity in old.keys() & new.keys() if old[identity] != new[identity])}
    return result


class GraphStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if self.path.is_symlink():
            raise GraphError("graph database must not be a symlink")
        if read_only:
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            self.connection.execute("PRAGMA query_only=ON")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute("CREATE TABLE IF NOT EXISTS graph_snapshots (snapshot_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, completed_at TEXT NOT NULL, payload TEXT NOT NULL)")
        self.connection.commit()

    def save(self, snapshot: GraphSnapshot) -> str:
        if self.read_only:
            raise GraphError("a read-only graph store cannot save snapshots")
        snapshot = GraphSnapshot.from_dict(snapshot.to_dict())
        value = snapshot.to_dict()
        self.connection.execute("INSERT OR IGNORE INTO graph_snapshots VALUES (?, ?, ?, ?)", (snapshot.snapshot_id, value["content_hash"], value["completed_at"], json.dumps(value, sort_keys=True)))
        self.connection.commit()
        return snapshot.snapshot_id

    def load(self, snapshot_id: Optional[str] = None) -> GraphSnapshot:
        if snapshot_id is None:
            row = self.connection.execute("SELECT payload FROM graph_snapshots ORDER BY completed_at DESC, snapshot_id DESC LIMIT 1").fetchone()
        else:
            row = self.connection.execute("SELECT payload FROM graph_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise GraphError("graph snapshot not found; crawl first")
        return GraphSnapshot.from_dict(json.loads(row[0]))

    def list_snapshots(self) -> list:
        return [{"snapshot_id": row[0], "content_hash": row[1], "completed_at": row[2]} for row in self.connection.execute("SELECT snapshot_id, content_hash, completed_at FROM graph_snapshots ORDER BY completed_at, snapshot_id")]

    def close(self) -> None:
        self.connection.close()
