"""Pinned, offline SWE-bench Verified adapter for the trusted campaign host.

No dataset/image download or model call is performed by import or inspection.
Arm executors are trusted host plugins, not worker-provided callbacks/receipts.
The bundled grader calls the pinned official harness in a separate process;
unit tests of that interface are not SWE-bench scores.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
from typing import Protocol
import uuid

from .benchmark import BenchmarkError, BenchmarkTrial
from .campaign import _trial_identity, validate_campaign
from .git_safety import GitSafetyError, safe_git_argv
from .schema import canonical_digest, require_digest


HARNESS_COMMIT = "87ab1f6ced28f75ba73ca899dc759b019310944a"  # official v5.0.1
SUITE = "swe-bench-verified"
RAW_FIELDS = frozenset({"repo", "instance_id", "base_commit", "patch", "test_patch",
                        "problem_statement", "hints_text", "created_at", "version",
                        "FAIL_TO_PASS", "PASS_TO_PASS", "environment_setup_commit", "difficulty"})
PREPARED_EXTRA = frozenset({"image", "eval_script", "log_parser", "eval_type", "image_assets", "split"})
PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PATCH_BYTES = 4 * 1024 * 1024


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise BenchmarkError(label + " has missing or unknown fields")


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise BenchmarkError(label + " must be a bounded nonnegative integer")
    return value


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}", value):
        raise BenchmarkError("unsafe SWE-bench instance identity")
    return value


def _commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise BenchmarkError("SWE-bench revision must be a full pinned commit")
    return value


def bytes_digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def verify_harness_checkout(checkout, *, snapshot=None, expected_commit=HARNESS_COMMIT):
    """Verify imported package bytes, independent of stat/index/filter caches.

    A private snapshot contains only verified committed package files, excluding
    ignored modules and bytecode. Python can otherwise load poisoned __pycache__
    even after tracked source bytes are restored to the same size and mtime.
    """
    root = Path(checkout).resolve(strict=True)
    environment = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    def read(*arguments):
        try:
            argv = safe_git_argv("/usr/bin/git", ["-C", str(root), *arguments], env=environment)
            result = subprocess.run(argv, env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        except (GitSafetyError, OSError, subprocess.TimeoutExpired) as error:
            raise BenchmarkError("official source identity could not be safely inspected") from error
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise BenchmarkError("official source inventory is invalid or exceeds its bound")
        return result.stdout
    if (read("rev-parse", "HEAD^{commit}").decode().strip() != expected_commit
            or Path(read("rev-parse", "--show-toplevel").decode().strip()).resolve() != root
            or read("status", "--porcelain", "--untracked-files=normal").strip()):
        raise BenchmarkError("official harness must be a clean checkout at the supported pinned commit")
    files, total = {}, 0
    for entry in read("ls-tree", "-rz", "--full-tree", "HEAD", "--", "swebench").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split()
        relative = Path(name.decode("utf-8", "strict"))
        if (relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "swebench"
                or mode not in {"100644", "100755"} or kind != "blob"
                or relative.suffix in {".pyc", ".pyo"} or "__pycache__" in relative.parts or len(files) >= 10000):
            raise BenchmarkError("unsupported executable-package path or object type")
        path = root
        for component in relative.parts:
            path = path / component
            if path.is_symlink():
                raise BenchmarkError("official executable source cannot traverse symlinks")
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024 * 1024:
                raise BenchmarkError("official source file is not bounded regular data")
            data = stream.read(32 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        total += len(data)
        if total > 128 * 1024 * 1024 or len(data) != before.st_size:
            raise BenchmarkError("official source exceeds inventory limit or changed while reading")
        hasher = hashlib.sha1() if len(blob) == 40 else hashlib.sha256()
        hasher.update(("blob " + str(len(data)) + "\0").encode("ascii"))
        hasher.update(data)
        if (hasher.hexdigest() != blob or bool(before.st_mode & 0o111) != (mode == "100755")
                or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise BenchmarkError("official executable source bytes differ from their pinned Git object")
        files[relative] = data
    if Path("swebench/harness/run_evaluation.py") not in files or read("rev-parse", "HEAD^{commit}").decode().strip() != expected_commit:
        raise BenchmarkError("official source is missing or changed during verification")
    if snapshot is not None:
        destination = Path(snapshot)
        destination.mkdir(mode=0o700, exist_ok=False)
        for relative, data in files.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
    return canonical_digest({str(name): bytes_digest(data) for name, data in files.items()})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError("duplicate JSON object key")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(BenchmarkError("nonfinite JSON")))
    except (ValueError, UnicodeError) as error:
        raise BenchmarkError("invalid SWE-bench JSON") from error


def _read(path, maximum=MAX_FILE_BYTES):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise BenchmarkError("expected a bounded regular, non-symlink benchmark file")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise BenchmarkError("benchmark file exceeded size ceiling")
    return data


def _rows(path):
    data = _read(path)
    if str(path).endswith(".jsonl"):
        rows = [_json(line) for line in data.splitlines() if line.strip()]
    elif str(path).endswith(".json"):
        rows = _json(data)
    else:
        raise BenchmarkError("offline ingestion accepts local JSON or JSONL only")
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise BenchmarkError("Verified cohort must contain 1..500 rows")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BenchmarkError("dataset row must be an object")
        identity = _safe_id(row.get("instance_id"))
        if identity in indexed:
            raise BenchmarkError("duplicate dataset instance")
        indexed[identity] = row
    return bytes_digest(data), indexed


def _test_ids(value):
    result = _json(value) if isinstance(value, str) else value
    if (not isinstance(result, list) or any(not isinstance(item, str) or not item for item in result)
            or len(result) != len(set(result))):
        raise BenchmarkError("invalid or duplicate reference test identifiers")
    return result


def _environment(value):
    _exact(value, {"architecture", "network", "memory_bytes", "cpu_millis", "pids_limit"}, "grader environment")
    if value["architecture"] not in {"amd64", "arm64"} or value["network"] != "none":
        raise BenchmarkError("offline grader requires pinned architecture and no network")
    for field in ("memory_bytes", "cpu_millis", "pids_limit"):
        _int(value[field], field, 1)
    return deepcopy(value)


def freeze_offline_lock(dataset_path, prepared_path, *, dataset_revision, images, environment):
    """Read-only proposal. The owner must review/freeze this lock with a campaign.

    ``images`` maps each chosen instance to exact local Docker image ID and a
    digest-pinned registry reference, plus the baseline public-test digest.
    Prepared rows come from the reviewed official task export; they are not
    generated from model output. Download provenance is owner-supplied, not
    falsely described as independently verified by this local importer.
    """
    dataset_digest, raw = _rows(dataset_path)
    prepared_digest, prepared = _rows(prepared_path)
    if not isinstance(images, dict) or not images or not set(images) <= set(raw) or not set(images) <= set(prepared):
        raise BenchmarkError("chosen images must match both offline datasets")
    tasks = []
    for identity in sorted(images):
        row, compiled = raw[identity], prepared[identity]
        _exact(row, RAW_FIELDS, "Verified row")
        if not set(compiled) <= RAW_FIELDS | PREPARED_EXTRA or not (RAW_FIELDS | {"image", "eval_script", "log_parser", "eval_type"}) <= set(compiled):
            raise BenchmarkError("prepared task has unsupported fields")
        for field in RAW_FIELDS:
            if compiled[field] != row[field] or not isinstance(row[field], str):
                raise BenchmarkError("prepared task changed original Verified data")
        _commit(row["base_commit"])
        _commit(row["environment_setup_commit"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo"]):
            raise BenchmarkError("invalid repository identity")
        if not _test_ids(row["FAIL_TO_PASS"]) or not row["patch"].strip() or not row["test_patch"].strip():
            raise BenchmarkError("gold/no-op preflight needs a real patch and nonempty failing-test oracle")
        if set(_test_ids(row["FAIL_TO_PASS"])) & set(_test_ids(row["PASS_TO_PASS"])):
            raise BenchmarkError("reference test sets overlap")
        if (compiled.get("image_assets") not in (None, {}, "{}") or compiled.get("split", "test") != "test"
                or any(not isinstance(compiled[name], str) or not compiled[name] for name in ("eval_script", "log_parser", "eval_type"))):
            raise BenchmarkError("only offline text tasks with explicit evaluator are supported")
        pin = images[identity]
        _exact(pin, {"image_ref", "image_id", "public_tests_digest"}, "image pin")
        for field in ("image_id", "public_tests_digest"):
            require_digest(pin[field], field)
        if not isinstance(pin["image_ref"], str) or not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", pin["image_ref"]):
            raise BenchmarkError("image reference must use an immutable registry digest")
        # Replacing a mutable source tag is explicit in the lock, not an implicit
        # assumption that a downloaded 'latest' tag names the pinned bytes.
        tasks.append(dict(instance_id=identity, task_digest=canonical_digest(row),
                          prepared_task_digest=canonical_digest(compiled), **pin))
    return dict(schema="camol.swebench_lock", schema_version=1,
                dataset_id="princeton-nlp/SWE-bench_Verified", dataset_revision=_commit(dataset_revision),
                dataset_digest=dataset_digest, prepared_digest=prepared_digest, harness_commit=HARNESS_COMMIT,
                environment=_environment(environment), instances=tasks)


class OfflineVerifiedDataset:
    """Read-only protected oracle. Do not give this object or its paths to workers."""

    def __init__(self, dataset_path, prepared_path, lock):
        _exact(lock, {"schema", "schema_version", "dataset_id", "dataset_revision", "dataset_digest",
                      "prepared_digest", "harness_commit", "environment", "instances"}, "SWE-bench lock")
        if lock["schema"] != "camol.swebench_lock" or type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
            raise BenchmarkError("unsupported SWE-bench lock")
        if not isinstance(lock["instances"], list) or not lock["instances"]:
            raise BenchmarkError("empty SWE-bench lock")
        images = {}
        for item in lock["instances"]:
            _exact(item, {"instance_id", "task_digest", "prepared_task_digest", "image_ref", "image_id", "public_tests_digest"}, "locked task")
            if item["instance_id"] in images:
                raise BenchmarkError("duplicate locked task")
            images[item["instance_id"]] = {field: item[field] for field in ("image_ref", "image_id", "public_tests_digest")}
        actual = freeze_offline_lock(dataset_path, prepared_path, dataset_revision=lock["dataset_revision"],
                                    images=images, environment=lock["environment"])
        if actual != lock:
            raise BenchmarkError("offline dataset, prepared evaluator or harness pin changed")
        self.lock = deepcopy(lock)
        self.paths = (Path(dataset_path).resolve(), Path(prepared_path).resolve())
        raw_digest, self._raw = _rows(dataset_path)
        prepared_digest, self._prepared = _rows(prepared_path)
        if raw_digest != lock["dataset_digest"] or prepared_digest != lock["prepared_digest"]:
            raise BenchmarkError("offline oracle changed during ingestion")
        self._pins = {item["instance_id"]: item for item in actual["instances"]}

    def public_task(self, identity):
        if identity not in self._pins:
            raise BenchmarkError("task is outside pinned cohort")
        return {name: self._raw[identity][name] for name in PUBLIC_FIELDS}

    def task(self, identity):
        public = self.public_task(identity)
        pin = self._pins[identity]
        evaluator = {"harness_commit": HARNESS_COMMIT, "prepared_task_digest": pin["prepared_task_digest"],
                     "adapter_protocol": "camol.swebench_official/v1"}
        environment = dict(policy=self.lock["environment"], image_ref=pin["image_ref"], image_id=pin["image_id"])
        return dict(task_id=identity, task_digest=pin["task_digest"], source_revision=public["base_commit"],
                    evaluator_digest=canonical_digest(evaluator), environment_digest=canonical_digest(environment),
                    public_tests_digest=pin["public_tests_digest"], family=public["repo"])

    def grade_request(self, identity, patch, purpose, manifest_digest):
        if not isinstance(patch, str) or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise BenchmarkError("candidate patch is not bounded text")
        if purpose not in {"gold", "noop", "candidate"}:
            raise BenchmarkError("invalid grade purpose")
        task, pin = self.task(identity), self._pins[identity]
        prepared = deepcopy(self._prepared[identity])
        prepared["image"] = pin["image_ref"]
        return dict(schema="camol.swebench_grade_request", schema_version=1, task=task,
                    manifest_digest=manifest_digest, lock_digest=canonical_digest(self.lock),
                    candidate_digest=bytes_digest(patch.encode("utf-8")), patch=patch, purpose=purpose,
                    prepared=prepared, image_id=pin["image_id"], environment=self.lock["environment"],
                    harness_commit=HARNESS_COMMIT)


@dataclass(frozen=True)
class ArmCapabilities:
    """Trusted host registration; never deserialized from an agent message.

    'provider_proxy' means the host enforces token/USD limits BEFORE each
    provider request. CLI-only observations are insufficient. A kernel-isolated
    worker must not be able to read or change any protected path or other arms.
    """
    executor_id: str
    implementation_digest: str
    budget_enforcement: str
    isolation: str
    exact_model_version: bool
    source_and_environment_pins: bool

    def require_ready(self):
        _safe_id(self.executor_id)
        require_digest(self.implementation_digest, "executor implementation")
        if (self.budget_enforcement != "provider_proxy" or self.isolation != "kernel"
                or self.exact_model_version is not True or self.source_and_environment_pins is not True):
            raise BenchmarkError("arm executor cannot enforce frozen model/budget/oracle isolation; CLI-only capability is insufficient")


class ArmExecutor(Protocol):
    capabilities: ArmCapabilities

    async def preflight(self, public_task, manifest, protected_paths):
        """Trusted host proves actual isolation and model/budget enforcement."""

    async def execute(self, public_task, arm, seed, manifest, request_digest):
        """Return host-observed receipt, patch and usage, never self-graded score."""

    async def teardown(self, trial_id):
        """Drain only this trial's processes/resources, and prove cleanup."""


