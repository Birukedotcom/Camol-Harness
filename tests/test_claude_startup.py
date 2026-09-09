import asyncio
import json
import hashlib
import os
import sys
import time
import unittest
from unittest.mock import patch

from camol.claude_startup import ClaudeStartup
from camol.claude_peer_policy import execution_policy
from camol.native_peers import NativePeerInvocation
from camol.peer_mcp import NAMES
from camol.providers import ModelProfile, ProviderError
from camol.sandbox import SandboxInputError, SandboxError, MacOSSandboxBackend, DeveloperTrustedBackend
from tests import test_claude_peers, test_sandbox


def profile():
    return dict(test_claude_peers.profile(), schema_version=5, execution_policy=execution_policy(strict_startup=True))


def status():
    return dict(mcpServers=[dict(name="camol_peers", status="connected",
        serverInfo=dict(name="camol-peers", version="1"), tools=[dict(name=name) for name in NAMES])])


class Writer:
    def __init__(self, startup, mutate=lambda value: value):
        self.startup, self.mutate, self.sent = startup, mutate, []
        self.raw = []

    def write(self, raw):
        value = json.loads(raw)
        self.raw.append(raw)
        self.sent.append(value)
        if value["type"] == "control_request":
            payload = status() if value["request"]["subtype"] == "mcp_status" else {}
            response = self.mutate(dict(type="control_response", response=dict(subtype="success",
                request_id=value["request_id"], response=payload)))
            if response is not None:
                encoded = response if isinstance(response, bytes) else (json.dumps(response) + "\n").encode()
                # Arbitrary pipe boundaries cannot reinterpret a response.
                self.startup.feed(encoded[:17])
                self.startup.feed(encoded[17:])

    async def drain(self):
        await asyncio.sleep(0)


