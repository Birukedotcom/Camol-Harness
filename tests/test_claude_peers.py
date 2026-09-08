"""Opt-in Claude policy and actual CLI/relay fixtures, never hosted inference."""

import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from camol.claude_peer_policy import execution_policy, settings
from camol.native_peers import NativePeerInvocation, peer_policy
from camol.providers import ModelProfile, ProviderError, create_claude_capability
from camol.probes import local_target_id
from camol.sandbox import MacOSSandboxBackend
from camol.state import project
from tests import test_evaluation, test_codex_adapter, test_native_peers, test_providers
from tests.test_gate_runtime import v5_plan


def profile():
    return test_providers.profile_payload(schema_version=4, peer_policy=peer_policy(),
        execution_policy=execution_policy(), local_provider=None, local_endpoint=None,
        credential_read_paths=[], allowed_tools=["Read", "Write"], capability_ttl_seconds=300)


class ClaudePeerPolicyTests(unittest.TestCase):
    def test_exact_opt_in_preserves_old_identity_and_denies_unsupported_contracts(self):
        old = test_providers.profile_payload()
        self.assertEqual(ModelProfile.from_dict(old).to_dict(), old)
        self.assertEqual(ModelProfile.from_dict(profile()).to_dict(), profile())
        supplied = profile()
        parsed = ModelProfile.from_dict(supplied)
        supplied["execution_policy"]["peer_capability_exposure"] = "hidden"
        self.assertEqual(parsed.to_dict(), profile())
        parsed.execution_policy["peer_capability_exposure"] = "hidden"
        with self.assertRaises(ProviderError):
            parsed.to_dict()
        for mutation in (dict(schema_version=3), dict(peer_policy=None), dict(execution_policy=None),
                dict(network_destinations=[]), dict(allowed_tools=["default"]), dict(allowed_tools=["Bash(*)"]),
                dict(allowed_tools=["mcp__foreign__anything"]), dict(allowed_tools=["Read", "Read"]),
                dict(permission_mode="plan"), dict(provider="openai"), dict(local_endpoint="http://127.0.0.1:42"),
                dict(execution_policy=dict(execution_policy(), peer_capability_exposure="hidden"))):
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, ProviderError)):
                ModelProfile.from_dict(dict(profile(), **mutation))


class ClaudePeerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_native_peers.NativePeerLifetimeTests.asyncSetUp
    asyncTearDown = test_native_peers.NativePeerLifetimeTests.asyncTearDown

    async def test_explicit_config_has_no_secret_or_global_edit_and_keeps_oauth_mode(self):
        self.profile = ModelProfile.from_dict(profile())
        async with NativePeerInvocation(self.adapter, self.profile, self.fixture.fixture.assignment, 1) as peer:
            token = peer.environment["CAMOL_PEER_TOKEN"]
            self.assertNotIn(token, json.dumps(peer.argv))
            self.assertIn("--restricted", peer.argv)
            self.assertIn("--strict-mcp-config", peer.argv)
            self.assertNotIn("--safe-mode", peer.argv)
            self.assertNotIn("--bare", peer.argv)
            self.assertEqual(peer.argv[peer.argv.index("--setting-sources") + 1], "")
            self.assertEqual(json.loads(peer.argv[peer.argv.index("--settings") + 1]), settings())
            config = json.loads(peer.argv[peer.argv.index("--mcp-config") + 1])
            self.assertEqual(set(config["mcpServers"]), {"camol_peers"})
            server = config["mcpServers"]["camol_peers"]
            self.assertEqual(server["env"]["CAMOL_PEER_TOKEN"], "${CAMOL_PEER_TOKEN}")
            allowed = peer.argv[peer.argv.index("--allowedTools") + 1:]
            self.assertEqual(set(allowed), {"Read", "Write", "mcp__camol_peers__list_boxes",
                "mcp__camol_peers__observe_box", "mcp__camol_peers__inbox", "mcp__camol_peers__send_message"})
        self.assertFalse(peer.parent.exists())


CLIENT = r'''
import os, subprocess
config=json.loads(sys.argv[sys.argv.index('--mcp-config')+1])['mcpServers']['camol_peers']
assert '--restricted' in sys.argv and '--strict-mcp-config' in sys.argv
assert '--safe-mode' not in sys.argv and '--bare' not in sys.argv
assert sys.argv[sys.argv.index('--setting-sources')+1] == ''
assert json.loads(sys.argv[sys.argv.index('--settings')+1]) == {'disableAllHooks':True,'autoMemoryEnabled':False,'claudeMdExcludes':['**']}
assert sys.argv[sys.argv.index('--tools')+1] == 'Read,Write'
assert set(sys.argv[sys.argv.index('--allowedTools')+1:]) == {'Read','Write','mcp__camol_peers__list_boxes','mcp__camol_peers__observe_box','mcp__camol_peers__inbox','mcp__camol_peers__send_message'}
config['cwd']=str(Path.cwd())
config['env_vars']=list(config['env'])
assert all(value=='${'+name+'}' for name,value in config['env'].items())
''' + "child=subprocess.Popen" + test_native_peers.MCP_CLIENT.split("child=subprocess.Popen", 1)[1]

