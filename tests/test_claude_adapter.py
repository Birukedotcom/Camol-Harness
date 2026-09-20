import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.artifacts import ArtifactStore
from camol.claude_adapter import ClaudeCLIAdapter
from camol.adapter import AdapterError, create_agent_adapter
from camol.probes import Redactor
from camol.sandbox import DeveloperTrustedBackend, SandboxPolicy
from camol.runner import HarnessRunner
from camol.usage import UsageRecord
from camol.sandbox import SandboxError
from tests.test_providers import profile_payload


class ClaudeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.workspace.mkdir()
        self.state.mkdir()
        self.bin.mkdir()
        self.profile = self.workspace / "profile.yaml"
        self.profile.write_text(json.dumps(profile_payload()), encoding="utf-8")
        self.executable = self.bin / "fake-claude"
        self.executable.write_text(
            """#!/usr/bin/env python3
import hashlib, json, re, sys
from pathlib import Path
prompt = sys.stdin.read()
match = re.search(r'\"packet_sha256\": \"([0-9a-f]{64})\"', prompt)
Path('built.txt').write_text('built by fake provider\\n')
result = {
  'status': 'complete', 'packet_sha256': match.group(1), 'checkpoint': 'done',
  'completed_step_ids': ['build'], 'input_tokens': 999, 'output_tokens': 999,
  'evidence': [{'evidence_id': 'claim-1', 'kind': 'claim', 'data': {'claim': 'built'}}],
  'messages': [], 'summary': 'built'
}
events = [
  {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'tool-1', 'name': 'Write', 'input': {'path': 'built.txt'}}]}},
  {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1', 'is_error': False, 'content': 'ok'}]}},
  {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(result),
   'total_cost_usd': 0.03, 'usage': {'input_tokens': 12, 'output_tokens': 8},
   'modelUsage': {'claude-fable-5': {'costUSD': 0.03}}}
]
for event in events: print(json.dumps(event))
""",
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        self.policy = SandboxPolicy(
            policy_id="test", workspace=str(self.workspace),
            read_paths=(str(self.workspace), str(self.state), str(self.bin)),
            write_paths=(str(self.workspace), str(self.state)),
            environment_names=("PATH",), network_destinations=("*",),
            credential_refs=("existing-login",), trust_tier="developer_trusted",
        )
        self.agent = {
            "id": "builder", "box": ".", "adapter": {
                "kind": "claude_cli", "profile": "profile.yaml", "timeout_seconds": 30,
            },
        }
        self.assignment = {"task_id": "build", "agent_id": "builder", "lease_id": "lease-1"}
        self.packet = {"run": {"id": "run-1"}, "readiness": {"runtime_id": "fake@1"}}

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def adapter(self):
        return ClaudeCLIAdapter(
            self.workspace, "run-1", state_dir=self.state,
            sandbox_backend=DeveloperTrustedBackend(), sandbox_policy=self.policy,
            artifact_store=ArtifactStore(self.state), redactor=Redactor(env={}),
        )

    async def test_bounded_turn_captures_resolution_usage_tools_and_transcript(self):
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}):
            result = await self.adapter().execute_turn(
                self.agent, self.assignment, self.packet, 1, cost_budget_cents=5,
            )
        self.assertEqual(result["input_tokens"], 12)
        self.assertEqual(result["output_tokens"], 8)
        self.assertTrue((self.workspace / "built.txt").is_file())
        observed = result.pop("_camol_observed_evidence")
        kinds = [item["kind"] for item in observed]
        self.assertIn("model_request", kinds)
        self.assertIn("model_usage", kinds)
        self.assertEqual(kinds.count("tool_call"), 2)
        usage = next(item for item in observed if item["kind"] == "model_usage")
        self.assertEqual(usage["data"]["model"], "claude-fable-5")
        self.assertEqual(usage["data"]["cost_usd_micros"], 30000)
        command = next(item for item in observed if item["kind"] == "command")
        self.assertNotIn("CONTEXT PACKET", " ".join(command["data"]["argv"]))
        self.assertTrue(command["artifact_refs"])

    async def test_overspend_retains_incurred_usage_and_tool_history(self):
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaisesRegex(AdapterError, "cost ceiling") as caught:
                await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=1)
        items = caught.exception.observed_evidence
        record = UsageRecord.from_dict(next(item["data"] for item in items if item["kind"] == "model_usage"))
        self.assertEqual(record.cost_usd_micros, 30000)
        self.assertEqual(record.total_tokens, 20)
        self.assertEqual(record.outcome, "error")
        self.assertEqual(sum(item["kind"] == "tool_call" for item in items), 2)

    async def test_invalid_agent_json_retains_valid_provider_receipt(self):
        content = self.executable.read_text()
        self.executable.write_text(content.replace("json.dumps(result)", "'invalid-json'"))
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaisesRegex(AdapterError, "JSON result contract") as caught:
                await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
        receipt = next(item for item in caught.exception.observed_evidence if item["kind"] == "model_usage")
        self.assertEqual(receipt["data"]["cost_usd_micros"], 30000)

    async def test_transport_failure_reserves_budget_and_marks_usage_unknown(self):
        adapter = self.adapter()
        with patch.object(adapter.sandbox_backend, "run", side_effect=SandboxError("sandboxed process timed out")):
            with self.assertRaises(AdapterError) as caught:
                await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
        receipt = UsageRecord.from_dict(caught.exception.observed_evidence[0]["data"])
        self.assertEqual(receipt.provenance, "unknown")
        self.assertIsNone(receipt.total_tokens)
        self.assertIsNone(receipt.cost_usd_micros)
        self.assertEqual(receipt.cost_charge, 50000)

    async def test_unapproved_secondary_model_fails_with_billing_evidence(self):
        content = self.executable.read_text()
        content = content.replace("'modelUsage': {'claude-fable-5': {'costUSD': 0.03}}", "'model': 'claude-fable-5', 'modelUsage': {'claude-fable-5': {'costUSD': 0.02}, 'unapproved': {'costUSD': 0.01}}")
        self.executable.write_text(content)
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaisesRegex(AdapterError, "secondary model") as caught:
                await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
        self.assertEqual(next(item["data"]["cost_usd_micros"] for item in caught.exception.observed_evidence if item["kind"] == "model_usage"), 30000)

    async def test_exhausted_budget_prevents_cli_launch(self):
        with self.assertRaisesRegex(AdapterError, "budget is exhausted"):
            await self.adapter().execute_turn(
                self.agent, self.assignment, self.packet, 1, cost_budget_cents=0,
            )
        self.assertFalse((self.workspace / "built.txt").exists())

    async def test_written_provider_result_is_consumed_after_restart_without_second_call(self):
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}):
            first = await self.adapter().execute_turn(
                self.agent, self.assignment, self.packet, 1, cost_budget_cents=5,
            )
            self.executable.rename(self.executable.with_suffix(".disabled"))
            recovered = await self.adapter().execute_turn(
                self.agent, self.assignment, self.packet, 1, cost_budget_cents=5,
            )
        self.assertEqual(recovered["summary"], first["summary"])
        self.assertTrue(recovered["_camol_observed_evidence"])

    def test_registry_selects_adapter_without_kernel_vendor_logic(self):
        adapter = create_agent_adapter(
            "claude_cli", self.workspace, "run-1", state_dir=self.state,
            sandbox_backend=DeveloperTrustedBackend(), sandbox_policy=self.policy,
        )
        self.assertIsInstance(adapter, ClaudeCLIAdapter)

    def test_remaining_cost_is_the_strictest_turn_task_or_run_ceiling(self):
        state = {"evidence": {
            "one": {"kind": "model_usage", "epistemic_status": "OBSERVED", "producer": "adapter", "task_id": "build", "data": {"cost_usd_micros": 150_000}},
            "two": {"kind": "model_usage", "epistemic_status": "OBSERVED", "producer": "adapter", "task_id": "other", "data": {"cost_usd_micros": 120_000}},
        }}
        self.assertEqual(
            HarnessRunner._provider_cost_remaining(state, self.agent, "build", self.workspace),
            3,
        )
        state["evidence"]["three"] = {
            "kind": "model_usage", "epistemic_status": "OBSERVED", "producer": "adapter", "task_id": "other", "data": {"cost_usd_micros": 30_000},
        }
        self.assertEqual(
            HarnessRunner._provider_cost_remaining(state, self.agent, "build", self.workspace),
            0,
        )


if __name__ == "__main__":
    unittest.main()
