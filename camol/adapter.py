"""Process adapter for one bounded agent turn."""

import asyncio
import hashlib
import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from .artifacts import ArtifactRef, ArtifactStore
from .probes import Redactor
from .sandbox import SandboxBackend, SandboxPolicy


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, observed_evidence: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.observed_evidence = list(observed_evidence or ())


def _substitute(argv: List[str], values: Dict[str, str]) -> List[str]:
    rendered = []
    for argument in argv:
        value = argument
        for name, replacement in values.items():
            value = value.replace("{" + name + "}", replacement)
        rendered.append(value)
    return rendered


class ProcessAgentAdapter:
    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        state_dir: Optional[Path] = None,
        sandbox_backend: Optional[SandboxBackend] = None,
        sandbox_policy: Optional[SandboxPolicy] = None,
        artifact_store: Optional[ArtifactStore] = None,
        redactor: Optional[Redactor] = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else self.workspace / ".camol"
        self.sandbox_backend = sandbox_backend
        if sandbox_backend is not None:
            sandbox_backend.invocation_root = self.state_dir / "packets" / run_id
        self.sandbox_policy = sandbox_policy
        self.artifact_store = artifact_store
        self.redactor = redactor or Redactor()
        self.before_launch = None
        self.execution_environment = None
        if (sandbox_backend is None) != (sandbox_policy is None):
            raise AdapterError("sandbox backend and policy must be supplied together")

    def _inside_workspace(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve()
        try:
            path.relative_to(self.workspace)
        except ValueError as error:
            raise AdapterError("agent path escapes the harness workspace") from error
        return path

    async def _authorize_launch(self, assignment, turn_number):
        """Runtime callback runs only for a new invocation, never cache replay."""
        if self.before_launch is not None:
            await self.before_launch(assignment, turn_number)

    async def execute_turn(
        self,
        agent: Dict[str, Any],
        assignment: Dict[str, str],
        packet: Dict[str, Any],
        turn_number: int,
        *,
        cost_budget_cents: Optional[int] = None,
    ) -> Dict[str, Any]:
        box = self._inside_workspace(agent["box"])
        packet_dir = self.state_dir / "packets" / self.run_id / assignment["task_id"]
        try:
            packet_dir.resolve().relative_to(self.state_dir)
        except ValueError as error:
            raise AdapterError("packet path escapes the state directory") from error
        box.mkdir(parents=True, exist_ok=True)
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_path = packet_dir / "turn-{:03d}.packet.json".format(turn_number)
        result_path = packet_dir / "turn-{:03d}.result.json".format(turn_number)
        worker_dir = packet_dir / "worker-output"
        worker_dir.mkdir(parents=True, exist_ok=True)
        worker_result_path = worker_dir / "turn-{:03d}.result.json".format(turn_number)
        desired_packet_bytes = (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode("utf-8")
        packet_bytes = desired_packet_bytes
        if packet_path.exists():
            try:
                existing_packet_bytes = packet_path.read_bytes()
                existing_packet = json.loads(existing_packet_bytes)
                if (
                    existing_packet.get("run", {}).get("id") == self.run_id
                    and existing_packet.get("lease") == packet.get("lease")
                ):
                    packet_bytes = existing_packet_bytes
                else:
                    packet_path.write_bytes(desired_packet_bytes)
                    if result_path.exists():
                        result_path.unlink()
            except (OSError, json.JSONDecodeError):
                packet_path.write_bytes(desired_packet_bytes)
                if result_path.exists():
                    result_path.unlink()
        else:
            packet_path.write_bytes(packet_bytes)
        packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
        if result_path.exists():
            try:
                recovered = json.loads(result_path.read_text(encoding="utf-8"))
                self.validate_result(recovered, packet_sha256)
                recovered = self.redactor.value(recovered)
                recovered_bytes = (json.dumps(recovered, indent=2, sort_keys=True) + "\n").encode("utf-8")
                if recovered_bytes != result_path.read_bytes():
                    result_path.write_bytes(recovered_bytes)
                observed = [
                    {
                        "kind": "command",
                        "epistemic_status": "EXECUTED",
                        "producer": "adapter",
                        "artifact_refs": [],
                        "data": {
                            "recovered_unconsumed_result": True,
                            "result_sha256": "sha256:" + hashlib.sha256(recovered_bytes).hexdigest(),
                        },
                    }
                ]
                result_reference = self._store_content(
                    recovered_bytes,
                    assignment,
                    channel="agent-result",
                    media_type="application/json",
                    redact=True,
                )
                if result_reference is not None:
                    observed.append(
                        {
                            "kind": "transcript",
                            "epistemic_status": "OBSERVED",
                            "producer": "adapter",
                            "artifact_refs": [result_reference.to_dict()],
                            "data": {"channel": "agent-result", "recovered": True},
                        }
                    )
                recovered["_camol_observed_evidence"] = observed
                return recovered
            except (AdapterError, json.JSONDecodeError):
                result_path.unlink()

        await self._authorize_launch(assignment, turn_number)
        argv = _substitute(
            agent["adapter"]["argv"],
            {
                "workspace": str(self.workspace),
                "box": str(box),
                "packet": str(packet_path),
                "result": str(worker_result_path),
                "run_id": self.run_id,
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
            },
        )
        invocation_id = str(uuid4())
        if self.sandbox_backend is not None:
            sandboxed = await self.sandbox_backend.run(
                argv,
                cwd=box,
                policy=self.sandbox_policy,
                timeout_seconds=agent["adapter"]["timeout_seconds"],
                environment=self.execution_environment,
                invocation_record=packet_dir / "turn-{:03d}.invocation.json".format(turn_number),
            )
            return_code = sandboxed.exit_code
            stdout_bytes = sandboxed.stdout
            stderr_bytes = sandboxed.stderr
            stdout_sha256 = sandboxed.stdout_sha256
            stderr_sha256 = sandboxed.stderr_sha256
            stdout_size = sandboxed.stdout_bytes
            stderr_size = sandboxed.stderr_bytes
            stdout_truncated = sandboxed.stdout_truncated
            stderr_truncated = sandboxed.stderr_truncated
            backend = sandboxed.backend
            sandbox_policy_digest = sandboxed.policy_digest
            started_at = sandboxed.started_at
            finished_at = sandboxed.finished_at
            process_id = sandboxed.process_id
            process_group_id = sandboxed.process_group_id
        else:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(box),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=agent["adapter"]["timeout_seconds"]
                )
            except asyncio.TimeoutError as error:
                process.kill()
                await process.wait()
                raise AdapterError("agent turn timed out") from error
            return_code = process.returncode
            stdout_sha256 = "sha256:" + hashlib.sha256(stdout_bytes).hexdigest()
            stderr_sha256 = "sha256:" + hashlib.sha256(stderr_bytes).hexdigest()
            stdout_size = len(stdout_bytes)
            stderr_size = len(stderr_bytes)
            stdout_truncated = False
            stderr_truncated = False
            backend = "legacy-unsandboxed"
            sandbox_policy_digest = None
            started_at = finished_at = "1970-01-01T00:00:00+00:00"
            process_id = process_group_id = None
        stdout_reference = self._store_content(
            stdout_bytes,
            assignment,
            channel="stdout",
            media_type="text/plain",
            redact=True,
            source_sha256=stdout_sha256,
            source_bytes=stdout_size,
            truncated=stdout_truncated,
            invocation_id=invocation_id,
        )
        stderr_reference = self._store_content(
            stderr_bytes,
            assignment,
            channel="stderr",
            media_type="text/plain",
            redact=True,
            source_sha256=stderr_sha256,
            source_bytes=stderr_size,
            truncated=stderr_truncated,
            invocation_id=invocation_id,
        )
        output_references = [
            reference.to_dict()
            for reference in (stdout_reference, stderr_reference)
            if reference is not None
        ]
        observed_evidence = [
            {
                "kind": "command",
                "epistemic_status": "EXECUTED",
                "producer": "adapter",
                "artifact_refs": output_references,
                "data": {
                    "invocation_id": invocation_id,
                    "turn_number": turn_number,
                    "argv": self.redactor.argv(argv),
                    "cwd": self.redactor.text(str(box)),
                    "exit_code": return_code,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "stdout_sha256": stdout_sha256,
                    "stderr_sha256": stderr_sha256,
                    "stdout_bytes": stdout_size,
                    "stderr_bytes": stderr_size,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "sandbox_backend": backend,
                    "sandbox_policy_digest": sandbox_policy_digest,
                    "process_id": process_id,
                    "process_group_id": process_group_id,
                    "environment_names": list(self.sandbox_policy.environment_names) if self.sandbox_policy else [],
                },
            },
            {
                "kind": "environment",
                "epistemic_status": "OBSERVED",
                "producer": "adapter",
                "artifact_refs": [],
                "data": {
                    "sandbox_backend": backend,
                    "sandbox_policy_digest": sandbox_policy_digest,
                    "allowlisted_names": list(self.sandbox_policy.environment_names) if self.sandbox_policy else [],
                    "values_persisted": False,
                },
            },
        ]
        if return_code != 0:
            raise AdapterError(
                "agent process exited {}; stderr sha256 {}".format(
                    return_code, stderr_sha256
                ),
                observed_evidence=observed_evidence,
            )
        if not worker_result_path.exists():
            raise AdapterError("agent did not write its structured result", observed_evidence=observed_evidence)
        try:
            descriptor = os.open(str(worker_result_path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as result_file:
                if not stat.S_ISREG(os.fstat(result_file.fileno()).st_mode):
                    raise ValueError("result is not a regular file")
                raw = result_file.read((16 << 20) + 1)
            if len(raw) > 16 << 20:
                raise ValueError("result exceeds its capture bound")
            result = json.loads(raw)
        except (OSError, ValueError) as error:
            raise AdapterError("agent result is not valid JSON", observed_evidence=observed_evidence) from error
        try:
            self.validate_result(result, packet_sha256)
        except AdapterError as error:
            raise AdapterError(str(error), observed_evidence=observed_evidence) from error
        result = self.redactor.value(result)
        clean_result_bytes = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
        # Cache only after collection/validation in the control-owned root.
        # Workers never receive write permission for packets, journals or logs.
        temporary = result_path.with_suffix(".tmp")
        temporary.write_bytes(clean_result_bytes)
        temporary.replace(result_path)
        result_reference = self._store_content(
            clean_result_bytes,
            assignment,
            channel="agent-result",
            media_type="application/json",
            redact=True,
            invocation_id=invocation_id,
        )
        packet_reference = self._store_content(
            packet_bytes,
            assignment,
            channel="context-packet",
            media_type="application/json",
            redact=True,
            invocation_id=invocation_id,
        )
        for channel, reference in (("context-packet", packet_reference), ("agent-result", result_reference)):
            if reference is not None:
                observed_evidence.append(
                    {
                        "kind": "transcript",
                        "epistemic_status": "OBSERVED",
                        "producer": "adapter",
                        "artifact_refs": [reference.to_dict()],
                        "data": {"channel": channel, "invocation_id": invocation_id},
                    }
                )
        result["_camol_observed_evidence"] = observed_evidence
        return result
    def _store_content(
        self,
        content: bytes,
        assignment: Dict[str, str],
        *,
        channel: str,
        media_type: str,
        redact: bool,
        source_sha256: Optional[str] = None,
        source_bytes: Optional[int] = None,
        truncated: bool = False,
        invocation_id: Optional[str] = None,
    ) -> Optional[ArtifactRef]:
        if self.artifact_store is None:
            return None
        producer = {
            "run_id": self.run_id,
            "task_id": assignment["task_id"],
            "agent_id": assignment["agent_id"],
            "lease_id": assignment["lease_id"],
            "channel": channel,
            "role": "adapter",
        }
        if invocation_id:
            producer["invocation_id"] = invocation_id
        return self.artifact_store.put_bytes(
            content,
            producer=producer,
            media_type=media_type,
            redact=redact,
            source_sha256=source_sha256,
            source_bytes=source_bytes,
            truncated=truncated,
        )

    @staticmethod
    def validate_result(result: Dict[str, Any], packet_sha256: str) -> None:
        if not isinstance(result, dict):
            raise AdapterError("agent result must be an object")
        allowed = {
            "status", "packet_sha256", "checkpoint", "completed_step_ids",
            "input_tokens", "output_tokens", "evidence", "messages", "summary", "blocker",
        }
        unknown = sorted(set(result) - allowed)
        if unknown:
            raise AdapterError("agent result has unknown fields: {}".format(", ".join(unknown)))
        if result.get("status") not in {"continue", "complete", "blocked"}:
            raise AdapterError("agent result status must be continue, complete, or blocked")
        if not isinstance(result.get("checkpoint"), str) or not result["checkpoint"].strip():
            raise AdapterError("agent result must contain a checkpoint")
        if not isinstance(result.get("completed_step_ids"), list):
            raise AdapterError("agent result completed_step_ids must be an array")
        if any(not isinstance(step_id, str) for step_id in result["completed_step_ids"]):
            raise AdapterError("agent result completed_step_ids must contain strings")
        for field in ("input_tokens", "output_tokens"):
            if not isinstance(result.get(field), int) or result[field] < 0:
                raise AdapterError("agent result {} must be a non-negative integer".format(field))
        if not isinstance(result.get("evidence"), list):
            raise AdapterError("agent result evidence must be an array")
        for evidence in result["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) - {"evidence_id", "kind", "data"}:
                raise AdapterError("worker evidence must contain only evidence_id, kind, and data")
        result.setdefault("messages", [])
        if not isinstance(result["messages"], list):
            raise AdapterError("agent result messages must be an array")
        for message in result["messages"]:
            if not isinstance(message, dict):
                raise AdapterError("each agent message must be an object")
        if result.get("packet_sha256") != packet_sha256:
            raise AdapterError("agent result does not belong to the current context packet")
        if result["status"] == "complete" and (
            not isinstance(result.get("summary"), str) or not result["summary"].strip()
        ):
            raise AdapterError("a complete result must contain a summary")
        if result["status"] == "blocked" and not isinstance(result.get("blocker"), dict):
            raise AdapterError("a blocked result must contain a blocker object")


_ADAPTER_FACTORIES: Dict[str, Callable[..., ProcessAgentAdapter]] = {"process": ProcessAgentAdapter}
_BUNDLED_ADAPTER_MODULES = ("camol.claude_adapter", "camol.codex_adapter")


def register_agent_adapter(kind: str, factory: Callable[..., ProcessAgentAdapter]) -> None:
    """Register an adapter without adding provider logic to the runner."""
    if not kind or not callable(factory):
        raise AdapterError("adapter registration requires a kind and callable factory")
    existing = _ADAPTER_FACTORIES.get(kind)
    if existing is not None and existing is not factory:
        raise AdapterError("execution adapter {!r} is already registered".format(kind))
    _ADAPTER_FACTORIES[kind] = factory


def create_agent_adapter(kind: str, *args: Any, **kwargs: Any) -> ProcessAgentAdapter:
    """Provider-neutral adapter registry boundary used by the runner."""
    if kind not in _ADAPTER_FACTORIES:
        for module in _BUNDLED_ADAPTER_MODULES:
            importlib.import_module(module)
    factory = _ADAPTER_FACTORIES.get(kind)
    if factory is None:
        raise AdapterError("no execution adapter registered for {!r}".format(kind))
    return factory(*args, **kwargs)
