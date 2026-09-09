"""Private subprocess entry point for the pinned official SWE-bench grader.

This is not a worker tool. It receives protected reference data and Docker access.
The public suite adapter denies these paths to all model/worker processes.
"""

import importlib
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camol.benchmark import BenchmarkError
from camol.schema import canonical_digest, require_digest
from camol.swebench import HARNESS_COMMIT, MAX_PATCH_BYTES, _environment, _exact, _grade, _json, _read, _safe_id, _test_ids, bytes_digest, verify_harness_checkout


def _packet(packet):
    _exact(packet, {"request", "harness_checkout", "invocation_id", "timeout_seconds"}, "official bridge packet")
    request = packet["request"]
    _exact(request, {"schema", "schema_version", "task", "manifest_digest", "lock_digest", "candidate_digest",
                     "patch", "purpose", "prepared", "image_id", "environment", "harness_commit"}, "official grade request")
    _exact(request["task"], {"task_id", "task_digest", "source_revision", "evaluator_digest", "environment_digest", "public_tests_digest", "family"}, "grade task")
    if (not isinstance(request["patch"], str) or len(request["patch"].encode("utf-8")) > MAX_PATCH_BYTES
            or not isinstance(request["prepared"], dict)):
        raise BenchmarkError("grade patch/evaluator must be bounded data")
    if (request["schema"] != "camol.swebench_grade_request" or type(request["schema_version"]) is not int or request["schema_version"] != 1
            or request["harness_commit"] != HARNESS_COMMIT or request["purpose"] not in {"gold", "noop", "candidate"}
            or request["candidate_digest"] != bytes_digest(request["patch"].encode("utf-8"))
            or request["prepared"]["instance_id"] != request["task"]["task_id"]
            or request["prepared"]["base_commit"] != request["task"]["source_revision"]):
        raise BenchmarkError("invalid official bridge candidate/pin")
    _safe_id(packet["invocation_id"])
    _safe_id(request["task"]["task_id"])
    if (not isinstance(request["prepared"].get("image"), str)
            or not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", request["prepared"]["image"])
            or request["prepared"].get("image_assets") not in (None, {}, "{}")
            or (request["purpose"] == "noop" and request["patch"] != "")):
        raise BenchmarkError("offline image/oracle pin or negative control changed")
    _environment(request["environment"])
    for field in ("manifest_digest", "lock_digest", "candidate_digest", "image_id"):
        require_digest(request[field], field)
    expected_environment = dict(policy=request["environment"], image_ref=request["prepared"]["image"], image_id=request["image_id"])
    if canonical_digest(expected_environment) != request["task"]["environment_digest"]:
        raise BenchmarkError("official environment digest does not describe requested image/resources")
    if not _test_ids(request["prepared"]["FAIL_TO_PASS"]):
        raise BenchmarkError("negative control requires nonempty issue tests")
    _test_ids(request["prepared"]["PASS_TO_PASS"])
    if type(packet["timeout_seconds"]) is not int or not 0 < packet["timeout_seconds"] <= 86400:
        raise BenchmarkError("invalid grader time limit")
    return request


def _labels(packet):
    return {"camol.swebench.invocation": packet["invocation_id"],
            "camol.swebench.request": canonical_digest(packet["request"])}


def cleanup(client, packet):
    labels = _labels(packet)
    for container in client.containers.list(all=True, filters={"label": [key + "=" + value for key, value in labels.items()]}):
        actual = container.attrs.get("Config", {}).get("Labels", {})
        if any(actual.get(key) != value for key, value in labels.items()):
            raise BenchmarkError("refusing cleanup of a container with another invocation owner")
        container.remove(force=True)
    if client.containers.list(all=True, filters={"label": [key + "=" + value for key, value in labels.items()]}):
        raise BenchmarkError("grader containers did not drain")


class _Images:
    def __init__(self, client, request):
        self.client, self.request = client, request

    def get(self, name):
        if name != self.request["prepared"]["image"]:
            raise BenchmarkError("official grader requested an unpinned image")
        image = self.client.images.get(name)
        if (image.id != self.request["image_id"]
                or image.attrs.get("Architecture") != self.request["environment"]["architecture"]
                or image.attrs.get("Os") != "linux"):
            raise BenchmarkError("local Docker image bytes/platform differ from frozen environment")
        return image

    def pull(self, *args, **kwargs):
        raise BenchmarkError("offline evaluation never downloads images")