def _grade(receipt, request):
    _exact(receipt, {"schema", "schema_version", "request_digest", "report", "report_digest", "test_output_digest",
                     "environment_digest", "candidate_digest", "invocation_id", "elapsed_ms", "cleanup_complete"}, "grade receipt")
    if (receipt["schema"] != "camol.swebench_grade" or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["request_digest"] != canonical_digest(request)
            or receipt["environment_digest"] != request["task"]["environment_digest"]
            or receipt["candidate_digest"] != request["candidate_digest"]
            or receipt["cleanup_complete"] is not True):
        raise BenchmarkError("grader changed candidate/environment identity or cleanup is incomplete")
    _safe_id(receipt["invocation_id"])
    _int(receipt["elapsed_ms"], "grader elapsed")
    require_digest(receipt["test_output_digest"], "official test output")
    if canonical_digest(receipt["report"]) != receipt["report_digest"]:
        raise BenchmarkError("official report digest mismatch")
    identity = request["task"]["task_id"]
    report = receipt["report"]
    if not isinstance(report, dict) or set(report) != {identity} or not isinstance(report[identity], dict):
        raise BenchmarkError("official report belongs to another instance")
    record = report[identity]
    for key in ("resolved", "patch_successfully_applied", "infra_failure"):
        if type(record.get(key)) is not bool:
            raise BenchmarkError("official report lacks typed execution status")
    if record["infra_failure"] or not record["patch_successfully_applied"]:
        raise BenchmarkError("official grader produced infrastructure or unparseable execution evidence")
    statuses = record.get("tests_status")
    if not isinstance(statuses, dict):
        raise BenchmarkError("missing official per-test statuses")
    failures = {}
    for family in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        value = statuses.get(family)
        _exact(value, {"success", "failure"}, "official test statuses")
        passed, failed = _test_ids(value["success"]), _test_ids(value["failure"])
        expected = _test_ids(request["prepared"][family])
        if set(passed) & set(failed) or set(passed) | set(failed) != set(expected):
            raise BenchmarkError("official grader omitted, added or duplicated reference tests")
        failures[family] = len(failed)
    if record["resolved"] != (sum(failures.values()) == 0):
        raise BenchmarkError("official resolved bit conflicts with test results")
    return dict(accepted=record["resolved"], regressions=failures["PASS_TO_PASS"], failing=failures["FAIL_TO_PASS"])


