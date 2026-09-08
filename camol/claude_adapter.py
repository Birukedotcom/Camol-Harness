"""Claude CLI implementation of Camol's generic bounded-turn contract."""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from .adapter import AdapterError, ProcessAgentAdapter, register_agent_adapter
from .providers import ModelProfile, ProviderError, _resolved_model, _usage, model_profile_for_adapter
from .schema import canonical_digest
from .sandbox import SandboxError
from .usage import UsageRecord
from .invocations import InvocationJournal


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
            profile = model_profile_for_adapter(self.workspace, agent["adapter"])
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
            "message_acknowledgments": [],
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
        box = self._inside_workspace(agent["box"])
        box.mkdir(parents=True, exist_ok=True)
        packet_dir = self.state_dir / "packets" / self.run_id / assignment["task_id"]
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_path = packet_dir / "turn-{:03d}.packet.json".format(turn_number)
        result_path = packet_dir / "turn-{:03d}.provider-result.json".format(turn_number)
        desired_packet = (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode("utf-8")
        packet_bytes = desired_packet
        if packet_path.is_file():
            try:
                existing_bytes = packet_path.read_bytes()
                existing = json.loads(existing_bytes)
                if existing.get("lease") == packet.get("lease") and existing.get("run", {}).get("id") == self.run_id:
                    packet_bytes = existing_bytes
                else:
                    packet_path.write_bytes(desired_packet)
                    result_path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                packet_path.write_bytes(desired_packet)
                result_path.unlink(missing_ok=True)
        else:
            packet_path.write_bytes(packet_bytes)
        packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
        if result_path.is_file():
            try:
                recovered = json.loads(result_path.read_text(encoding="utf-8"))
                observed = recovered.pop("_camol_observed_evidence")
                usage_invocation = recovered.pop("_camol_usage_invocation_id", None)
                self.validate_result(recovered, packet_sha256)
                if not isinstance(observed, list):
                    raise AdapterError("recovered provider evidence is malformed")
                recovered["_camol_observed_evidence"] = observed
                recovered["_camol_usage_invocation_id"] = usage_invocation
                return recovered
            except (KeyError, OSError, json.JSONDecodeError, AdapterError):
                result_path.unlink()
        if ceiling <= 0:
            raise AdapterError("provider cost budget is exhausted")
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
        started_at = datetime.now(timezone.utc).isoformat()
        def usage_evidence(final=None, *, outcome="unknown", finished_at=None, start=None):
            input_tokens = output_tokens = cost = cache_read = cache_creation = None
            resolved = None
            if isinstance(final, dict):
                resolved = _resolved_model(final)
                if not isinstance(resolved, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*", resolved) is None:
                    resolved = None
                try:
                    input_tokens, output_tokens, cost = _usage(final)
                    cache_read = final["usage"].get("cache_read_input_tokens", 0)
                    cache_creation = final["usage"].get("cache_creation_input_tokens", 0)
                except ProviderError:
                    # Malformed or partial billing is unknown; the frozen
                    # reservation remains charged until a receipt reconciles it.
                    pass
            record = UsageRecord(
                invocation_id=invocation_id, run_id=self.run_id,
                task_id=assignment["task_id"], agent_id=assignment["agent_id"],
                lease_id=assignment["lease_id"], turn_number=turn_number,
                provider=profile.provider, model=resolved, phase="worker",
                provenance="provider_observed" if input_tokens is not None else "unknown",
                outcome=outcome, input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_tokens=cache_read, cache_creation_tokens=cache_creation,
                cost_usd_micros=cost, reserved_tokens=profile.max_turn_tokens,
                reserved_cost_usd_micros=ceiling * 10_000,
                started_at=start or started_at,
                finished_at=finished_at or datetime.now(timezone.utc).isoformat(),
            )
            return {"kind": "model_usage", "epistemic_status": "OBSERVED", "producer": "adapter",
                    "artifact_refs": [], "data": record.to_dict()}
        journal = InvocationJournal(packet_dir / ("turn-{:03d}.{}.charge-pending.json".format(turn_number, assignment["lease_id"])),
            packet_sha256=packet_sha256, profile_digest=profile.digest(), assignment=assignment,
            run_id=self.run_id, turn_number=turn_number, workspace=self.workspace)
        await self._authorize_launch(assignment, turn_number)
        from .provider_budget import reserve_hosted
        def reservation_evidence(allocated):
            nonlocal ceiling
            ceiling = allocated
            return [usage_evidence()]
        ceiling = reserve_hosted(journal, state_dir=self.state_dir, profile=profile,
            plan_digest=packet.get("run", {}).get("plan_digest"), requested_cents=ceiling,
            evidence_factory=reservation_evidence, baselines=getattr(self, "budget_baselines", ()))
        self.hosted_invocation_id = invocation_id
        argv[argv.index("--max-budget-usd") + 1] = "{:.2f}".format(ceiling / 100)
        try:
            sandboxed = await self.sandbox_backend.run(
                argv,
                cwd=box,
                policy=self.sandbox_policy,
                timeout_seconds=agent["adapter"]["timeout_seconds"],
                environment=self.execution_environment,
                stdin_bytes=prompt,
                invocation_record=packet_dir / "turn-{:03d}.invocation.json".format(turn_number),
            )
        except asyncio.CancelledError as error:
            error.observed_evidence = [usage_evidence(outcome="cancelled")]
            journal.record_outcome(error.observed_evidence)
            raise
        except (SandboxError, OSError) as error:
            observed = [usage_evidence()]
            journal.record_outcome(observed)
            raise AdapterError(str(error), observed_evidence=observed) from error
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
                "turn_number": turn_number,
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
                "process_id": sandboxed.process_id,
                "process_group_id": sandboxed.process_group_id,
                "stdin_sha256": "sha256:" + hashlib.sha256(prompt).hexdigest(),
            },
        }
        final = None
        events = []
        try:
            events, final = _events(sandboxed.stdout)
            if sandboxed.exit_code != 0:
                raise AdapterError("Claude CLI exited {}".format(sandboxed.exit_code))
            if sandboxed.stdout_truncated:
                raise AdapterError("Claude CLI stream exceeded the bounded 16 MiB capture")
            if final.get("is_error") is True or final.get("subtype") not in (None, "success"):
                raise AdapterError("Claude CLI returned an unsuccessful result")
            resolved = _resolved_model(final)
            model_values = final.get("modelUsage") or final.get("model_usage") or {}
            all_models = sorted(model_values.keys()) if isinstance(model_values, dict) else []
            if not resolved:
                raise AdapterError("Claude CLI did not report a resolved model")
            if resolved not in profile.allowed_resolved_models:
                raise AdapterError("Claude CLI resolved an unapproved model {!r}".format(resolved))
            if any(model not in profile.allowed_resolved_models for model in all_models):
                raise AdapterError("Claude CLI used an unapproved secondary model")
            input_tokens, output_tokens, cost_micros = _usage(final)
            if input_tokens + output_tokens > profile.max_turn_tokens:
                raise AdapterError("provider usage exceeded the profile token ceiling")
            if cost_micros > ceiling * 10_000:
                raise AdapterError("provider usage exceeded the available cost ceiling")
            text = final.get("result")
            if not isinstance(text, str):
                raise AdapterError("Claude CLI result event has no final text")
            result = _json_object(text)
            result["input_tokens"] = input_tokens
            result["output_tokens"] = output_tokens
            self.validate_result(result, packet_sha256)
        except (AdapterError, ProviderError, ValueError) as error:
            observed = [command_evidence, usage_evidence(final, outcome="error", start=sandboxed.started_at, finished_at=sandboxed.finished_at)]
            for activity in _tool_activity(events):
                observed.append({"kind": "tool_call", "epistemic_status": "EXECUTED", "producer": "adapter",
                                 "artifact_refs": [], "data": self.redactor.value(dict(activity, invocation_id=invocation_id))})
            journal.record_outcome(self.redactor.value(observed))
            raise AdapterError(str(error), observed_evidence=observed) from error
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
                    "resolved_model": resolved,
                    "all_reported_models": all_models or [resolved],
                    "effort": profile.effort,
                    "permission_mode": profile.permission_mode,
                    "allowed_tools": list(profile.allowed_tools),
                    "cost_ceiling_usd_cents": ceiling,
                },
            },
            usage_evidence(final, outcome="success", start=sandboxed.started_at, finished_at=sandboxed.finished_at),
            {
                "kind": "transcript", "epistemic_status": "OBSERVED", "producer": "adapter",
                "artifact_refs": references,
                "data": {"channel": "provider-stream", "invocation_id": invocation_id},
            },
        ]
        for activity in _tool_activity(events):
            observed.append({
                "kind": "tool_call", "epistemic_status": "EXECUTED", "producer": "adapter",
                "artifact_refs": [], "data": self.redactor.value(dict(activity, invocation_id=invocation_id)),
            })
        result["_camol_observed_evidence"] = observed
        result["_camol_usage_invocation_id"] = invocation_id
        journal.record_outcome(self.redactor.value(observed))
        persisted = self.redactor.value(result)
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(persisted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(result_path)
        return result


register_agent_adapter("claude_cli", ClaudeCLIAdapter)