HEADER = '''#!/usr/bin/env python3
import hashlib,json,re,sys
from pathlib import Path
if '--version' in sys.argv:
 print('2.1.263 fixture'); raise SystemExit(0)
if '--help' in sys.argv:
 print('--restricted --strict-mcp-config --mcp-config --setting-sources --settings --tools --allowedTools --disable-slash-commands --permission-prompts'); raise SystemExit(0)
if sys.argv[1:3] == ['auth','status']:
 print('{"loggedIn":true}'); raise SystemExit(0)
'''
BODY = "prompt=sys.stdin.read()" + test_codex_adapter.FAKE_CODEX.split("prompt=sys.stdin.read()", 1)[1].split("for event in [", 1)[0] + "\nworker_result=result\n"
FINAL = '''
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(worker_result),
 'total_cost_usd':0.03,'usage':{'input_tokens':12,'output_tokens':8},'modelUsage':{'claude-fable-5':{'costUSD':0.03}}}))
'''


class ClaudePeerBuildTests(unittest.TestCase):
    setUp = test_evaluation.EvaluationLoopTests.setUp
    tearDown = test_evaluation.EvaluationLoopTests.tearDown

    def execute_build(self, *, sandboxed=False, no_handshake=False, cleanup_fail=False, missing_flag=False, startup_denied=False):
        binary = self.root / "bin"
        binary.mkdir()
        executable = binary / "fake-claude"
        header = HEADER.replace("--restricted ", "") if missing_flag else HEADER
        executable.write_text(header + BODY + ("" if no_handshake else CLIENT) + FINAL)
        executable.chmod(0o755)
        plan = v5_plan("claude-peer-build")
        plan["agents"][0]["adapter"] = dict(kind="claude_cli", profile="claude.json", profile_snapshot=profile(), timeout_seconds=30)
        if sandboxed:
            plan["agents"][0]["trust_tier"] = "developer_sandboxed"
        contexts, tokens = [], []
        enter = NativePeerInvocation.__aenter__
        async def capture(invocation):
            result = await enter(invocation)
            contexts.append(invocation)
            tokens.append(result.environment["CAMOL_PEER_TOKEN"])
            if cleanup_fail:
                (result.parent / "foreign").write_text("preserve")
            return result
        with patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}), patch.object(NativePeerInvocation, "__aenter__", capture):
            create_claude_capability(ModelProfile.from_dict(profile()), target_id=local_target_id(), state_dir=self.state,
                cwd=self.source, accept_spend=True, runner=test_providers.fixture_runner(test_providers.Completed()))
            final, events = test_evaluation.EvaluationLoopTests.execute(self, plan)
        if missing_flag:
            self.assertEqual(final["tasks"]["change"]["attempts"], 0)
            self.assertIn("POLICY_DENIED", json.dumps(events))
            self.assertEqual(contexts, [])
            return
        try:
            if startup_denied:
                self.assertNotEqual(final["status"], "awaiting_acceptance")
                self.assertEqual(len(contexts), 1)
                snapshots = []
                def visit(value):
                    if isinstance(value, dict):
                        if value.get("protocol") == "claude-stream-startup-v1":
                            snapshots.append(value)
                        for item in value.values():
                            visit(item)
                    elif isinstance(value, list):
                        for item in value:
                            visit(item)
                visit(events)
                self.assertTrue(snapshots)
                self.assertTrue(all(item["failure"] and not item["prompt_dispatch_started"] and not item["prompt_sent"] for item in snapshots))
                self.assertIn('"provenance": "unknown"', json.dumps(events))
            elif no_handshake or cleanup_fail:
                self.assertNotEqual(final["status"], "awaiting_acceptance")
                self.assertIn('"input_tokens": 12', json.dumps(events))
                self.assertIn('"cost_usd_micros": 30000', json.dumps(events))
                self.assertTrue(contexts)
            else:
                if final["status"] != "awaiting_acceptance":
                    from camol.artifacts import ArtifactStore, artifact_refs
                    store = ArtifactStore(self.state)
                    self.fail(json.dumps(dict(tasks=final["tasks"], stderr=[store.read(ref).decode("utf-8", "replace")[-1500:] for ref in artifact_refs(events)
                        if ref.producer.get("channel") == "provider-stderr"])))
                self.assertEqual(len(final["peer_tool_reads"]), 1)
                packet = json.loads((self.state / "packets" / plan["run"]["id"] / "change" / "turn-001.packet.json").read_text())
                with patch("camol.native_peers.NativePeerInvocation", side_effect=AssertionError("cache must not relaunch")):
                    cached = asyncio.run(contexts[0].adapter.execute_turn(final["runbook"]["agents"][0], contexts[0].assignment, packet, 1, cost_budget_cents=0))
                self.assertEqual(cached["input_tokens"], 12)
            self.assertEqual(project(events), final)
            self.assertFalse((self.source / "output.txt").exists())
            for context in contexts:
                self.assertTrue(context.endpoint.snapshot()["closed"])
            for token in tokens:
                self.assertNotIn(token, json.dumps(events))
                for path in self.state.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        self.assertNotIn(token.encode(), path.read_bytes(), str(path))
        finally:
            for context in contexts:
                if cleanup_fail:
                    (context.parent / "foreign").unlink()
                    asyncio.run(context.close())

    def test_real_cli_relay_build_and_cached_recovery(self):
        self.execute_build()

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS Seatbelt")
    def test_actual_macos_sandboxed_cli_relay_build(self):
        self.execute_build(sandboxed=True)

    def test_missing_handshake_refuses_completion_and_retains_provider_usage(self):
        self.execute_build(no_handshake=True)

    def test_cleanup_failure_refuses_completion_and_retains_usage(self):
        self.execute_build(cleanup_fail=True)

    def test_missing_restricted_flag_denies_admission_before_worker_spend(self):
        self.execute_build(missing_flag=True)
