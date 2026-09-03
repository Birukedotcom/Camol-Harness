"""Process adapter for one bounded agent turn."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .sandbox import SandboxBackend, SandboxPolicy


class AdapterError(RuntimeError):
    pass


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
    ):
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else self.workspace / ".camol"
        self.sandbox_backend = sandbox_backend
        self.sandbox_policy = sandbox_policy
        if (sandbox_backend is None) != (sandbox_policy is None):
            raise AdapterError("sandbox backend and policy must be supplied together")

    def _inside_workspace(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve()
        try:
            path.relative_to(self.workspace)
        except ValueError as error:
            raise AdapterError("agent path escapes the harness workspace") from error
        return path

    async def execute_turn(
        self,
        agent: Dict[str, Any],
        assignment: Dict[str, str],
        packet: Dict[str, Any],
        turn_number: int,
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
                recovered_bytes = result_path.read_bytes()
                recovered["evidence"] = [
                    {
                        "kind": "command",
                        "data": {
                            "recovered_unconsumed_result": True,
                            "result_sha256": hashlib.sha256(recovered_bytes).hexdigest(),
                        },
                    }
                ] + recovered["evidence"]
                return recovered
            except (AdapterError, json.JSONDecodeError):
                result_path.unlink()

        argv = _substitute(
            agent["adapter"]["argv"],
            {
                "workspace": str(self.workspace),
                "box": str(box),
                "packet": str(packet_path),
                "result": str(result_path),
                "run_id": self.run_id,
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
            },
        )
        if self.sandbox_backend is not None:
            sandboxed = await self.sandbox_backend.run(
                argv,
                cwd=box,
                policy=self.sandbox_policy,
                timeout_seconds=agent["adapter"]["timeout_seconds"],
            )
            return_code = sandboxed.exit_code
            stdout_bytes = sandboxed.stdout
            stderr_bytes = sandboxed.stderr
            backend = sandboxed.backend
            sandbox_policy_digest = sandboxed.policy_digest
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
            backend = "legacy-unsandboxed"
            sandbox_policy_digest = None
        command_evidence = {
            "kind": "command",
            "data": {
                "argv": argv,
                "cwd": str(box),
                "exit_code": return_code,
                "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                "stdout_bytes": len(stdout_bytes),
                "stderr_bytes": len(stderr_bytes),
                "sandbox_backend": backend,
                "sandbox_policy_digest": sandbox_policy_digest,
            },
        }
        if return_code != 0:
            raise AdapterError(
                "agent process exited {}; stderr sha256 {}".format(
                    return_code, hashlib.sha256(stderr_bytes).hexdigest()
                )
            )
        if not result_path.exists():
            raise AdapterError("agent did not write its structured result")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AdapterError("agent result is not valid JSON") from error
        self.validate_result(result, packet_sha256)
        result["evidence"] = [command_evidence] + result["evidence"]
        return result

    @staticmethod
    def validate_result(result: Dict[str, Any], packet_sha256: str) -> None:
        if not isinstance(result, dict):
            raise AdapterError("agent result must be an object")
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