class StartupProtocolTests(unittest.IsolatedAsyncioTestCase):
    def startup(self, *, ready=True, authorization_failure=False):
        calls = []
        async def authorize():
            calls.append("reauthorized")
            if authorization_failure:
                raise ValueError("untrusted error prose")
        value = ClaudeStartup(b"PRIVATE TASK PROMPT", authorize=authorize, peer_ready=lambda: ready, timeout_seconds=0.03)
        return value, calls

    async def test_matching_control_and_owner_authorization_precede_prompt_once(self):
        startup, authorized = self.startup()
        writer = Writer(startup)
        await startup.write(writer)
        self.assertEqual([item["type"] for item in writer.sent], ["control_request", "control_request", "user"])
        self.assertNotIn("PRIVATE TASK PROMPT", json.dumps(writer.sent[:2]))
        self.assertEqual(writer.sent[-1]["message"]["content"], "PRIVATE TASK PROMPT")
        self.assertEqual(authorized, ["reauthorized"])
        self.assertTrue(startup.snapshot()["prompt_sent"])
        self.assertEqual(startup.snapshot()["stdin_attempted_sha256"], "sha256:" + hashlib.sha256(b"".join(writer.raw)).hexdigest())
        self.assertEqual(startup.snapshot()["stdin_attempted_bytes"], sum(map(len, writer.raw)))
        with self.assertRaises(SandboxInputError):
            await startup.write(writer)
        self.assertEqual(len(writer.sent), 3)

    async def test_bad_status_or_absent_owner_handshake_never_receives_prompt(self):
        cases = [dict(mcpServers=[]), dict(mcpServers=status()["mcpServers"] * 2)]
        for updates in (dict(name="other"), dict(status="pending"), dict(status="failed"),
                dict(serverInfo={}), dict(tools=[]), dict(tools=[dict(name="approve")] * 4)):
            cases.append(dict(mcpServers=[dict(status()["mcpServers"][0], **updates)]))
        for broken in cases:
            startup, authorized = self.startup()
            def mutate(response):
                if "mcpServers" in response["response"]["response"]:
                    response["response"]["response"] = broken
                return response
            writer = Writer(startup, mutate)
            with self.assertRaises(SandboxInputError):
                await startup.write(writer)
            self.assertFalse(startup.prompt_started)
            self.assertEqual(authorized, [])
        for ready, failure in ((False, False), (True, True)):
            startup, _ = self.startup(ready=ready, authorization_failure=failure)
            with self.assertRaises(SandboxInputError):
                await startup.write(Writer(startup))
            self.assertFalse(startup.prompt_started)
            self.assertNotIn("untrusted error prose", json.dumps(startup.snapshot()))

    async def test_malformed_foreign_duplicate_denied_and_unsolicited_controls_fail_closed(self):
        def duplicate(value):
            return ((json.dumps(value) + "\n") * 2).encode()
        for mutation in (lambda value: dict(type="control_request", request={}),
                lambda value: dict(type="assistant", message={}),
                lambda value: dict(value, response=dict(value["response"], request_id="foreign")),
                lambda value: dict(value, response=dict(value["response"], subtype="error")),
                lambda value: b'{"type":"system","type":"system"}\n',
                lambda value: b'x' * ((256 << 10) + 1), duplicate, lambda value: None):
            startup, authorized = self.startup()
            writer = Writer(startup, mutation)
            with self.assertRaises(SandboxInputError):
                await startup.write(writer)
            self.assertFalse(startup.prompt_started)
            self.assertFalse(authorized)

    async def test_eof_does_not_release_prompt(self):
        startup, _ = self.startup()
        writer = Writer(startup, lambda value: None)
        task = asyncio.create_task(startup.write(writer))
        await asyncio.sleep(0)
        startup.eof()
        with self.assertRaises(SandboxInputError):
            await task
        self.assertFalse(startup.prompt_started)

    async def test_cancel_during_authorization_does_not_release_prompt(self):
        entered = asyncio.Event()
        async def authorize():
            entered.set()
            await asyncio.Event().wait()
        startup = ClaudeStartup(b"prompt", authorize=authorize, peer_ready=lambda: True)
        task = asyncio.create_task(startup.write(Writer(startup)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(startup.failure, "STARTUP_CANCELLED")
        self.assertFalse(startup.prompt_started)

    async def test_owner_revocation_during_reauthorization_refuses_prompt(self):
        ready = True
        async def authorize():
            nonlocal ready
            ready = False
        startup = ClaudeStartup(b"prompt", authorize=authorize, peer_ready=lambda: ready)
        writer = Writer(startup)
        with self.assertRaises(SandboxInputError):
            await startup.write(writer)
        self.assertEqual(startup.failure, "OWNER_HANDSHAKE_STALE")
        self.assertFalse(startup.prompt_started)
        self.assertEqual(len(writer.sent), 2)

    async def test_write_failure_keeps_prompt_dispatch_unknown_not_unsent(self):
        startup, _ = self.startup()
        class BrokenWriter(Writer):
            async def drain(self):
                if self.sent[-1]["type"] == "user":
                    raise BrokenPipeError()
        with self.assertRaises(SandboxInputError):
            await startup.write(BrokenWriter(startup))
        self.assertTrue(startup.prompt_started)
        self.assertFalse(startup.prompt_sent)


class StagedSandboxTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_sandbox.SandboxTests.setUp
    tearDown = test_sandbox.SandboxTests.tearDown
    policy = test_sandbox.SandboxTests.policy

    async def test_eager_and_staged_input_are_rejected_before_any_process(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("must not launch")):
            with self.assertRaises(SandboxError):
                await DeveloperTrustedBackend().run([sys.executable, "-c", "pass"], cwd=self.workspace,
                    policy=self.policy("developer_trusted"), timeout_seconds=5, stdin_bytes=b"secret", input_protocol=object())

    async def test_cancellation_during_initialization_terminates_the_actual_child(self):
        async def authorize():
            self.fail("no handshake or prompt was authorized")
        startup = ClaudeStartup(b"secret", authorize=authorize, peer_ready=lambda: False)
        record = self.workspace / "invocation.json"
        task = asyncio.create_task(DeveloperTrustedBackend().run([sys.executable, "-c",
            "import sys,time; from pathlib import Path; sys.stdin.readline(); Path('waiting').write_text('yes'); time.sleep(60)"],
            cwd=self.workspace, policy=self.policy("developer_trusted"), timeout_seconds=30,
            input_protocol=startup, invocation_record=record))
        try:
            async def started():
                while not (self.workspace / "waiting").exists():
                    if task.done():
                        await task
                        self.fail("child exited before waiting")
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(started(), 5)
            pid = json.loads(record.read_text())["pid"]
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(startup.prompt_started)
        self.assertEqual(json.loads(record.read_text())["state"], "terminated")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_initialization_timeout_reaps_child_and_preserves_output(self):
        async def authorize():
            self.fail("unready startup must not authorize")
        startup = ClaudeStartup(b"secret", authorize=authorize, peer_ready=lambda: False, timeout_seconds=0.1)
        record = self.workspace / "invocation.json"
        result = await DeveloperTrustedBackend().run([sys.executable, "-c",
            "import sys,time; sys.stdin.readline(); print('startup diagnostic',file=sys.stderr,flush=True); time.sleep(60)"],
            cwd=self.workspace, policy=self.policy("developer_trusted"), timeout_seconds=5,
            input_protocol=startup, invocation_record=record)
        self.assertEqual(startup.failure, "STARTUP_TIMEOUT")
        self.assertFalse(startup.prompt_started)
        self.assertIn(b"startup diagnostic", result.stderr)
        self.assertNotEqual(result.exit_code, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(result.process_id, 0)

    async def test_unexpected_writer_error_cannot_leave_a_running_child(self):
        class BrokenProtocol:
            async def write(self, writer):
                raise RuntimeError("owner callback failure")
            def feed(self, chunk):
                pass
            def eof(self):
                pass
        result = await DeveloperTrustedBackend().run([sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=self.workspace, policy=self.policy("developer_trusted"), timeout_seconds=5,
            input_protocol=BrokenProtocol())
        self.assertNotEqual(result.exit_code, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(result.process_id, 0)

    async def exercise_detached_pipe_holder(self, mode):
        async def authorize():
            self.fail("unready startup must not authorize")
        startup = ClaudeStartup(b"secret", authorize=authorize, peer_ready=lambda: False, timeout_seconds=10)
        record = self.workspace / "invocation.json"
        # Detached fixture self-exits; it never writes outside this disposable
        # directory. Process-group termination is not descendant containment.
        program = """import os,sys,time
from pathlib import Path
sys.stdin.readline()
pid=os.fork()
if not pid:
 os.setsid()
 Path('detached').write_text('started')
 time.sleep(4)
 Path('detached-done').write_text('done')
 os._exit(0)
while not Path('detached').exists(): time.sleep(.005)
if sys.argv[1]=='refuse': print('{"type":"control_request","request":{}}',flush=True)
time.sleep(60)
"""
        began = time.monotonic()
        task = asyncio.create_task(DeveloperTrustedBackend().run([sys.executable, "-c", program, mode],
            cwd=self.workspace, policy=self.policy("developer_trusted"), timeout_seconds=1 if mode == "timeout" else 6,
            input_protocol=startup, invocation_record=record))
        try:
            if mode == "cancel":
                async def child_started():
                    while not (self.workspace / "detached").exists():
                        await asyncio.sleep(.01)
                await asyncio.wait_for(child_started(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                with self.assertRaisesRegex(SandboxError, "timed out" if mode == "timeout" else "output pipes did not close"):
                    await task
            self.assertLess(time.monotonic() - began, 3.5)
            self.assertFalse(startup.prompt_started)
            self.assertEqual(json.loads(record.read_text())["state"], "timed_out" if mode == "timeout" else "terminated")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            async def child_finished():
                while not (self.workspace / "detached-done").exists():
                    await asyncio.sleep(.02)
            await asyncio.wait_for(child_finished(), 5)
            await asyncio.sleep(.05)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    async def test_detached_pipe_holder_cannot_stall_startup_refusal(self):
        await self.exercise_detached_pipe_holder("refuse")

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    async def test_detached_pipe_holder_cannot_stall_cancellation(self):
        await self.exercise_detached_pipe_holder("cancel")

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    async def test_detached_pipe_holder_cannot_stall_outer_timeout(self):
        await self.exercise_detached_pipe_holder("timeout")


class StartupProfileTests(unittest.TestCase):
    def test_strict_startup_never_reinterprets_legacy_profile(self):
        self.assertEqual(ModelProfile.from_dict(profile()).to_dict(), profile())
        old = test_claude_peers.profile()
        self.assertEqual(ModelProfile.from_dict(old).to_dict(), old)
        for value in (dict(profile(), schema_version=4), dict(old, schema_version=5)):
            with self.assertRaises(ProviderError):
                ModelProfile.from_dict(value)


START = r'''
import os,subprocess
assert sys.argv[sys.argv.index('--input-format')+1]=='stream-json'
def control():
 value=json.loads(sys.stdin.readline())
 assert value['type']=='control_request'
 assert 'CONTEXT PACKET' not in json.dumps(value)
 return value
def reply(request, payload):
 print(json.dumps(dict(type='control_response',response=dict(subtype='success',request_id=request['request_id'],response=payload))),flush=True)
initial=control()
assert initial['request']['subtype']=='initialize'
child=None
config=json.loads(sys.argv[sys.argv.index('--mcp-config')+1])['mcpServers']['camol_peers']
def rpc(identity,method,params):
 child.stdin.write(json.dumps(dict(jsonrpc='2.0',id=identity,method=method,params=params))+'\n');child.stdin.flush()
 return json.loads(child.stdout.readline())
if MODE!='no-handshake':
 child=subprocess.Popen([config['command']]+config['args'],env={name:os.environ[name] for name in config['env']},
  stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 assert 'result' in rpc(1,'initialize',dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='fixture',version='1')))
 child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');child.stdin.flush()
 catalog=rpc(2,'tools/list',{})['result']['tools']
else:
 catalog=[dict(name=name) for name in ['list_boxes','observe_box','inbox','send_message']]
reply(initial,{})
query=control()
assert query['request']['subtype']=='mcp_status'
reply(query,dict(mcpServers=[dict(name='camol_peers',status='failed' if MODE=='bad-status' else 'connected',
 serverInfo=dict(name='camol-peers',version='1'),tools=[dict(name=tool['name']) for tool in catalog])]))
incoming=sys.stdin.readline()
if not incoming:
 if child:
  child.stdin.close();child.wait(timeout=5)
 raise SystemExit(0)
message=json.loads(incoming)
assert message['type']=='user'
Path('prompt-received').write_text('yes')
prompt=message['message']['content']
'''
WORK = test_claude_peers.BODY.replace("prompt=sys.stdin.read()", "")
FINISH = r'''
if child:
 listed=rpc(3,'tools/call',dict(name='list_boxes',arguments=dict(request_id='after-prompt',offset=0,limit=2)))
 assert listed['result']['isError'] is False
 child.stdin.close();child.wait(timeout=5)
print(os.environ['CAMOL_PEER_TOKEN'],file=sys.stderr)
''' + test_claude_peers.FINAL


class StartupBuildTests(unittest.TestCase):
    setUp = test_claude_peers.ClaudePeerBuildTests.setUp
    tearDown = test_claude_peers.ClaudePeerBuildTests.tearDown

    def execute_build(self, *, mode="good", sandboxed=False):
        # Reuse the existing full runner/admission fixture, replacing only its
        # controlled provider protocol and exact opt-in profile.
        header = test_claude_peers.HEADER + "\nMODE=" + repr(mode) + "\n"
        original = test_claude_peers.profile
        payload = dict(original(), schema_version=5, execution_policy=execution_policy(strict_startup=True))
        contexts = []
        enter = NativePeerInvocation.__aenter__
        async def capture(invocation):
            contexts.append(invocation)
            return await enter(invocation)
        with patch.object(test_claude_peers, "profile", return_value=payload), patch.object(test_claude_peers, "HEADER", header), \
                patch.object(test_claude_peers, "BODY", START + WORK), patch.object(test_claude_peers, "CLIENT", ""), \
                patch.object(test_claude_peers, "FINAL", FINISH), patch.object(NativePeerInvocation, "__aenter__", capture):
            test_claude_peers.ClaudePeerBuildTests.execute_build(self, sandboxed=sandboxed, startup_denied=mode != "good")
        self.assertTrue(contexts)
        for context in contexts:
            self.assertEqual((context.adapter.workspace / "prompt-received").exists(), mode == "good")
            self.assertTrue(context.endpoint.snapshot()["closed"])

    def test_full_build_releases_prompt_only_after_native_status_and_owner_handshake(self):
        self.execute_build()

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS Seatbelt")
    def test_staged_build_under_actual_macos_sandbox(self):
        self.execute_build(sandboxed=True)

    def test_native_connected_claim_without_owner_handshake_does_not_receive_prompt(self):
        self.execute_build(mode="no-handshake")

    def test_native_failed_peer_never_receives_prompt(self):
        self.execute_build(mode="bad-status")