class SWEBenchVerifiedSuite:
    def __init__(self, dataset, grader, executors):
        self.dataset, self.grader, self.executors = dataset, grader, dict(executors)
        self._ready = set()

    def _validate(self, task, manifest):
        manifest = validate_campaign(manifest)
        if (manifest["suite"] != SUITE or manifest["suite_version"] != self.dataset.lock["dataset_revision"]
                or manifest["dataset_digest"] != self.dataset.lock["dataset_digest"]
                or task != self.dataset.task(task["task_id"]) or task not in manifest["tasks"]):
            raise BenchmarkError("campaign differs from pinned offline suite")
        if set(self.executors) != set(manifest["arms"]):
            raise BenchmarkError("all three matched arm executors must be registered")
        for executor in self.executors.values():
            if not isinstance(executor.capabilities, ArmCapabilities):
                raise BenchmarkError("arm capability must come from trusted host registration")
            executor.capabilities.require_ready()
        return manifest

    async def validate_gold(self, task, manifest):
        manifest = self._validate(task, manifest)
        identity, digest = task["task_id"], canonical_digest(manifest)
        for arm, executor in self.executors.items():
            receipt = await executor.preflight(self.dataset.public_task(identity), deepcopy(manifest),
                                               tuple(self.dataset.paths) + tuple(self.grader.protected_paths))
            expected = dict(arm=arm, task_digest=task["task_digest"], manifest_digest=digest,
                            environment_digest=task["environment_digest"], public_tests_digest=task["public_tests_digest"],
                            executor_digest=executor.capabilities.implementation_digest,
                            budget_digest=canonical_digest(manifest["budgets"]), ready=True)
            if receipt != expected:
                raise BenchmarkError("host executor preflight did not prove the exact frozen trial")
        receipts, outcomes = [], []
        for purpose, patch in (("gold", self.dataset._raw[identity]["patch"]), ("noop", "")):
            request = self.dataset.grade_request(identity, patch, purpose, digest)
            receipt = await self.grader.grade(request, timeout_seconds=manifest["budgets"]["max_elapsed_ms"] / 1000)
            outcomes.append(_grade(receipt, request))
            receipts.append(receipt)
        if not outcomes[0]["accepted"] or outcomes[1]["accepted"] or not outcomes[1]["failing"] or outcomes[1]["regressions"]:
            raise BenchmarkError("gold must pass and a real no-op must fail only issue tests before model spend")
        self._ready.add((identity, digest))
        return dict(**{key: task[key] for key in ("task_id", "task_digest", "evaluator_digest", "environment_digest")},
                    manifest_digest=digest, gold_accepted=True, noop_rejected=True,
                    gold_evidence_digest=canonical_digest(receipts[0]), noop_evidence_digest=canonical_digest(receipts[1]),
                    budget_enforced=True)

    async def run_trial(self, task, arm, seed, manifest):
        manifest = self._validate(task, manifest)
        if (task["task_id"], canonical_digest(manifest)) not in self._ready:
            raise BenchmarkError("official gold/no-op preflight has not passed in this host session")
        if type(seed) is not int:
            raise BenchmarkError("paired seed must be an integer")
        repetition = seed - manifest["selection"]["seed"]
        if not 0 <= repetition < manifest["repetitions"] or arm not in self.executors:
            raise BenchmarkError("trial is outside frozen paired cohort")
        trial_id = _trial_identity(manifest, task, repetition, arm)
        request_digest = canonical_digest(dict(manifest=canonical_digest(manifest), task=task, arm=arm, seed=seed,
                                              executor=self.executors[arm].capabilities.implementation_digest))
        started = time.monotonic()
        receipt = await self.executors[arm].execute(self.dataset.public_task(task["task_id"]), arm, seed,
                                                   deepcopy(manifest), request_digest)
        _exact(receipt, {"request_digest", "patch", "tokens", "cost_usd_micros", "retries", "human_interventions",
                         "tool_calls", "invariant_violations", "recovery_success", "usage_observed", "evidence_complete",
                         "artifact_manifest_digest"}, "host arm result")
        if receipt["request_digest"] != request_digest or receipt["usage_observed"] is not True or receipt["evidence_complete"] is not True:
            raise BenchmarkError("arm returned unknown usage, incomplete evidence or wrong trial")
        require_digest(receipt["artifact_manifest_digest"], "host trial artifact manifest")
        for name in ("tokens", "cost_usd_micros", "retries", "human_interventions", "tool_calls", "invariant_violations"):
            _int(receipt[name], "host " + name)
        if type(receipt["recovery_success"]) is not bool:
            raise BenchmarkError("host recovery status must be boolean")
        request = self.dataset.grade_request(task["task_id"], receipt["patch"], "candidate", canonical_digest(manifest))
        remaining = manifest["budgets"]["max_elapsed_ms"] / 1000 - (time.monotonic() - started)
        if remaining <= 0:
            raise BenchmarkError("trial deadline exhausted before official grading")
        grade = await self.grader.grade(request, timeout_seconds=remaining)
        outcome = _grade(grade, request)
        elapsed = int((time.monotonic() - started) * 1000)
        trial = BenchmarkTrial(trial_id=trial_id, arm=arm,
            **{name: task[name] for name in ("task_id", "task_digest", "source_revision", "evaluator_digest")},
            **{name: manifest["configuration"][name] for name in ("model", "model_version", "effort", "tool_policy_digest")},
            budget_digest=canonical_digest(manifest["budgets"]), outcome="accepted" if outcome["accepted"] else "rejected",
            accepted_behavior=int(outcome["accepted"]), regressions=outcome["regressions"], elapsed_ms=elapsed,
            **{name: receipt[name] for name in ("tokens", "cost_usd_micros", "retries", "human_interventions", "tool_calls",
                                              "invariant_violations", "recovery_success", "evidence_complete")},
            recorded_at=datetime.now(timezone.utc).isoformat())
        return dict(trial=trial.to_dict(), candidate_digest=request["candidate_digest"],
                    artifact_manifest_digest=canonical_digest(dict(worker=receipt["artifact_manifest_digest"], grader=grade)),
                    environment_digest=task["environment_digest"], manifest_digest=canonical_digest(manifest), seed=seed,
                    usage_observed=True)

    async def teardown(self, trial_id):
        for executor in self.executors.values():
            await executor.teardown(trial_id)
        await self.grader.teardown(trial_id)


