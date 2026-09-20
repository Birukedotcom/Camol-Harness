"""Native worker peer policy and real fake-CLI integration; never a model call."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from dataclasses import replace

import camol.native_peers as native_peer_module
from camol.native_peers import NativePeerInvocation, admission_paths, parent_path, peer_policy, ENV_NAMES
from camol.providers import ModelProfile, ProviderError
from camol.probes import Redactor
from camol.sandbox import SandboxPolicy
from camol.sandbox import MacOSSandboxBackend
from camol.state import project
from tests import test_peer_tools, test_codex_adapter, test_evaluation
from tests.test_gate_runtime import v5_plan


def profile():
    return dict(test_codex_adapter.codex_profile(), schema_version=3, peer_policy=peer_policy())


class NativePeerPolicyTests(unittest.TestCase):
    def test_old_profiles_keep_identity_and_new_policy_is_explicit_and_strict(self):
        old = test_codex_adapter.codex_profile()
        self.assertEqual(ModelProfile.from_dict(old).to_dict(), old)
        new = profile()
        parsed = ModelProfile.from_dict(new)
        self.assertEqual(parsed.to_dict(), new)
        self.assertNotEqual(parsed.digest(), ModelProfile.from_dict(old).digest())
        for mutation in (dict(new, peer_policy=None), dict(new, schema_version=2),
                dict(new, peer_policy=dict(peer_policy(), schema_version=True)),
                dict(new, peer_policy=dict(peer_policy(), operations=["approve"])),
                dict(new, peer_policy=dict(peer_policy(), operations=tuple(peer_policy()["operations"]))),
                dict(new, peer_policy=dict(peer_policy(), command="worker-script")),
                dict(new, adapter_kind="claude_cli", provider="anthropic", execution_policy=None)):
            with self.assertRaises((ValueError, ProviderError)):
                ModelProfile.from_dict(mutation)
        emitted = parsed.to_dict()
        emitted["peer_policy"]["operations"].append("approve")
        self.assertEqual(parsed.to_dict(), new)


class NativePeerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_peer_tools.PeerToolTests()
        self.fixture.setUp()
        self.profile = ModelProfile.from_dict(profile())
        assignment = self.fixture.fixture.assignment
        state, workspace = self.fixture.fixture.state_dir, self.fixture.fixture.source
        policy = SandboxPolicy(policy_id="native-peer-fixture", workspace=str(workspace),
            read_paths=(str(workspace),) + admission_paths(state, self.fixture.run, assignment["task_id"], assignment["agent_id"]),
            write_paths=(str(workspace),), environment_names=ENV_NAMES,
            network_destinations=("*",), credential_refs=(), trust_tier="developer_sandboxed")
        self.adapter = SimpleNamespace(state_dir=state, workspace=workspace, run_id=self.fixture.run,
            sandbox_policy=policy, peer_tools=self.fixture.tools, redactor=Redactor(env={}), artifact_store=None)

    async def asyncTearDown(self):
        self.fixture.doCleanups()

    def invocation(self):
        return NativePeerInvocation(self.adapter, self.profile, self.fixture.fixture.assignment, 1)

    async def test_exact_environment_config_redaction_and_cleanup(self):
        invocation = self.invocation()
        async with invocation as owned:
            token, path = owned.environment["CAMOL_PEER_TOKEN"], owned.endpoint.path
            self.assertTrue(path.is_socket())
            self.assertNotIn(token, json.dumps(owned.argv))
            self.assertNotIn(token, self.adapter.redactor.text("provider echoed " + token))
            self.assertNotIn(token, self.fixture.control.redactor.text(token))
            self.assertIn("CAMOL_PEER_TOKEN", " ".join(owned.argv))
            if __import__("sys").version_info >= (3, 11):
                import tomllib
                parsed = tomllib.loads(owned.argv[1])
                server = parsed["mcp_servers"]["camol_peers"]
                self.assertTrue(server["required"])
                self.assertEqual(set(server["enabled_tools"]), {"list_boxes", "observe_box", "inbox", "send_message"})
        self.assertFalse(path.exists())
        self.assertFalse(invocation.parent.exists())
        self.assertEqual(invocation.environment, {})
        await invocation.close()

    async def test_missing_grant_writable_runtime_and_wrong_turn_refuse_before_endpoint(self):
        base = self.adapter.sandbox_policy
        for changed in (replace(base, environment_names=()), replace(base, network_destinations=()),
                replace(base, read_paths=(str(self.adapter.workspace),)),
                replace(base, write_paths=base.write_paths + (str(Path(native_peer_module.__file__).resolve().parent / "peer_mcp.py"),)),
                replace(base, write_paths=base.write_paths + (str(Path(native_peer_module.__file__).resolve().parent.parent),))):
            self.adapter.sandbox_policy = changed
            with self.assertRaises(ProviderError):
                self.invocation()
        self.adapter.sandbox_policy = base
        with self.assertRaises(ProviderError):
            NativePeerInvocation(self.adapter, self.profile, self.fixture.fixture.assignment, 2)
        with self.assertRaises(ProviderError):
            NativePeerInvocation(self.adapter, self.profile, self.fixture.fixture.assignment, True)
        self.fixture.tools.close()
        with self.assertRaises(ValueError):
            self.invocation()

    async def test_provider_failure_and_cancellation_revoke_endpoint(self):
        for failure in (RuntimeError, asyncio.CancelledError):
            invocation = self.invocation()
            with self.assertRaises(failure):
                async with invocation:
                    raise failure("fixture")
            self.assertFalse(invocation.parent.exists())
            self.assertTrue(invocation.endpoint.snapshot()["closed"])

    async def test_retained_transport_content_is_not_reused_or_deleted(self):
        invocation = self.invocation()
        invocation.parent.mkdir(mode=0o700)
        foreign = invocation.parent / "unexpected"
        foreign.write_text("preserve")
        try:
            with self.assertRaisesRegex(ProviderError, "retained content"):
                async with invocation:
                    self.fail("must not open endpoint")
            self.assertIsNone(invocation.endpoint)
            self.assertEqual(foreign.read_text(), "preserve")
        finally:
            foreign.unlink()
            invocation.parent.rmdir()

    async def test_cleanup_failure_preserves_original_exception_and_revokes_capability(self):
        invocation = self.invocation()
        failure = asyncio.CancelledError()
        try:
            with self.assertRaises(asyncio.CancelledError) as caught:
                async with invocation:
                    foreign = invocation.parent / "unexpected"
                    foreign.write_text("preserve")
                    raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(failure.peer_cleanup_incomplete)
            self.assertTrue(invocation.endpoint.snapshot()["closed"])
            self.assertEqual(foreign.read_text(), "preserve")
        finally:
            foreign.unlink()
            await invocation.close()

    @unittest.skipUnless(os.environ.get("CAMOL_NATIVE_CODEX"), "optional explicitly selected installed Codex parser check")
    async def test_installed_codex_accepts_exact_per_invocation_mcp_configuration(self):
        async with self.invocation() as owned:
            process = await asyncio.create_subprocess_exec(os.environ["CAMOL_NATIVE_CODEX"], "mcp", "get",
                "camol_peers", "--json", *owned.argv, cwd="/tmp", stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, errors = await asyncio.wait_for(process.communicate(), 10)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            self.assertEqual(process.returncode, 0, errors.decode())
            value = json.loads(output)
            self.assertEqual(value["name"], "camol_peers")
            self.assertTrue(value["enabled"])
            self.assertEqual(set(value["enabled_tools"]), {"list_boxes", "observe_box", "inbox", "send_message"})
            self.assertNotIn(owned.environment["CAMOL_PEER_TOKEN"].encode(), output + errors)


MCP_CLIENT = r'''
import os, subprocess
prefix='mcp_servers={camol_peers={'
text=next(item for item in sys.argv if item.startswith(prefix))[len(prefix):-2]
config={}
while text:
 key, remaining=text.split('=',1)
 value, end=json.JSONDecoder().raw_decode(remaining)
 config[key]=value
 text=remaining[end:].lstrip(',')
assert config['required'] is True
assert '--strict-config' in sys.argv
assert set(config['enabled_tools']) == {'list_boxes','observe_box','inbox','send_message'}
assert 'shell_environment_policy.filters={CAMOL_PEER_ENDPOINT="exclude",CAMOL_PEER_TOKEN="exclude"}' in sys.argv
child=subprocess.Popen([config['command']]+config['args'],cwd=config['cwd'],
 env={name:os.environ[name] for name in config['env_vars']},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
def rpc(identity,method,params):
 child.stdin.write(json.dumps(dict(jsonrpc='2.0',id=identity,method=method,params=params))+'\n');child.stdin.flush()
 return json.loads(child.stdout.readline())
try:
 result=rpc(1,'initialize',dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='fake-native',version='1')))
 assert 'result' in result,result
 child.stdin.write(json.dumps(dict(jsonrpc='2.0',method='notifications/initialized'))+'\n');child.stdin.flush()
 listed=rpc(2,'tools/call',dict(name='list_boxes',arguments=dict(request_id='native-list',offset=0,limit=2)))
 assert listed['result']['isError'] is False,listed
 scope=json.loads(listed['result']['content'][0]['text'])['result']['subject']
 assert scope['task_id']==packet['task']['id']
 print(os.environ['CAMOL_PEER_TOKEN'],file=sys.stderr)
finally:
 child.stdin.close()
 child.wait(timeout=5)
 assert child.returncode==0,child.stderr.read()
'''


class NativePeerBuildTests(unittest.TestCase):
    setUp = test_evaluation.EvaluationLoopTests.setUp
    tearDown = test_evaluation.EvaluationLoopTests.tearDown

    def execute_peer_build(self, sandboxed=False, cleanup_fail=False, missing_strict_config=False):
        binary = self.root / "bin"
        binary.mkdir()
        executable = binary / "fake-codex"
        fixture = test_codex_adapter.FAKE_CODEX if missing_strict_config else test_codex_adapter.FAKE_CODEX.replace("--json --sandbox", "--json --sandbox --strict-config")
        executable.write_text(fixture.replace(
            "Path('argv.json').write_text", MCP_CLIENT + "\nPath('argv.json').write_text"))
        executable.chmod(0o755)
        plan = v5_plan("native-peer-build")
        plan["agents"][0]["adapter"] = dict(kind="codex_cli", profile="codex.json", profile_snapshot=profile(), timeout_seconds=30)
        if sandboxed:
            plan["agents"][0]["trust_tier"] = "developer_sandboxed"
        tokens = []
        contexts = []
        original = NativePeerInvocation.__aenter__
        async def capture(invocation):
            result = await original(invocation)
            tokens.append(result.environment["CAMOL_PEER_TOKEN"])
            contexts.append(invocation)
            if cleanup_fail:
                (invocation.parent / "unexpected").write_text("preserve")
            return result
        with patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}), patch.object(
                NativePeerInvocation, "__aenter__", capture):
            final, events = test_evaluation.EvaluationLoopTests.execute(self, plan)
        if missing_strict_config:
            self.assertEqual(final["tasks"]["change"]["status"], "waiting")
            self.assertEqual(final["tasks"]["change"]["attempts"], 0)
            self.assertIn("POLICY_DENIED", json.dumps(events))
            self.assertEqual(tokens, [])
            self.assertEqual(final["total_tokens"], 0)
            self.assertFalse(list(self.state.glob("packets/*/*/*.charge-pending.json")))
            return
        if cleanup_fail:
            try:
                self.assertNotEqual(final["status"], "awaiting_acceptance")
                self.assertTrue(contexts[0].cleanup_incomplete)
                self.assertTrue(contexts[0].endpoint.snapshot()["closed"])
                self.assertIn('"cleanup": "incomplete"', json.dumps(events))
                self.assertIn('"input_tokens": 12', json.dumps(events))
                self.assertIn('"output_tokens": 8', json.dumps(events))
                self.assertEqual(len(contexts), 1)
            finally:
                (contexts[0].parent / "unexpected").unlink()
                asyncio.run(contexts[0].close())
            return
        if final["status"] != "awaiting_acceptance":
            from camol.artifacts import ArtifactStore, artifact_refs
            artifacts = ArtifactStore(self.state)
            diagnostic = [artifacts.read(ref).decode("utf-8", "replace")[-3000:] for ref in artifact_refs(events)
                if ref.producer.get("channel") == "provider-stderr"]
            self.fail("native fixture did not finish: " + json.dumps(diagnostic))
        self.assertEqual(len(final["peer_tool_reads"]), 1)
        self.assertEqual(len(final["peer_tool_calls"]), 1)
        self.assertEqual(final["total_tokens"], 20)
        self.assertEqual(project(events), final)
        self.assertFalse((self.source / "output.txt").exists())
        agent = plan["agents"][0]["id"]
        self.assertFalse(parent_path(self.state, plan["run"]["id"], "change", agent).exists())
        self.assertEqual(len(tokens), 1)
        packet = json.loads((self.state / "packets" / plan["run"]["id"] / "change" / "turn-001.packet.json").read_text())
        with patch("camol.native_peers.NativePeerInvocation", side_effect=AssertionError("cached result must not open a peer endpoint")):
            recovered = asyncio.run(contexts[0].adapter.execute_turn(final["runbook"]["agents"][0],
                contexts[0].assignment, packet, 1, cost_budget_cents=0))
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["input_tokens"], 12)
        self.assertNotIn(tokens[0], json.dumps(events))
        for path in self.state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(tokens[0].encode(), path.read_bytes(), str(path))

    def test_frozen_v3_profile_runs_real_cli_relay_and_build_with_replay(self):
        self.execute_peer_build()

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS Seatbelt")
    def test_actual_sandboxed_native_worker_relay_build(self):
        self.execute_peer_build(sandboxed=True)

    def test_successful_provider_response_retains_usage_when_endpoint_cleanup_fails(self):
        self.execute_peer_build(cleanup_fail=True)

    def test_unsupported_cli_strict_config_is_denied_before_invocation_or_spend(self):
        self.execute_peer_build(missing_strict_config=True)
