import asyncio
import copy
import json
import os
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

from camol.adapter import AdapterError
from camol.codex_adapter import CodexCLIAdapter
from camol.codex_policy import require_local_model
from camol.providers import ModelProfile, ProviderError
from camol.runner import HarnessRunner
from camol.usage import UsageRecord
from tests import test_claude_adapter as fixture
from tests import test_evaluation as eval_fixture
from tests.test_gate_runtime import v5_plan
from tests.test_providers import profile_payload as original_profile


def codex_profile(local=False, endpoint="http://127.0.0.1:11434"):
    payload = original_profile()
    payload.update(schema_version=2, profile_id="codex-local" if local else "codex-worker",
                   provider="local" if local else "openai", adapter_kind="codex_oss" if local else "codex_cli",
                   runtime_binary="fake-codex", requested_model="fixture-local" if local else "fixture-codex",
                   allowed_resolved_models=["fixture-local" if local else "fixture-codex"],
                   permission_mode="acceptEdits", allowed_tools=[], credential_read_paths=[],
                   local_provider="ollama" if local else None, local_endpoint=endpoint if local else None,
                   execution_policy=dict(cost_enforcement="not_applicable" if local else "observed_only",
                       model_identity="requested_only", inner_turn_limit="unsupported", tool_policy="sandbox", network_enforcement="ambient"))
    if local:
        payload["credential_refs"] = []
    return payload


