"""Reviewed, one-shot reproduction execution using the normal sandbox boundary.

This is deliberately a local command oracle, not a claim that an arbitrary
command proves a prose specification. The owner reviews that mapping explicitly.
The source identity is a clean Git revision; ignored files and external runtime
dependencies are not represented as a hermetic filesystem snapshot.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import ArtifactRef, ArtifactStore
from .evidence import EvidenceRecord
from .probes import GIT_SAFETY_ARGS, Redactor, sanitized_environment
from .sandbox import SandboxPolicy, SandboxError, process_start_fingerprint, select_backend, validate_process_invocation
from .schema import canonical_digest, require_digest, require_identifier, require_timestamp


class DebugExecutionError(ValueError):
    pass


EXECUTOR = "debug-executor"
EXECUTION_EVENTS = frozenset({
    "DEBUG_REPRODUCTION_FROZEN", "DEBUG_EXECUTION_AUTHORIZED", "DEBUG_EXECUTION_STARTED",
    "DEBUG_EXECUTION_FINISHED", "DEBUG_EXECUTION_INTERRUPTED",
})


def _component(identity: str) -> str:
    # Logical identifiers may legally contain slashes. They are never paths.
    require_identifier(identity, "debug identity")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _packet_dir(artifacts: ArtifactStore, case_id: str) -> Path:
    return artifacts.state_dir / "packets" / ("debug-" + _component(case_id))


def _invocation_path(packet_dir: Path, execution_id: str, command_id: str) -> Path:
    return packet_dir / (_component(execution_id) + "-" + _component(command_id) + ".invocation.json")


def _exact(value: Any, fields: set, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise DebugExecutionError(label + " has missing or unknown fields")
    return deepcopy(value)


def _text(value: Any, label: str, maximum: int = 16384) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise DebugExecutionError(label + " must be bounded non-empty text")
    return value


def source_identity(workspace: Path) -> dict:
    """Read a clean tracked source identity with hooks and external Git tools off."""
    root = Path(workspace).resolve(strict=True)
    git = shutil.which("git", path=os.defpath)
    if not git:
        raise DebugExecutionError("Git is required for a reproducible debug source")

    def read(*args: str) -> str:
        result = subprocess.run(
            [git] + list(GIT_SAFETY_ARGS) + ["-C", str(root)] + list(args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=sanitized_environment(), timeout=30, check=False,
        )
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise DebugExecutionError("debug source identity could not be established")
        return result.stdout.decode("utf-8", "strict").strip()

    if Path(read("rev-parse", "--show-toplevel")).resolve() != root:
        raise DebugExecutionError("debug workspace must be the Git checkout root")
    if read("ls-files", "--others", "--exclude-standard", "-z"):
        raise DebugExecutionError("debug source must be a clean committed checkout")
    revision = read("rev-parse", "HEAD^{commit}")
    tree = read("rev-parse", "HEAD^{tree}")
    committed = {}
    for entry in read("ls-tree", "-rz", "--full-tree", revision).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            mode, kind, blob = metadata.split()
            committed[name] = (mode, blob)
    tracked = read("ls-files", "--stage", "-z").split("\0")
    file_identities, total = [], 0
    for entry in tracked:
        if not entry:
            continue
        if len(file_identities) >= 10000:
            raise DebugExecutionError("debug source inventory exceeds 10000 files")
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if committed.get(name) != (mode, blob):
            raise DebugExecutionError("debug source index differs from its committed revision")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or stage != "0" or mode not in {"100644", "100755"}:
            raise DebugExecutionError("debug fixture source must contain ordinary tracked files, not symlinks or submodules")
        if any(part.lower().startswith(".env") or part.lower() in {".ssh", ".aws", "credentials.json", "secrets.json"}
               or part.lower().endswith((".pem", ".key", ".p12")) for part in relative.parts):
            raise DebugExecutionError("use a sanitized debug fixture without tracked credential files")
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise DebugExecutionError("debug source paths cannot traverse symlinks")
        # Check descriptor type before reading, including a tracked file replaced
        # by a FIFO. A blocking open would wait for a writer before fstat runs.
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024 * 1024:
                raise DebugExecutionError("debug source file is non-regular or exceeds 32 MiB")
            object_digest = hashlib.sha1() if len(blob) == 40 else hashlib.sha256()
            object_digest.update(("blob " + str(before.st_size) + "\0").encode("ascii"))
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 128 * 1024 * 1024:
                    raise DebugExecutionError("debug source inventory exceeds 128 MiB")
                digest.update(chunk)
                object_digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise DebugExecutionError("debug source changed during observation")
        if object_digest.hexdigest() != blob:
            raise DebugExecutionError("working source bytes differ from their committed Git identity")
        file_identities.append({"path": name, "executable": bool(before.st_mode & 0o111),
                                "sha256": "sha256:" + digest.hexdigest()})
    if set(committed) != {item["path"] for item in file_identities}:
        raise DebugExecutionError("debug source tracked-file set changed")
    if read("rev-parse", "HEAD^{commit}") != revision:
        raise DebugExecutionError("debug source revision changed during observation")
    return validate_source({
        "schema": "camol.debug_source", "schema_version": 1,
        "workspace": str(root), "revision": revision, "tree": tree,
        "checkout_digest": canonical_digest(sorted(file_identities, key=lambda item: item["path"])),
    })


def validate_source(value: Any) -> dict:
    source = _exact(value, {"schema", "schema_version", "workspace", "revision", "tree", "checkout_digest"}, "debug source")
    if source["schema"] != "camol.debug_source" or type(source["schema_version"]) is not int or source["schema_version"] != 1:
        raise DebugExecutionError("unsupported debug source version")
    if not Path(_text(source["workspace"], "source workspace")).is_absolute():
        raise DebugExecutionError("source workspace must be absolute")
    for name in ("revision", "tree"):
        if not isinstance(source[name], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source[name]):
            raise DebugExecutionError("source " + name + " must be a full Git object id")
    require_digest(source["checkout_digest"], "source checkout_digest")
    return source


def executable_digest(path: str) -> str:
    executable = Path(path)
    if not executable.is_absolute() or not executable.is_file() or not os.access(str(executable), os.X_OK):
        raise DebugExecutionError("reproduction executable must be an absolute executable file")
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_contract(value: Any) -> dict:
    contract = _exact(value, {
        "schema", "schema_version", "run_id", "case_id", "plan_digest", "target_digest",
        "baseline_source", "commands", "evaluator_assets", "environment", "sandbox_policy", "reviewed_by",
    }, "reproduction contract")
    if contract["schema"] != "camol.debug_reproduction" or type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise DebugExecutionError("unsupported reproduction contract version")
    for name in ("run_id", "case_id", "reviewed_by"):
        require_identifier(contract[name], name)
    for name in ("plan_digest", "target_digest"):
        require_digest(contract[name], name)
    source = validate_source(contract["baseline_source"])
    assets = contract["evaluator_assets"]
    if not isinstance(assets, list) or len(assets) > 1024:
        raise DebugExecutionError("evaluator_assets must be an explicit bounded array")
    asset_names = set()
    for asset in assets:
        _exact(asset, {"path", "digest"}, "evaluator asset")
        path = Path(_text(asset["path"], "evaluator asset path"))
        if path.is_absolute() or ".." in path.parts or str(path) in asset_names:
            raise DebugExecutionError("evaluator asset paths must be unique relative paths")
        require_digest(asset["digest"], "evaluator asset digest")
        asset_names.add(str(path))
    policy = SandboxPolicy.from_dict(contract["sandbox_policy"])
    if policy.workspace != source["workspace"]:
        raise DebugExecutionError("sandbox and source must bind the same workspace")
    if policy.trust_tier != "developer_trusted":
        # A before/after hash cannot detect a child that weakens an oracle,
        # obtains a green result, then restores its original bytes. Measuring
        # a candidate is a read-only operation; any output belongs in an
        # explicitly granted scratch directory outside the source checkout.
        workspace = Path(policy.workspace)
        if any(Path(root) == workspace or Path(root).is_relative_to(workspace)
               or workspace.is_relative_to(Path(root)) for root in policy.write_paths):
            raise DebugExecutionError("hardened reproduction requires read-only source and evaluator assets; use external scratch writes")
        if policy.workspace not in policy.readonly_paths:
            raise DebugExecutionError("hardened reproduction must explicitly protect the entire source as read-only")
    # Local reproduction does not acquire provider credentials or remote-effect
    # authority. developer_trusted still honestly means OS restrictions absent.
    if policy.network_destinations or policy.credential_refs:
        raise DebugExecutionError("debug reproduction v1 supports only local, credential-free commands")
    environment = contract["environment"]
    if (not isinstance(environment, dict) or len(environment) > 128
            or set(environment) != set(policy.environment_names)):
        raise DebugExecutionError("reproduction must freeze every allowed environment value")
    for key, val in environment.items():
        _text(key, "environment name", 256)
        if not isinstance(val, str) or len(val) > 16384 or "\0" in val:
            raise DebugExecutionError("environment values must be bounded strings")
    commands = contract["commands"]
    if not isinstance(commands, list) or not commands or len(commands) > 32:
        raise DebugExecutionError("reproduction requires 1-32 explicitly reviewed commands")
    identities, guardrails = set(), set()
    for item in commands:
        command = _exact(item, {"command_id", "argv", "executable_digest", "cwd", "timeout_seconds",
                                "expected_exit_codes", "guardrail"}, "reproduction command")
        identity = require_identifier(command["command_id"], "command_id")
        if identity in identities:
            raise DebugExecutionError("command ids must be unique")
        identities.add(identity)
        argv = command["argv"]
        if not isinstance(argv, list) or not argv or len(argv) > 256:
            raise DebugExecutionError("command argv must be a non-empty bounded array")
        for arg in argv:
            if not isinstance(arg, str) or len(arg) > 16384 or "\0" in arg:
                raise DebugExecutionError("argv entries must be bounded strings")
        if not Path(argv[0]).is_absolute():
            raise DebugExecutionError("command executable must be absolute")
        if policy.trust_tier != "developer_trusted" and any(
                Path(argv[0]) == Path(root) or Path(argv[0]).is_relative_to(Path(root))
                for root in policy.write_paths):
            raise DebugExecutionError("hardened reproduction cannot grant writes to its frozen executable")
        require_digest(command["executable_digest"], "executable_digest")
        cwd = Path(_text(command["cwd"], "command cwd"))
        if cwd.is_absolute() or ".." in cwd.parts:
            raise DebugExecutionError("cwd must be workspace-relative without traversal")
        if type(command["timeout_seconds"]) is not int or not 1 <= command["timeout_seconds"] <= 3600:
            raise DebugExecutionError("command timeout must be 1-3600 seconds")
        codes = command["expected_exit_codes"]
        if (not isinstance(codes, list) or not codes or len(codes) > 16
                or any(type(code) is not int or not 0 <= code <= 255 for code in codes)
                or len(set(codes)) != len(codes)):
            raise DebugExecutionError("expected exit codes must be a unique bounded array")
        if command["guardrail"] is not None:
            guardrail = require_identifier(command["guardrail"], "guardrail")
            if guardrail in guardrails:
                raise DebugExecutionError("guardrail commands must have unique names")
            guardrails.add(guardrail)
    if sum(item["guardrail"] is None for item in commands) != 1:
        raise DebugExecutionError("exactly one reproduction command must be the target oracle")
    if Redactor().value(contract) != contract:
        raise DebugExecutionError("reproduction contract contains secret-shaped data; use a sanitized fixture")
    return contract


def environment_digest(contract: dict) -> str:
    return canonical_digest({
        "environment": contract["environment"], "sandbox_policy": contract["sandbox_policy"],
        "executables": [{"path": item["argv"][0], "digest": item["executable_digest"]}
                        for item in contract["commands"]],
    })


def reproduction_contract(*, run_id: str, case_id: str, plan_digest: str,
                          target_behavior: str, target_authority: str, workspace: Path,
                          commands: list, evaluator_assets: list, environment: dict, sandbox_policy: SandboxPolicy,
                          reviewed_by: str) -> dict:
    """Assemble a reviewable contract without executing or granting anything.

    Every command still requires explicit argv, cwd, timeout, expected exit
    codes, command id and target/guardrail mapping. Only observed identities are
    filled in; an oracle is never inferred from a prose target.
    """
    if not isinstance(commands, list) or not 1 <= len(commands) <= 32:
        raise DebugExecutionError("reproduction requires a bounded list of declared commands")
    if not isinstance(evaluator_assets, list) or len(evaluator_assets) > 1024:
        raise DebugExecutionError("evaluator_assets must be an explicit bounded path array")
    source = source_identity(workspace)
    assets = [{"path": path, "digest": _asset_digest(Path(source["workspace"]), path)} for path in evaluator_assets]
    declared = []
    for item in commands:
        command = _exact(item, {"command_id", "argv", "cwd", "timeout_seconds",
                                "expected_exit_codes", "guardrail"}, "declared debug command")
        argv = command["argv"]
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            raise DebugExecutionError("command must declare an executable argv")
        declared.append(dict(command, executable_digest=executable_digest(argv[0])))
    return validate_contract({
        "schema": "camol.debug_reproduction", "schema_version": 1,
        "run_id": run_id, "case_id": case_id, "plan_digest": plan_digest,
        "target_digest": canonical_digest({"target_behavior": target_behavior, "target_authority": target_authority}),
        "baseline_source": source, "commands": declared, "evaluator_assets": assets,
        "environment": deepcopy(environment), "sandbox_policy": sandbox_policy.to_dict(), "reviewed_by": reviewed_by,
    })


def _asset_digest(workspace: Path, relative: str) -> str:
    path = Path(_text(relative, "evaluator asset path"))
    if path.is_absolute() or ".." in path.parts:
        raise DebugExecutionError("evaluator assets must stay inside the source checkout")
    target = workspace
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise DebugExecutionError("evaluator assets cannot traverse symlinks")
    descriptor = os.open(str(target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32 * 1024 * 1024:
            raise DebugExecutionError("evaluator asset is not a bounded regular file")
        content = handle.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024:
        raise DebugExecutionError("evaluator asset exceeds its byte bound")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _verify_assets(contract: dict) -> None:
    workspace = Path(contract["baseline_source"]["workspace"])
    for asset in contract["evaluator_assets"]:
        if _asset_digest(workspace, asset["path"]) != asset["digest"]:
            raise DebugExecutionError("the candidate changed a frozen evaluator asset")


def _authority(state: dict, event: dict) -> None:
    if not state.get("approved_by") or event.get("actor_id") != state["approved_by"]:
        raise DebugExecutionError("debug execution authority requires the approving run owner")


def apply_execution_event(state: dict, event: dict) -> None:
    kind, p = event["type"], event["payload"]
    fields = {
        "DEBUG_REPRODUCTION_FROZEN": {"contract"},
        "DEBUG_EXECUTION_AUTHORIZED": {"execution_id", "source", "experiment_id", "contract_digest"},
        "DEBUG_EXECUTION_STARTED": {"execution_id", "owner_pid", "owner_started"},
        "DEBUG_EXECUTION_FINISHED": {"execution_id", "receipt"},
        "DEBUG_EXECUTION_INTERRUPTED": {"execution_id", "reason"},
    }
    _exact(p, fields[kind] | {"schema_version", "case_id"}, "debug execution event")
    if type(p["schema_version"]) is not int or p["schema_version"] != 2:
        raise DebugExecutionError("debug execution events require protocol version 2")
    case = state["debug_cases"].get(p["case_id"])
    if not case or case.get("schema_version") != 2:
        raise DebugExecutionError("execution requires a versioned debug case")
    if kind == "DEBUG_REPRODUCTION_FROZEN":
        _authority(state, event)
        contract = validate_contract(p["contract"])
        if case.get("execution_contract"):
            raise DebugExecutionError("the reproduction is already frozen; open a new case to change its oracle")
        if any(contract[name] != expected for name, expected in (
            ("run_id", state["run_id"]), ("case_id", p["case_id"]),
            ("plan_digest", state["plan_digest"]), ("target_digest", case["target_digest"]),
            ("reviewed_by", state["approved_by"]),
        )):
            raise DebugExecutionError("reproduction contract changed its authority or subject")
        case.update(execution_contract=contract, executions={})
        return
    contract = case.get("execution_contract")
    if not contract:
        raise DebugExecutionError("freeze a reviewed reproduction before execution")
    identity = require_identifier(p["execution_id"], "execution_id")
    executions = case["executions"]
    if kind == "DEBUG_EXECUTION_AUTHORIZED":
        _authority(state, event)
        if identity in executions:
            raise DebugExecutionError("execution identity is already consumed")
        if any(item["status"] in {"authorized", "running"} for item in executions.values()):
            raise DebugExecutionError("reconcile the pending execution before authorizing another")
        source = validate_source(p["source"])
        if source["workspace"] != contract["baseline_source"]["workspace"] or p["contract_digest"] != canonical_digest(contract):
            raise DebugExecutionError("execution changed its frozen source scope or command contract")
        experiment_id = p["experiment_id"]
        if experiment_id is None:
            if source != contract["baseline_source"]:
                raise DebugExecutionError("baseline execution changed its frozen original source")
        else:
            require_identifier(experiment_id, "experiment_id")
            if not case["experiments"] or case["experiments"][-1]["experiment_id"] != experiment_id:
                raise DebugExecutionError("execution must identify the current experiment")
            experiment = case["experiments"][-1]
            followup = experiment["status"] == "finished" and case["status"] in {"verified", "eval_promoted"}
            if experiment["status"] != "running" and not followup:
                raise DebugExecutionError("experiment has already finished")
            if experiment["candidate_digest"] != canonical_digest(source) or experiment["environment_digest"] != environment_digest(contract):
                raise DebugExecutionError("execution source or environment does not match the experiment")
            if set(experiment["guardrails"]) != {item["guardrail"] for item in contract["commands"] if item["guardrail"]}:
                raise DebugExecutionError("experiment guardrails differ from the frozen reproduction")
            attempts = [item for item in executions.values() if item["experiment_id"] == experiment_id]
            if not followup and len(attempts) >= experiment["max_attempts"]:
                raise DebugExecutionError("experiment has exhausted its execution attempts")
            from math import ceil
            ceiling = sum(item["timeout_seconds"] for item in contract["commands"])
            consumed = sum(ceil(sum(result["duration_ms"] for result in item["receipt"]["results"]) / 1000)
                           if item["status"] == "finished" else ceiling for item in attempts)
            if ceiling + (0 if followup else consumed) > experiment["max_seconds"]:
                raise DebugExecutionError("command ceilings exceed the experiment time budget")
        executions[identity] = dict(deepcopy(p), status="authorized")
        return
    execution = executions.get(identity)
    if not execution:
        raise DebugExecutionError("unknown authorized execution")
    if kind == "DEBUG_EXECUTION_STARTED":
        if event.get("actor_id") != EXECUTOR or execution["status"] != "authorized":
            raise DebugExecutionError("only the executor may consume one unused authorization")
        if type(p["owner_pid"]) is not int or p["owner_pid"] <= 1:
            raise DebugExecutionError("execution owner must have a process identity")
        _text(p["owner_started"], "execution owner start fingerprint")
        execution.update(status="running", started_seq=event.get("seq"),
                         owner_pid=p["owner_pid"], owner_started=p["owner_started"])
    elif kind == "DEBUG_EXECUTION_INTERRUPTED":
        if event.get("actor_id") != EXECUTOR:
            _authority(state, event)
        if execution["status"] not in {"authorized", "running"}:
            raise DebugExecutionError("execution is already terminal")
        execution.update(status="interrupted", reason=_text(p["reason"], "interruption reason"))
    elif kind == "DEBUG_EXECUTION_FINISHED":
        if event.get("actor_id") != EXECUTOR or execution["status"] != "running":
            raise DebugExecutionError("only the executor may finish an active execution")
        receipt = validate_receipt(p["receipt"], contract, execution)
        execution.update(status="finished", receipt=receipt, receipt_digest=canonical_digest(receipt))


def validate_receipt(value: Any, contract: dict, execution: dict) -> dict:
    receipt = _exact(value, {"schema", "schema_version", "execution_id", "contract_digest", "source",
                             "source_after", "environment_digest", "results"}, "debug execution receipt")
    if receipt["schema"] != "camol.debug_execution_receipt" or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise DebugExecutionError("unsupported execution receipt version")
    if (receipt["execution_id"] != execution["execution_id"] or receipt["contract_digest"] != canonical_digest(contract)
            or receipt["source"] != execution["source"] or receipt["source_after"] != execution["source"]
            or receipt["environment_digest"] != environment_digest(contract)):
        raise DebugExecutionError("execution receipt changed its source, environment or contract")
    results = receipt["results"]
    if not isinstance(results, list) or len(results) != len(contract["commands"]):
        raise DebugExecutionError("receipt must contain the exact frozen command set")
    previous_finish = None
    for result, command in zip(results, contract["commands"]):
        _exact(result, {"command_id", "argv", "cwd", "exit_code", "passed", "started_at", "finished_at",
                        "duration_ms", "backend", "policy_digest", "stdout", "stderr"}, "command result")
        cwd = str(Path(contract["baseline_source"]["workspace"]) / command["cwd"])
        if result["command_id"] != command["command_id"] or result["argv"] != command["argv"] or result["cwd"] != cwd:
            raise DebugExecutionError("receipt command differs from reviewed argv or cwd")
        if type(result["exit_code"]) is not int or type(result["passed"]) is not bool or result["passed"] != (result["exit_code"] in command["expected_exit_codes"]):
            raise DebugExecutionError("command verdict must derive from its actual exit code and frozen oracle")
        start = datetime.fromisoformat(require_timestamp(result["started_at"], "command started_at").replace("Z", "+00:00"))
        finish = datetime.fromisoformat(require_timestamp(result["finished_at"], "command finished_at").replace("Z", "+00:00"))
        if finish < start or previous_finish is not None and start < previous_finish:
            raise DebugExecutionError("execution timestamps must be ordered")
        previous_finish = finish
        if type(result["duration_ms"]) is not int or result["duration_ms"] != int((finish - start).total_seconds() * 1000):
            raise DebugExecutionError("duration does not match command timestamps")
        policy = SandboxPolicy.from_dict(contract["sandbox_policy"])
        backend = "developer_trusted" if policy.trust_tier == "developer_trusted" else "macos-seatbelt"
        if result["policy_digest"] != policy.digest() or result["backend"] != backend:
            raise DebugExecutionError("execution backend differs from frozen sandbox policy")
        for channel in ("stdout", "stderr"):
            reference = ArtifactRef.from_dict(result[channel])
            if (reference.producer.get("run_id") != contract["run_id"]
                    or reference.producer.get("invocation_id") != execution["execution_id"]
                    or reference.producer.get("channel") != channel
                    or reference.producer.get("role") != EXECUTOR):
                raise DebugExecutionError("output artifact has another execution producer")
    return receipt


def validate_executed_evidence(state: dict, case: dict, record: EvidenceRecord) -> None:
    """A status/producer string alone cannot manufacture a kernel execution."""
    data = record.data
    execution = case.get("executions", {}).get(data.get("execution_id"))
    if not execution or execution["status"] != "finished" or record.producer != EXECUTOR or record.kind != "test_result":
        raise DebugExecutionError("executed debug evidence needs a finished kernel receipt")
    receipt, contract = execution["receipt"], case["execution_contract"]
    if data.get("receipt_digest") != canonical_digest(receipt):
        raise DebugExecutionError("evidence does not reference the exact execution receipt")
    matches = [(command, result) for command, result in zip(contract["commands"], receipt["results"])
               if command["command_id"] == data.get("command_id")]
    if len(matches) != 1:
        raise DebugExecutionError("evidence command is not present in its receipt")
    command, result = matches[0]
    expected = evidence_data(case, execution, command, result)
    if data != expected:
        raise DebugExecutionError("executed evidence metadata differs from its kernel receipt")
    refs = {canonical_digest(item.to_dict()) for item in record.artifact_refs}
    expected_refs = {result[name]["digest"]: result[name] for name in ("stdout", "stderr")}
    if refs != {canonical_digest(item) for item in expected_refs.values()}:
        raise DebugExecutionError("executed evidence must retain the exact output artifact references")


def evidence_data(case: dict, execution: dict, command: dict, result: dict) -> dict:
    return {
        "execution_id": execution["execution_id"], "receipt_digest": execution["receipt_digest"],
        "command_id": command["command_id"], "passed": result["passed"],
        "target_digest": case["target_digest"], "guardrail": command["guardrail"],
        "experiment_id": execution["experiment_id"],
        "environment_digest": execution["receipt"]["environment_digest"],
        "candidate_digest": canonical_digest(execution["source"]),
        "source_revision": execution["source"]["revision"], "argv": result["argv"],
        "cwd": result["cwd"], "exit_code": result["exit_code"], "duration_ms": result["duration_ms"],
        "backend": result["backend"],
        "source_isolation": ("hash_checks_only" if case["execution_contract"]["sandbox_policy"]["trust_tier"]
                             == "developer_trusted" else "read_only_source_enforced"),
    }


async def execute(debugger: Any, case_id: str, execution_id: str, artifacts: ArtifactStore) -> list:
    """Consume exactly one reviewed authorization; never retry a consumed command."""
    case = debugger.inspect(case_id)
    execution = case.get("executions", {}).get(execution_id)
    if not execution or execution["status"] != "authorized":
        raise DebugExecutionError("execution is not an unused reviewed authorization")
    contract = validate_contract(case["execution_contract"])
    policy = SandboxPolicy.from_dict(contract["sandbox_policy"])
    workspace = Path(policy.workspace)
    if artifacts.state_dir == workspace or artifacts.state_dir.is_relative_to(workspace):
        raise DebugExecutionError("debug evidence state must be outside the source checkout")
    packet_dir = _packet_dir(artifacts, case_id)
    if any(path.is_symlink() for path in (artifacts.state_dir / "packets", packet_dir)):
        raise DebugExecutionError("debug invocation records cannot use symlink directories")
    if any(Path(root) == artifacts.state_dir or Path(root).is_relative_to(artifacts.state_dir)
           or artifacts.state_dir.is_relative_to(Path(root)) for root in policy.write_paths):
        raise DebugExecutionError("child write authority cannot include controller evidence or invocation records")
    if source_identity(workspace) != execution["source"]:
        raise DebugExecutionError("source changed since execution was authorized")
    _verify_assets(contract)
    for command in contract["commands"]:
        if executable_digest(command["argv"][0]) != command["executable_digest"]:
            raise DebugExecutionError("reproduction executable changed after review")
        cwd = (workspace / command["cwd"]).resolve(strict=True)
        if not cwd.is_relative_to(workspace) or str(cwd) != str(workspace / command["cwd"]):
            raise DebugExecutionError("reproduction cwd escapes or changes its frozen path")
    backend = select_backend(policy, invocation_root=packet_dir)
    debugger._execution_commit("DEBUG_EXECUTION_STARTED", case_id, EXECUTOR, execution_id=execution_id,
                               owner_pid=os.getpid(), owner_started=process_start_fingerprint(os.getpid()))
    results = []
    try:
        for command in contract["commands"]:
            _verify_assets(contract)
            result = await backend.run(
                command["argv"], cwd=workspace / command["cwd"], policy=policy,
                timeout_seconds=command["timeout_seconds"], environment=contract["environment"],
                invocation_record=_invocation_path(packet_dir, execution_id, command["command_id"]),
            )
            output = {}
            for channel in ("stdout", "stderr"):
                ref = artifacts.put_bytes(
                    getattr(result, channel), producer={"run_id": debugger.run_id, "invocation_id": execution_id,
                                                       "channel": channel, "role": EXECUTOR},
                    media_type="text/plain", redact=True,
                    source_sha256=getattr(result, channel + "_sha256"),
                    source_bytes=getattr(result, channel + "_bytes"),
                    truncated=getattr(result, channel + "_truncated"),
                )
                output[channel] = ref.to_dict()
            start = datetime.fromisoformat(result.started_at.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(result.finished_at.replace("Z", "+00:00"))
            results.append(dict(output, command_id=command["command_id"], argv=list(result.argv), cwd=result.cwd,
                                exit_code=result.exit_code, passed=result.exit_code in command["expected_exit_codes"],
                                started_at=result.started_at, finished_at=result.finished_at,
                                duration_ms=int((finish - start).total_seconds() * 1000), backend=result.backend,
                                policy_digest=result.policy_digest))
        after = source_identity(workspace)
        _verify_assets(contract)
        receipt = dict(schema="camol.debug_execution_receipt", schema_version=1, execution_id=execution_id,
                       contract_digest=canonical_digest(contract), source=execution["source"], source_after=after,
                       environment_digest=environment_digest(contract), results=results)
        debugger._execution_commit("DEBUG_EXECUTION_FINISHED", case_id, EXECUTOR, execution_id=execution_id, receipt=receipt)
    except BaseException as error:
        debugger._execution_commit("DEBUG_EXECUTION_INTERRUPTED", case_id, EXECUTOR, execution_id=execution_id,
                                   reason="execution ended without a valid complete receipt: " + type(error).__name__)
        raise
    return collect_execution(debugger, case_id, execution_id, artifacts)


def collect_execution(debugger: Any, case_id: str, execution_id: str, artifacts: ArtifactStore) -> list:
    state = debugger._state()
    case = debugger.inspect(case_id)
    execution = case.get("executions", {}).get(execution_id)
    if not execution or execution["status"] != "finished":
        raise DebugExecutionError("only a complete kernel receipt can recover evidence")
    contract = case["execution_contract"]
    results = validate_receipt(execution["receipt"], contract, execution)["results"]
    identities = []
    for command, result in zip(contract["commands"], results):
        refs = [ArtifactRef.from_dict(result[name]) for name in ("stdout", "stderr")]
        refs = tuple({ref.digest: ref for ref in refs}.values())
        for ref in refs:
            artifacts.verify(ref)
        existing = [EvidenceRecord.from_dict(state["evidence"][identity]) for identity in case["evidence_ids"]
                    if state["evidence"][identity].get("epistemic_status") == "EXECUTED"
                    and state["evidence"][identity].get("data", {}).get("execution_id") == execution_id
                    and state["evidence"][identity].get("data", {}).get("command_id") == command["command_id"]]
        if existing:
            validate_executed_evidence(state, case, existing[0])
            identities.append(existing[0].evidence_id)
            continue
        record = EvidenceRecord(
            evidence_id=uuid4().hex, run_id=debugger.run_id, task_id=None, debug_case_id=case_id,
            agent_id=None, lease_id=None, fence_digest=None, kind="test_result", epistemic_status="EXECUTED",
            producer=EXECUTOR, observed_at=debugger.orchestrator._now(),
            data=evidence_data(case, execution, command, result), artifact_refs=refs,
        )
        # Reference equality also catches a redaction collision in argv metadata.
        validate_executed_evidence(debugger._state(), case, record)
        state = debugger._state()
        debugger.orchestrator._emit(debugger.run_id, "EVIDENCE_RECORDED", record.to_dict(),
                                     actor_id=EXECUTOR, expected_seq=state["last_seq"])
        identities.append(record.evidence_id)
    return identities


def reconcile(debugger: Any, case_id: str, execution_id: str, artifacts: ArtifactStore,
              *, approved_by: str, resolution: str) -> dict:
    """Consume an abandoned attempt only after its executor and children are gone.

    This never terminates an arbitrary PID. The supervisor's existing explicit
    force-stop path can terminate correctly identified orphans first.
    """
    case = debugger.inspect(case_id)
    execution = case.get("executions", {}).get(execution_id)
    if not execution or execution["status"] not in {"authorized", "running"}:
        raise DebugExecutionError("execution is not pending reconciliation")

    def alive(pid: int, marker: str) -> bool:
        try:
            return process_start_fingerprint(pid) == marker
        except SandboxError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                pass
            raise DebugExecutionError("process liveness cannot be proven; reconcile with the supervisor")

    if execution["status"] == "running" and alive(execution["owner_pid"], execution["owner_started"]):
        raise DebugExecutionError("the debug executor is still alive; cancel and await it first")
    packet_dir = _packet_dir(artifacts, case_id)
    for command in case["execution_contract"]["commands"]:
        path = _invocation_path(packet_dir, execution_id, command["command_id"])
        if path.is_symlink() or packet_dir.is_symlink() or packet_dir.parent.is_symlink():
            raise DebugExecutionError("invocation record paths cannot be symlinks")
        if not path.exists():
            continue
        record = validate_process_invocation(json.loads(path.read_text(encoding="utf-8")))
        if (record["argv_digest"] != canonical_digest(command["argv"])
                or record["policy_digest"] != SandboxPolicy.from_dict(case["execution_contract"]["sandbox_policy"]).digest()):
            raise DebugExecutionError("invocation record does not match frozen execution")
        if record["state"] == "active" and alive(record["pid"], record["process_started"]):
            raise DebugExecutionError("a debug subprocess is still alive; use supervisor orphan reconciliation")
    return debugger._execution_commit("DEBUG_EXECUTION_INTERRUPTED", case_id, approved_by,
                                       execution_id=execution_id, reason=resolution)