class _Containers:
    def __init__(self, client, packet):
        self.client, self.packet = client, packet

    def get(self, name):
        expected = "sweb.eval." + self.packet["request"]["task"]["task_id"].lower() + "." + self.packet["invocation_id"]
        if name != expected:
            raise BenchmarkError("refusing another grader's container name")
        container = self.client.containers.get(name)
        if any(container.attrs.get("Config", {}).get("Labels", {}).get(key) != value for key, value in _labels(self.packet).items()):
            raise BenchmarkError("existing container does not belong to this invocation")
        return container

    def create(self, **kwargs):
        request = self.packet["request"]
        expected = "sweb.eval." + request["task"]["task_id"].lower() + "." + self.packet["invocation_id"]
        if kwargs.get("image") != request["prepared"]["image"] or kwargs.get("name") != expected:
            raise BenchmarkError("official container identity changed")
        allowed = {"image", "name", "user", "detach", "command", "cap_add"}
        if set(kwargs) - allowed:
            raise BenchmarkError("official grader requested unreviewed Docker privileges")
        # Upstream supports privileged browser tasks. This adapter's frozen
        # offline Python tier deliberately does not grant SYS_ADMIN or egress.
        environment = request["environment"]
        kwargs.update(cap_add=[], cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                      network_disabled=True, mem_limit=environment["memory_bytes"],
                      nano_cpus=environment["cpu_millis"] * 1000000, pids_limit=environment["pids_limit"],
                      labels=_labels(self.packet), platform="linux/" + environment["architecture"])
        container = self.client.containers.create(**kwargs)
        container.reload()
        actual = container.attrs
        host = actual.get("HostConfig", {})
        if (actual.get("Image") != request["image_id"] or host.get("NetworkMode") != "none"
                or host.get("Memory") != environment["memory_bytes"] or host.get("NanoCpus") != environment["cpu_millis"] * 1000000
                or host.get("PidsLimit") != environment["pids_limit"] or host.get("Privileged") is not False
                or host.get("Binds") or host.get("CapAdd")
                or "ALL" not in (host.get("CapDrop") or [])
                or not any(item.startswith("no-new-privileges") for item in host.get("SecurityOpt", []))):
            raise BenchmarkError("Docker readback failed frozen resource/isolation constraints")
        if any(actual.get("Config", {}).get("Labels", {}).get(key) != value for key, value in _labels(self.packet).items()):
            raise BenchmarkError("Docker readback lost invocation owner")
        return container


class GuardedDockerClient:
    """Narrow surface used by the pinned run_instance, not general Docker API."""
    def __init__(self, client, packet):
        self.images = _Images(client, packet["request"])
        self.containers = _Containers(client, packet)


def _load_official(harness):
    root = Path.cwd() / "official-source"
    verify_harness_checkout(harness, snapshot=root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    module = importlib.import_module("swebench.harness.run_evaluation")
    if Path(module.__file__).resolve() != root / "swebench" / "harness" / "run_evaluation.py":
        raise BenchmarkError("wrong installed official harness was imported")
    return module


def run_official(packet, directory, client, official):
    """Injection point for protocol tests. Actual caller loads pinned official code."""
    request = _packet(packet)
    start = time.monotonic()
    # cwd is an exclusive random invocation directory: official cached report
    # identities do not bind patch content, so reuse is never authorized here.
    log_root = directory / "logs" / "evaluation" / packet["invocation_id"]
    if log_root.exists():
        raise BenchmarkError("official output directory already exists; cached grades are not fresh execution")
    spec = official.make_test_spec(request["prepared"])
    prediction = dict(instance_id=request["task"]["task_id"], model_name_or_path="camol-candidate", model_patch=request["patch"])
    original_cleanup = getattr(official, "cleanup_container", None)
    # Upstream invokes an unpinned 'docker' executable for cleanup. Use exact
    # owner-label Docker API cleanup instead, within this private subprocess.
    official.cleanup_container = lambda *_args, **_kwargs: cleanup(client, packet)
    try:
        result = official.run_instance(spec, prediction, GuardedDockerClient(client, packet), packet["invocation_id"],
                                      timeout=packet["timeout_seconds"], rewrite_reports=False,
                                      skip_patch=request["purpose"] == "noop" or request["patch"] == "", task_repo=None)
        if not isinstance(result, tuple) or len(result) != 2 or result[0] != request["task"]["task_id"]:
            raise BenchmarkError("official grader did not complete this instance")
        instance_dir = log_root / "camol-candidate" / request["task"]["task_id"]
        report = _json(_read(instance_dir / "report.json"))
        if report != result[1]:
            raise BenchmarkError("official returned report differs from protected disk evidence")
        test_output = _read(instance_dir / "test_output.txt")
        if not test_output.strip():
            raise BenchmarkError("official grader produced empty test evidence")
    finally:
        try:
            cleanup(client, packet)
        finally:
            official.cleanup_container = original_cleanup
    receipt = dict(schema="camol.swebench_grade", schema_version=1, request_digest=canonical_digest(request),
                   report=report, report_digest=canonical_digest(report), test_output_digest=bytes_digest(test_output),
                   environment_digest=request["task"]["environment_digest"], candidate_digest=request["candidate_digest"],
                   invocation_id=packet["invocation_id"], elapsed_ms=int((time.monotonic() - start) * 1000), cleanup_complete=True)
    _grade(receipt, request)
    return receipt


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in {"grade", "cleanup"}:
        raise BenchmarkError("private bridge expects grade|cleanup and protected request")
    request_path = Path(args[1]).absolute()
    packet = _json(_read(request_path))
    _packet(packet)
    if request_path.parent.resolve() != Path.cwd().resolve():
        raise BenchmarkError("bridge cwd must be its private invocation directory")
    import docker
    # No inherited DOCKER_HOST/TLS/key credentials. This tier intentionally uses
    # only the host's already configured local Unix Docker endpoint.
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=30)
    try:
        if args[0] == "cleanup":
            cleanup(client, packet)
            return
        official = _load_official(packet["harness_checkout"])
        receipt = run_official(packet, request_path.parent, client, official)
        descriptor = os.open(str(request_path.parent / "receipt.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream, sort_keys=True, allow_nan=False)
    finally:
        client.close()


if __name__ == "__main__":
    main()
