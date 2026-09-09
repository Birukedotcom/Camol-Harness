"""Tracked local control-plane preparation, separate from model execution.

The child prepares workspaces/probes but never opens the event store, reserves
shared capacity, leases a task, or launches an agent. The owner publishes results.
"""

import os
from pathlib import Path
import sys
from uuid import uuid4

from .admission import AdmissionBundle, AdmissionController, AdmissionError
from .json_contracts import decode_contract
from .runbook import runbook_digest, validate_runbook
from .sandbox import DeveloperTrustedBackend, SandboxError, SandboxPolicy
from .schema import canonical_json_bytes, parse_timestamp
from .workspace import WorkspaceError, WorkspaceManager


MAX_BYTES = 20 << 20  # Full 8-MiB plan plus selected task/agent and response overhead.
BOOTSTRAP = "import sys;sys.path.insert(0,sys.argv[1]);from camol.admission_process import main;main()"


class _Input:
    """Use the backend's bounded staged-input cancellation/pipe settlement."""
    def __init__(self, raw):
        self.raw = raw

    async def write(self, writer):
        writer.write(self.raw)
        await writer.drain()

    def feed(self, chunk):
        pass

    def eof(self):
        pass


def prepare(request):
    fields = {"schema", "schema_version", "source", "state_dir", "runbook", "target_id", "observed_at", "arguments"}
    if (not isinstance(request, dict) or set(request) != fields or request["schema"] != "camol.admission_preparation"
            or type(request["schema_version"]) is not int or request["schema_version"] != 1):
        raise AdmissionError("invalid admission preparation request")
    arguments = request["arguments"]
    expected = {"plan_digest", "task", "agent", "granted_by", "base_revision", "expected_evaluator_digest"}
    if not isinstance(arguments, dict) or set(arguments) != expected:
        raise AdmissionError("invalid admission preparation arguments")
    runbook = validate_runbook(request["runbook"])
    if runbook_digest(runbook) != arguments["plan_digest"]:
        raise AdmissionError("admission preparation differs from the frozen plan")
    for field, collection in (("task", "tasks"), ("agent", "agents")):
        value = arguments[field]
        if not isinstance(value, dict):
            raise AdmissionError("invalid preparation subject")
        frozen = next((item for item in runbook[collection] if item["id"] == value.get("id")), None)
        if frozen is None or any(value.get(key) != expected for key, expected in frozen.items()):
            raise AdmissionError("preparation subject differs from the frozen plan")
    observed = parse_timestamp(request["observed_at"], "admission preparation time")
    manager = WorkspaceManager(Path(request["source"]), Path(request["state_dir"]))
    controller = AdmissionController(runbook, manager, target_id=request["target_id"], clock=lambda: observed)
    bundle, _ = controller.prepare(**arguments)
    return dict(schema="camol.admission_prepared", schema_version=1, bundle=bundle.to_dict())


def main():
    try:
        request = decode_contract(sys.stdin.buffer.read(MAX_BYTES + 1), max_bytes=MAX_BYTES)
        result = prepare(request)
    except Exception as error:
        # No raw exception text: subprocess/path/provider errors may carry secrets.
        kind = "workspace" if isinstance(error, WorkspaceError) else "sandbox" if isinstance(error, SandboxError) else "admission"
        result = dict(schema="camol.admission_preparation_error", schema_version=1, kind=kind)
    raw = canonical_json_bytes(result)
    if len(raw) > MAX_BYTES:
        raw = canonical_json_bytes(dict(schema="camol.admission_preparation_error", schema_version=1, kind="admission"))
    sys.stdout.buffer.write(raw)


async def prepare_async(controller, arguments):
    request = dict(schema="camol.admission_preparation", schema_version=1,
        source=str(controller.workspaces.source), state_dir=str(controller.state_dir),
        runbook=controller.runbook, target_id=controller.target_id,
        observed_at=controller.clock().isoformat(), arguments=arguments)
    raw = canonical_json_bytes(request)
    if len(raw) > MAX_BYTES:
        raise AdmissionError("admission preparation exceeds its byte ceiling")
    package_root = Path(__file__).resolve().parent.parent
    policy = SandboxPolicy(policy_id="control-admission-preparation", workspace=str(package_root),
        read_paths=(str(package_root), str(controller.workspaces.source), str(controller.state_dir)),
        write_paths=(str(controller.state_dir), str(controller.workspaces.git_common_dir)), environment_names=tuple(sorted(os.environ)),
        network_destinations=(), credential_refs=(), trust_tier="developer_trusted")
    # This trusted controller helper is not a sandboxed model worker. Its real
    # child lifecycle uses the same bounded cleanup and durable orphan records.
    backend = DeveloperTrustedBackend()
    backend.max_capture_bytes = MAX_BYTES
    backend.invocation_root = controller.state_dir / "packets" / controller.runbook["run"]["id"]
    directory = backend.invocation_root / "admission-preparation"
    if directory.exists() and len(list(directory.glob("*.invocation.json"))) >= 10000:
        raise AdmissionError("admission preparation history requires owner archival")
    record = directory / (uuid4().hex + ".invocation.json")
    outcome = await backend.run([sys.executable, "-I", "-c", BOOTSTRAP, str(package_root)],
        cwd=package_root, policy=policy, timeout_seconds=60, environment=os.environ,
        input_protocol=_Input(raw), invocation_record=record)
    if outcome.exit_code != 0 or outcome.stdout_truncated:
        raise AdmissionError("admission preparation process failed or exceeded its output bound")
    response = decode_contract(outcome.stdout, max_bytes=MAX_BYTES)
    if (isinstance(response, dict) and set(response) == {"schema", "schema_version", "kind"}
            and response["schema"] == "camol.admission_preparation_error" and type(response["schema_version"]) is int
            and response["schema_version"] == 1):
        if response["kind"] not in ("workspace", "sandbox", "admission"):
            raise AdmissionError("invalid admission preparation error category")
        error = {"workspace": WorkspaceError, "sandbox": SandboxError}.get(response["kind"], AdmissionError)
        raise error("admission preparation failed; inspect the declared source/runtime prerequisites")
    if (not isinstance(response, dict) or set(response) != {"schema", "schema_version", "bundle"}
            or response["schema"] != "camol.admission_prepared" or type(response["schema_version"]) is not int
            or response["schema_version"] != 1):
        raise AdmissionError("invalid admission preparation response")
    bundle = AdmissionBundle.from_dict(response["bundle"])
    binding = bundle.binding
    if (binding.run_id, binding.plan_digest, binding.task_id, binding.worker_id, binding.box_id, binding.target_id) != (
            controller.runbook["run"]["id"], arguments["plan_digest"], arguments["task"]["id"],
            arguments["agent"]["id"], arguments["agent"]["id"], controller.target_id):
        raise AdmissionError("admission preparation returned another subject")
    handle = controller.workspaces.restore_receipt(bundle.workspace)
    if (handle.run_id, handle.task_id, handle.box_id, handle.integration) != (
            binding.run_id, binding.task_id, binding.box_id, False):
        raise AdmissionError("admission preparation returned another workspace")
    return bundle, handle