class OfficialSWEBenchGrader:
    """Concrete local, offline official-grader subprocess. Does not install tools.

    ``python`` must have the pinned official harness dependencies installed.
    Grader storage and checkout are protected paths the worker sandbox denies.
    Container pulls/builds/network/capability escalation are explicitly denied.
    """
    def __init__(self, *, python, harness_checkout, evidence_root):
        self.python = Path(python).absolute()
        self.harness = Path(harness_checkout).resolve()
        self.root = Path(evidence_root).absolute()
        if self.root.is_symlink() or not self.python.is_file():
            raise BenchmarkError("grader paths are invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid() or stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise BenchmarkError("official evidence root must be owner-only")
        self.protected_paths = (self.root.resolve(), self.harness)
        self._verify_pin()

    def _verify_pin(self):
        verify_harness_checkout(self.harness)

    async def _invoke(self, directory, mode, timeout):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(directory), "TMPDIR": str(directory),
                       "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "PYTHONNOUSERSITE": "1"}
        process = await asyncio.create_subprocess_exec(str(self.python), "-I", str(Path(__file__).with_name("swebench_bridge.py")),
                    mode, str(directory / "request.json"), cwd=directory, env=environment,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        if process.returncode:
            raise BenchmarkError("official grader bridge failed; inspect protected evidence")

    async def grade(self, request, *, timeout_seconds):
        self._verify_pin()
        invocation = "grade-" + canonical_digest(request).split(":")[1][:20] + "-" + uuid.uuid4().hex
        directory = self.root / invocation
        directory.mkdir(mode=0o700)
        packet = dict(request=deepcopy(request), harness_checkout=str(self.harness), invocation_id=invocation,
                      timeout_seconds=max(1, int(timeout_seconds)))
        descriptor = os.open(str(directory / "request.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(packet, stream, sort_keys=True, allow_nan=False)
        try:
            await self._invoke(directory, "grade", timeout_seconds)
        finally:
            # Killing a Python process does not stop a Docker container. Cleanup
            # is a separately awaited exact-label operation, even on cancellation.
            cleanup = asyncio.create_task(self._invoke(directory, "cleanup", 30))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        receipt = _json(_read(directory / "receipt.json"))
        _grade(receipt, request)
        return receipt

    async def teardown(self, trial_id):
        # Grade invocations drain synchronously, including timeouts. No shared
        # global image deletion, process sweep, or Docker prune is authorized.
        return None