FAKE_CODEX = r'''#!/usr/bin/env python3
import hashlib,json,re,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.146.0'); raise SystemExit(0)
if '--help' in sys.argv:
 print('--ignore-user-config --ignore-rules --ephemeral --output-schema --json --sandbox'); raise SystemExit(0)
if sys.argv[1:3] == ['login','status']:
 print('Logged in using ChatGPT'); raise SystemExit(0)
prompt=sys.stdin.read()
packet=json.loads(prompt.split('CONTEXT PACKET\n')[-1])
digest=re.search(r'"packet_sha256": "([0-9a-f]{64})"', prompt).group(1)
Path('argv.json').write_text(json.dumps(sys.argv))
Path('output.txt').write_text('good\n')
result={'status':'complete','packet_sha256':digest,'checkpoint':'built',
 'completed_step_ids':[step['id'] for step in packet.get('task',{}).get('remaining_steps', [{'id':'build'}])],
 'input_tokens':999,'output_tokens':999,'summary':'built','blocker':None,
 'evidence':[{'kind':'claim','data':{'claim':'built'}},{'kind':'artifact','data':{'path':'output.txt','sha256':hashlib.sha256(Path('output.txt').read_bytes()).hexdigest()}}]}
for event in [{'type':'thread.started','thread_id':'fixture-thread'},
 {'type':'item.completed','item':{'id':'tool-1','type':'file_change','changes':[{'path':'output.txt','kind':'add'}]}},
 {'type':'item.completed','item':{'id':'message-1','type':'agent_message','text':json.dumps(result)}},
 {'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':2,'output_tokens':8}}]: print(json.dumps(event))
'''


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ClaudeAdapterTests.asyncSetUp
    asyncTearDown = fixture.ClaudeAdapterTests.asyncTearDown

    def prepare(self, local=False, endpoint="http://127.0.0.1:11434"):
        profile = codex_profile(local, endpoint)
        self.profile.write_text(json.dumps(profile))
        self.executable = self.bin / "fake-codex"
        self.executable.write_text(FAKE_CODEX)
        self.executable.chmod(0o755)
        self.agent["adapter"]["kind"] = profile["adapter_kind"]
        self.policy = replace(self.policy, credential_refs=tuple(profile["credential_refs"]))
        return CodexCLIAdapter(self.workspace, "run-1", state_dir=self.state,
            sandbox_backend=fixture.DeveloperTrustedBackend(), sandbox_policy=self.policy,
            artifact_store=fixture.ArtifactStore(self.state), redactor=fixture.Redactor(env={}))

    async def test_actual_fake_codex_build_retains_unknown_cost_tools_and_recovers_without_spend(self):
        adapter = self.prepare()
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            result = await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
            recovered = await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=0)
        self.assertEqual(result, recovered)
        self.assertEqual(result["input_tokens"], 12)
        self.assertEqual((self.workspace / "output.txt").read_text(), "good\n")
        observed = result["_camol_observed_evidence"]
        usage = UsageRecord.from_dict(next(item["data"] for item in observed if item["kind"] == "model_usage"))
        self.assertIsNone(usage.cost_usd_micros)
        self.assertIsNone(usage.model)
        self.assertEqual(usage.cost_charge, 50000)
        self.assertTrue(any(item["kind"] == "tool_call" for item in observed))
        self.assertIn("--ignore-user-config", json.loads((self.workspace / "argv.json").read_text()))

    async def test_malformed_final_preserves_tokens_and_cancellation_preserves_reservation(self):
        adapter = self.prepare()
        self.executable.write_text(FAKE_CODEX.replace("'text':json.dumps(result)", "'text':'bad-json'"))
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            with self.assertRaises(AdapterError) as caught:
                await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
        usage = next(item["data"] for item in caught.exception.observed_evidence if item["kind"] == "model_usage")
        self.assertEqual(usage["input_tokens"], 12)
        first_evidence = caught.exception.observed_evidence
        with patch.object(adapter.sandbox_backend, "run", side_effect=AssertionError("must not duplicate uncertain invocation")):
            with self.assertRaisesRegex(AdapterError, "already launched") as recovered:
                await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=5)
        self.assertEqual(recovered.exception.observed_evidence, first_evidence)
        with patch.object(adapter.sandbox_backend, "run", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError) as caught:
                await adapter.execute_turn(self.agent, self.assignment, self.packet, 2, cost_budget_cents=5)
        self.assertEqual(caught.exception.observed_evidence[0]["data"]["outcome"], "cancelled")

    async def test_local_worker_selects_catalogued_model_without_bootstrap_or_credentials(self):
        adapter = self.prepare(local=True)
        with patch("camol.codex_adapter.require_local_model", return_value={"requested_model": "fixture-local"}), patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            result = await adapter.execute_turn(self.agent, self.assignment, self.packet, 1, cost_budget_cents=0)
        argv = json.loads((self.workspace / "argv.json").read_text())
        self.assertNotIn("--oss", argv)
        self.assertIn('model_provider="camol_local"', argv)
        usage = next(item["data"] for item in result["_camol_observed_evidence"] if item["kind"] == "model_usage")
        self.assertEqual(usage["reserved_cost_usd_micros"], 0)
        self.assertEqual(usage["cost_usd_micros"], 0)

    def test_strict_guarantees_remote_endpoints_and_credentials_are_denied(self):
        profile = codex_profile()
        self.assertEqual(ModelProfile.from_dict(profile).to_dict(), profile)
        for field, value in (("cost_enforcement", "hard"), ("model_identity", "resolved"), ("network_enforcement", "loopback_only")):
            invalid = copy.deepcopy(profile)
            invalid["execution_policy"][field] = value
            with self.assertRaises(ProviderError):
                ModelProfile.from_dict(invalid)
        for endpoint in ("https://remote.invalid", "http://localhost:11434", "http://127.0.0.1:11434/secret", "http://u:p@127.0.0.1:11434"):
            with self.assertRaises(ProviderError):
                ModelProfile.from_dict(codex_profile(True, endpoint))

    def test_loopback_catalog_is_read_only_and_redirects_are_denied(self):
        paths = []
        redirect = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                paths.append(self.path)
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://remote.invalid/models")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"models": [{"name": "fixture-local"}]}).encode())
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = ModelProfile.from_dict(codex_profile(True, "http://127.0.0.1:" + str(server.server_port)))
            self.assertEqual(require_local_model(profile)["requested_model"], "fixture-local")
            self.assertEqual(paths, ["/api/tags"])
            redirect.append(True)
            with self.assertRaisesRegex(ProviderError, "redirect"):
                require_local_model(profile)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CodexRuntimeTests(unittest.TestCase):
    setUp = eval_fixture.EvaluationLoopTests.setUp
    tearDown = eval_fixture.EvaluationLoopTests.tearDown

    def test_v5_codex_worker_gates_and_integration_without_paid_provider(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "fake-codex"
        executable.write_text(FAKE_CODEX)
        executable.chmod(0o755)
        plan = v5_plan("codex-v5-worker")
        plan["agents"][0]["adapter"] = {"kind": "codex_cli", "profile": "codex.json", "profile_snapshot": codex_profile(), "timeout_seconds": 30}
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            final, events = eval_fixture.EvaluationLoopTests.execute(self, plan)
        self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 1)
        self.assertTrue(final["integrations"])
        self.assertEqual(final["total_tokens"], 20)
        self.assertFalse((self.source / "output.txt").exists())

    def test_unknown_charge_stops_before_a_second_provider_call(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "fake-codex"
        executable.write_text(FAKE_CODEX.replace("'status':'complete'", "'status':'continue'"))
        executable.chmod(0o755)
        plan = v5_plan("codex-unknown-stop")
        plan["agents"][0]["adapter"] = {"kind": "codex_cli", "profile": "codex.json", "profile_snapshot": codex_profile(), "timeout_seconds": 30}
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            final, events = eval_fixture.EvaluationLoopTests.execute(self, plan)
        self.assertEqual(final["status"], "running")
        self.assertEqual(final["tasks"]["change"]["status"], "waiting")
        self.assertEqual(final["tasks"]["change"]["waiting"]["code"], "OPERATOR_ATTENTION")
        self.assertEqual(final["tasks"]["change"]["turn_count"], 1)
        self.assertEqual(sum(event["type"] == "AGENT_TURN_RECORDED" for event in events), 1)
        self.assertFalse(list(self.state.glob("packets/*/*/turn-002.*.charge-pending.json")))

    def test_worker_cannot_change_a_file_backed_profile_after_admission(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "fake-codex"
        executable.write_text(FAKE_CODEX.replace("'status':'complete'", "'status':'continue'").replace(
            "Path('argv.json').write_text(json.dumps(sys.argv))",
            "Path('argv.json').write_text(json.dumps(sys.argv)); policy=json.loads(Path('codex.json').read_text()); policy['effort']='low'; Path('codex.json').write_text(json.dumps(policy))"))
        executable.chmod(0o755)
        profile = codex_profile()
        profile["effort"] = "high"
        (self.source / "codex.json").write_text(json.dumps(profile))
        eval_fixture.git(self.source, "add", "codex.json")
        eval_fixture.git(self.source, "commit", "-q", "-m", "freeze provider policy")
        plan = v5_plan("codex-profile-tamper")
        plan["agents"][0]["adapter"] = {"kind": "codex_cli", "profile": "codex.json", "timeout_seconds": 30}
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            final, _ = eval_fixture.EvaluationLoopTests.execute(self, plan)
        self.assertEqual(final["tasks"]["change"]["status"], "waiting")
        self.assertEqual(final["tasks"]["change"]["waiting"]["code"], "POLICY_DENIED")
        self.assertEqual(final["tasks"]["change"]["turn_count"], 1)
        self.assertEqual(json.loads((self.source / "codex.json").read_text())["effort"], "high")
