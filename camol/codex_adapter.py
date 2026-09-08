"""Codex exec and local Responses-provider workers with honest capability limits."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from .adapter import AdapterError, ProcessAgentAdapter, register_agent_adapter
from .claude_adapter import ClaudeCLIAdapter
from .codex_policy import require_local_model, validate_codex_policy
from .providers import model_profile_for_adapter
from .sandbox import SandboxError
from .schema import canonical_digest
from .usage import UsageRecord
from .invocations import InvocationJournal


RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["continue", "complete", "blocked"]},
        "packet_sha256": {"type": "string"}, "checkpoint": {"type": "string"},
        "completed_step_ids": {"type": "array", "items": {"type": "string"}},
        "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"},
        "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": ["claim"]}, "data": {"type": "object", "additionalProperties": False,
                "properties": {"claim": {"type": "string"}}, "required": ["claim"]}}, "required": ["kind", "data"]}},
        "summary": {"type": "string"},
        "blocker": {"type": ["object", "null"], "additionalProperties": False,
            "properties": {"kind": {"type": "string"}, "detail": {"type": "string"}}, "required": ["kind", "detail"]},
    },
}
RESULT_SCHEMA["required"] = list(RESULT_SCHEMA["properties"])
RESULT_SCHEMA["properties"]["evidence"]["items"] = {"anyOf": [
    RESULT_SCHEMA["properties"]["evidence"]["items"],
    {"type": "object", "additionalProperties": False, "properties": {
        "kind": {"type": "string", "enum": ["artifact"]},
        "data": {"type": "object", "additionalProperties": False,
                 "properties": {"path": {"type": "string"}, "sha256": {"type": "string"}},
                 "required": ["path", "sha256"]}}, "required": ["kind", "data"]},
]}


def parse_stream(raw):
    events = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("non-object event")
                events.append(item)
    except (ValueError, UnicodeError) as error:
        raise AdapterError("Codex stream is not complete JSONL") from error
    completed = [item for item in events if item.get("type") == "turn.completed"]
    if len(completed) != 1 or any(item.get("type") in {"turn.failed", "error"} for item in events):
        raise AdapterError("Codex stream does not contain exactly one successful terminal turn")
    usage = completed[0].get("usage", {})
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise AdapterError("Codex terminal event lacks valid token usage")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise AdapterError("Codex cached usage exceeds total input")
    finals = [item["item"].get("text") for item in events if item.get("type") == "item.completed" and isinstance(item.get("item"), dict) and item["item"].get("type") == "agent_message"]
    if not finals or not isinstance(finals[-1], str):
        error = AdapterError("Codex final structured result is missing")
        error.observed_usage = usage
        raise error
    return events, usage, finals[-1]


class CodexCLIAdapter(ProcessAgentAdapter):
    async def execute_turn(self, agent, assignment, packet, turn_number, *, cost_budget_cents=None):
        if self.sandbox_backend is None or self.sandbox_policy is None:
            raise AdapterError("Codex workers require an explicit outer sandbox policy")
        profile = model_profile_for_adapter(self.workspace, agent["adapter"])
        validate_codex_policy(profile)
        if tuple(self.sandbox_policy.network_destinations) != profile.network_destinations:
            raise AdapterError("Codex profile does not match the granted network policy")
        if tuple(self.sandbox_policy.credential_refs) != tuple(sorted(profile.credential_refs)):
            raise AdapterError("Codex profile does not match the granted credential policy")
        local = profile.adapter_kind == "codex_oss"
        ceiling = profile.max_turn_usd_cents if cost_budget_cents is None else min(profile.max_turn_usd_cents, cost_budget_cents)
        box = self._inside_workspace(agent["box"])
        box.mkdir(parents=True, exist_ok=True)
        packet_dir = self.state_dir / "packets" / self.run_id / assignment["task_id"]
        packet_dir.mkdir(parents=True, exist_ok=True)
        stem = "turn-{:03d}".format(turn_number)
        packet_path, result_path = packet_dir / (stem + ".packet.json"), packet_dir / (stem + ".codex-result.json")
        packet_bytes = (json.dumps(packet, sort_keys=True, indent=2) + "\n").encode()
        if packet_path.exists():
            try:
                existing_bytes = packet_path.read_bytes()
                existing = json.loads(existing_bytes)
                if existing.get("run", {}).get("id") == self.run_id and existing.get("lease") == packet.get("lease"):
                    packet_bytes = existing_bytes
                else:
                    result_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                result_path.unlink(missing_ok=True)
        packet_path.write_bytes(packet_bytes)
        packet_hash = hashlib.sha256(packet_bytes).hexdigest()
        if result_path.is_file():
            try:
                recovered = json.loads(result_path.read_text())
                evidence = recovered.pop("_camol_observed_evidence")
                invocation = recovered.pop("_camol_usage_invocation_id")
                self.validate_result(recovered, packet_hash)
                return dict(recovered, _camol_observed_evidence=evidence, _camol_usage_invocation_id=invocation)
            except (KeyError, OSError, ValueError, AdapterError):
                raise AdapterError("retained Codex result is corrupt; inspect before retrying a potentially paid request")
        if not local and ceiling <= 0:
            raise AdapterError("provider reservation budget is exhausted; reconcile unknown usage before another paid launch")
        schema_path = packet_dir / (stem + ".schema.json")
        schema_path.write_text(json.dumps(RESULT_SCHEMA, sort_keys=True))
        argv = [profile.runtime_binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--color", "never", "--model", profile.requested_model, "--sandbox", "workspace-write",
                "--output-schema", str(schema_path), "-C", str(box),
                "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=false",
                "-c", "model_reasoning_effort=" + json.dumps(profile.effort), "-c", "mcp_servers={}", "-c", 'web_search="disabled"']
        if local:
            await asyncio.to_thread(require_local_model, profile)
            # A custom Responses provider avoids --oss's model bootstrap path:
            # removal of a catalog entry cannot trigger an implicit download.
            argv += ["-c", 'model_provider="camol_local"', "-c", 'model_providers.camol_local.name="Camol local"',
                     "-c", "model_providers.camol_local.base_url=" + json.dumps(profile.local_endpoint.rstrip("/") + "/v1"),
                     "-c", 'model_providers.camol_local.wire_api="responses"',
                     "-c", "model_providers.camol_local.requires_openai_auth=false"]
        argv.append("-")
        invocation_id = str(uuid4())
        started = datetime.now(timezone.utc).isoformat()

        def usage_evidence(usage=None, outcome="unknown"):
            record = UsageRecord(
                invocation_id=invocation_id, run_id=self.run_id, task_id=assignment["task_id"], agent_id=assignment["agent_id"],
                lease_id=assignment["lease_id"], turn_number=turn_number, provider=profile.provider, model=None, phase="worker",
                provenance="provider_observed" if usage is not None else "unknown", outcome=outcome,
                input_tokens=usage.get("input_tokens") if usage else None, output_tokens=usage.get("output_tokens") if usage else None,
                cache_read_tokens=usage.get("cached_input_tokens") if usage else None, cache_creation_tokens=0 if usage else None,
                cost_usd_micros=0 if local and usage else None, reserved_tokens=profile.max_turn_tokens,
                reserved_cost_usd_micros=0 if local else ceiling * 10000,
                started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
            )
            return dict(kind="model_usage", epistemic_status="OBSERVED", producer="adapter", artifact_refs=[], data=record.to_dict())

        journal = InvocationJournal(packet_dir / (stem + "." + assignment["lease_id"] + ".charge-pending.json"),
            packet_sha256=packet_hash, profile_digest=profile.digest(), assignment=assignment,
            run_id=self.run_id, turn_number=turn_number, workspace=self.workspace)
        await self._authorize_launch(assignment, turn_number)
        journal.reserve([usage_evidence()])
        self.sandbox_backend.max_capture_bytes = max(self.sandbox_backend.max_capture_bytes, 16 << 20)
        try:
            process = await self.sandbox_backend.run(argv, cwd=box, policy=self.sandbox_policy,
                timeout_seconds=agent["adapter"]["timeout_seconds"],
                environment=self.execution_environment,
                stdin_bytes=ClaudeCLIAdapter._prompt(packet, packet_hash),
                invocation_record=packet_dir / (stem + ".invocation.json"))
        except asyncio.CancelledError as error:
            error.observed_evidence = [usage_evidence(outcome="cancelled")]
            journal.record_outcome(error.observed_evidence)
            raise
        except (SandboxError, OSError) as error:
            evidence = [usage_evidence()]
            journal.record_outcome(evidence)
            raise AdapterError(str(error), observed_evidence=evidence) from error
        observed = []
        for channel, raw, digest, size, truncated in (("provider-stream", process.stdout, process.stdout_sha256, process.stdout_bytes, process.stdout_truncated), ("provider-stderr", process.stderr, process.stderr_sha256, process.stderr_bytes, process.stderr_truncated)):
            reference = self._store_content(raw, assignment, channel=channel, media_type="application/x-ndjson" if channel == "provider-stream" else "text/plain", redact=True,
                source_sha256=digest, source_bytes=size, truncated=truncated, invocation_id=invocation_id)
            observed.append(dict(kind="transcript", epistemic_status="OBSERVED", producer="adapter", artifact_refs=[reference.to_dict()] if reference else [], data={"channel": channel}))
        usage = None
        try:
            events, usage, final = parse_stream(process.stdout)
            for event in events:
                item = event.get("item")
                if event.get("type", "").startswith("item.") and isinstance(item, dict) and item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                    observed.append(dict(kind="tool_call", epistemic_status="EXECUTED", producer="adapter", artifact_refs=[],
                                         data={"invocation_id": invocation_id, "event": event}))
            observed.append(dict(kind="command", epistemic_status="EXECUTED", producer="adapter", artifact_refs=[], data={
                "runtime": "codex", "requested_model": profile.requested_model, "resolved_model": None,
                "execution_policy": profile.execution_policy, "exit_code": process.exit_code, "cost_applicability": "not_applicable" if local else "unknown",
                "argv": argv}))
            if process.exit_code != 0 or process.stdout_truncated:
                raise AdapterError("Codex execution failed or provider stream was truncated")
            result = json.loads(final)
            if not isinstance(result, dict):
                raise AdapterError("Codex final result must be an object")
            result.update(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"])
            self.validate_result(result, packet_hash)
            if usage["input_tokens"] + usage["output_tokens"] > profile.max_turn_tokens:
                raise AdapterError("Codex observed token usage exceeds frozen per-turn budget")
        except (AdapterError, ValueError) as error:
            usage = getattr(error, "observed_usage", usage)
            observed.append(usage_evidence(usage, "error"))
            journal.record_outcome(self.redactor.value(observed))
            raise AdapterError(str(error), observed_evidence=observed) from error
        observed.append(usage_evidence(usage, "success"))
        journal.record_outcome(self.redactor.value(observed))
        result = self.redactor.value(dict(result, _camol_observed_evidence=observed, _camol_usage_invocation_id=invocation_id))
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, sort_keys=True) + "\n")
        temporary.replace(result_path)
        return result


register_agent_adapter("codex_cli", CodexCLIAdapter)
register_agent_adapter("codex_oss", CodexCLIAdapter)
