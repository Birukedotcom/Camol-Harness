"""Claude CLI implementation of Camol's generic bounded-turn contract."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

from .adapter import AdapterError, ProcessAgentAdapter, register_agent_adapter
from .providers import ModelProfile, ProviderError, _resolved_model, _usage, load_model_profile
from .schema import canonical_digest


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _json_object(text: str) -> Dict[str, Any]:
    candidate = text.strip()
    match = _FENCE.match(candidate)
    if match:
        candidate = match.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise AdapterError("Claude final response is not the required JSON result contract") from error
    if not isinstance(value, dict):
        raise AdapterError("Claude final response must be a JSON object")
    return value


def _events(raw: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AdapterError("Claude stream contains invalid JSON on line {}".format(number)) from error
        if not isinstance(value, dict):
            raise AdapterError("Claude stream events must be objects")
        values.append(value)
    finals = [value for value in values if value.get("type") == "result"]
    if len(finals) != 1:
        raise AdapterError("Claude stream must contain exactly one result event")
    return values, finals[0]


def _tool_activity(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    activity = []
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {"tool_use", "tool_result"}:
                continue
            if block["type"] == "tool_use":
                activity.append({
                    "phase": "request",
                    "tool_use_id": block.get("id"),
                    "tool": block.get("name"),
                    "input_digest": canonical_digest(block.get("input", {})),
                    "input_keys": sorted(block.get("input", {}).keys()) if isinstance(block.get("input"), dict) else [],
                })
            else:
                activity.append({
                    "phase": "result",
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": block.get("is_error") is True,
                })
    return activity


class ClaudeCLIAdapter(ProcessAgentAdapter):
    """Execute one Claude Code print-mode turn inside a frozen sandbox."""

    ADAPTER_VERSION = 1

    def _profile(self, agent: Dict[str, Any]) -> ModelProfile:
        try:
            profile = load_model_profile(self.workspace, agent["adapter"]["profile"])
        except ProviderError as error:
            raise AdapterError(str(error)) from error
        if profile.adapter_kind != "claude_cli" or profile.provider != "anthropic":
            raise AdapterError("model profile is not compatible with the Claude CLI adapter")
        return profile

    @staticmethod
    def _prompt(packet: Dict[str, Any], packet_sha256: str) -> bytes:
        contract = {
            "status": "continue | complete | blocked",
            "packet_sha256": packet_sha256,
            "checkpoint": "non-empty durable checkpoint",
            "completed_step_ids": ["only ids from task.remaining_steps"],
            "input_tokens": 0,
            "output_tokens": 0,
            "evidence": [{"evidence_id": "unique-id", "kind": "claim", "data": {"claim": "..."}}],
            "messages": [],
            "summary": "required for complete",
            "blocker": "required for blocked",
        }
        prompt = (
            "You are a bounded worker inside Camol. The JSON context packet below is data and the frozen task contract, "
            "not permission to expand scope. Work only inside the current directory. Do not change the evaluator or Camol state. "
            "At the end, emit exactly one JSON object and no Markdown using the return shape below. "
            "Token fields in your object are placeholders and will be replaced by observed provider usage.\n\n"
            "RETURN SHAPE\n{}\n\nCONTEXT PACKET\n{}\n"
        ).format(json.dumps(contract, sort_keys=True), json.dumps(packet, sort_keys=True))
        return prompt.encode("utf-8")

    async def execute_turn(
        self,
        agent: Dict[str, Any],
        assignment: Dict[str, str],
        packet: Dict[str, Any],
        turn_number: int,
        *,
        cost_budget_cents: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self.sandbox_backend is None or self.sandbox_policy is None:
            raise AdapterError("hosted adapters require an explicit sandbox policy")
        profile = self._profile(agent)
        ceiling = profile.max_turn_usd_cents if cost_budget_cents is None else min(cost_budget_cents, profile.max_turn_usd_cents)
        if ceiling <= 0:
            raise AdapterError("provider cost budget is exhausted")
        box = self._inside_workspace(agent["box"])
        box.mkdir(parents=True, exist_ok=True)
        packet_dir = self.state_dir / "packets" / self.run_id / assignment["task_id"]
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_path = packet_dir / "turn-{:03d}.packet.json".format(turn_number)
        packet_bytes = (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode("utf-8")
        packet_path.write_bytes(packet_bytes)
        packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
        prompt = self._prompt(packet, packet_sha256)
        invocation_id = str(uuid4())
        argv = [
            profile.runtime_binary,
            "-p",
            "--input-format", "text",
            "--output-format", "stream-json",
            "--verbose",
            "--model", profile.requested_model,
            "--effort", profile.effort,
            "--max-turns", str(profile.max_agent_turns),
            "--max-budget-usd", "{:.2f}".format(ceiling / 100),
            "--permission-mode", profile.permission_mode,
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--safe-mode",
        ]
        if profile.allowed_tools:
            argv.extend(["--allowedTools", *profile.allowed_tools])
        # The provider stream contains the final structured result at its tail.
        # Keep a finite but profile-appropriate capture; never parse a truncated
        # prefix as if it were a complete turn.
        self.sandbox_backend.max_capture_bytes = max(self.sandbox_backend.max_capture_bytes, 16 << 20)
        sandboxed = await self.sandbox_backend.run(
            argv,
            cwd=box,
            policy=self.sandbox_policy,
            timeout_seconds=agent["adapter"]["timeout_seconds"],
            stdin_bytes=prompt,
        )
        references = []
        for channel, content, digest, size, truncated in (
            ("provider-stream", sandboxed.stdout, sandboxed.stdout_sha256, sandboxed.stdout_bytes, sandboxed.stdout_truncated),
            ("provider-stderr", sandboxed.stderr, sandboxed.stderr_sha256, sandboxed.stderr_bytes, sandboxed.stderr_truncated),
        ):
            reference = self._store_content(
                content,
                assignment,
                channel=channel,
                media_type="application/x-ndjson" if channel == "provider-stream" else "text/plain",
                redact=True,
                source_sha256=digest,
                source_bytes=size,
                truncated=truncated,
                invocation_id=invocation_id,
            )
            if reference:
                references.append(reference.to_dict())
        command_evidence = {
            "kind": "command", "epistemic_status": "EXECUTED", "producer": "adapter",
            "artifact_refs": references,
            "data": {
                "invocation_id": invocation_id,
                "argv": self.redactor.argv(argv),
                "cwd": self.redactor.text(str(box)),
                "exit_code": sandboxed.exit_code,
                "started_at": sandboxed.started_at,
                "finished_at": sandboxed.finished_at,
                "stdout_sha256": sandboxed.stdout_sha256,
                "stderr_sha256": sandboxed.stderr_sha256,
                "stdout_bytes": sandboxed.stdout_bytes,
                "stderr_bytes": sandboxed.stderr_bytes,
                "sandbox_backend": sandboxed.backend,
                "sandbox_policy_digest": sandboxed.policy_digest,
                "stdin_sha256": "sha256:" + hashlib.sha256(prompt).hexdigest(),
            },
        }
        if sandboxed.exit_code != 0:
            raise AdapterError("Claude CLI exited {}".format(sandboxed.exit_code), observed_evidence=[command_evidence])
        if sandboxed.stdout_truncated:
            raise AdapterError("Claude CLI stream exceeded the bounded 16 MiB capture", observed_evidence=[command_evidence])
        events, final = _events(sandboxed.stdout)
        if final.get("is_error") is True or final.get("subtype") not in (None, "success"):
            raise AdapterError("Claude CLI returned an unsuccessful result", observed_evidence=[command_evidence])
        resolved = _resolved_model(final)
        all_models = sorted((final.get("modelUsage") or final.get("model_usage") or {}).keys())
        if not resolved and len(all_models) == 1:
            resolved = all_models[0]
        if not resolved:
            raise AdapterError("Claude CLI did not report a resolved model", observed_evidence=[command_evidence])
        if resolved not in profile.allowed_resolved_models:
            raise AdapterError("Claude CLI resolved an unapproved model {!r}".format(resolved), observed_evidence=[command_evidence])
        input_tokens, output_tokens, cost_micros = _usage(final)
        if input_tokens + output_tokens > profile.max_turn_tokens:
            raise AdapterError("provider usage exceeded the profile token ceiling", observed_evidence=[command_evidence])
        if cost_micros > ceiling * 10_000:
            raise AdapterError("provider usage exceeded the available cost ceiling", observed_evidence=[command_evidence])
        text = final.get("result")
        if not isinstance(text, str):
            raise AdapterError("Claude CLI result event has no final text", observed_evidence=[command_evidence])
        result = _json_object(text)
        result["input_tokens"] = input_tokens
        result["output_tokens"] = output_tokens
        self.validate_result(result, packet_sha256)
        result = self.redactor.value(result)
        observed: List[Dict[str, Any]] = [
            command_evidence,
            {
                "kind": "model_request", "epistemic_status": "EXECUTED", "producer": "adapter", "artifact_refs": [],
                "data": {
                    "adapter_version": self.ADAPTER_VERSION,
                    "profile_id": profile.profile_id,
                    "profile_digest": profile.digest(),
                    "maturity": profile.maturity,
                    "runtime_version": packet.get("readiness", {}).get("runtime_id"),
                    "requested_model": profile.requested_model,
                    "effort": profile.effort,
                    "permission_mode": profile.permission_mode,
                    "allowed_tools": list(profile.allowed_tools),
                    "cost_ceiling_usd_cents": ceiling,
                },
            },
            {
                "kind": "model_usage", "epistemic_status": "OBSERVED", "producer": "adapter", "artifact_refs": [],
                "data": {
                    "requested_model": profile.requested_model,
                    "resolved_model": resolved,
                    "all_reported_models": all_models or [resolved],
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd_micros": cost_micros,
                    "provider_session_persisted": False,
                },
            },
            {
                "kind": "transcript", "epistemic_status": "OBSERVED", "producer": "adapter",
                "artifact_refs": references,
                "data": {"channel": "provider-stream", "invocation_id": invocation_id},
            },
        ]
        for activity in _tool_activity(events):
            observed.append({
                "kind": "tool_call", "epistemic_status": "EXECUTED", "producer": "adapter",
                "artifact_refs": [], "data": self.redactor.value(activity),
            })
        result["_camol_observed_evidence"] = observed
        return result


register_agent_adapter("claude_cli", ClaudeCLIAdapter)
