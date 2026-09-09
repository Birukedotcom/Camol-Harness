"""Actual local OS transport boundary, without a native CLI or model request."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import camol
from camol.peer_transport import PeerEndpoint
from camol.sandbox import MacOSSandboxBackend, SandboxPolicy, system_read_paths
from tests import test_peer_tools


@unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS Seatbelt")
class PeerSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_peer_tools.PeerToolTests()
        self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="camol-peer-os-", dir="/tmp")
        self.parent = Path(self.temp.name).resolve()
        self.workspace = self.parent / "workspace"
        self.workspace.mkdir()
        self.endpoint = await PeerEndpoint(self.fixture.tools, self.parent).start()

    async def asyncTearDown(self):
        await self.endpoint.close()
        self.temp.cleanup()
        self.fixture.doCleanups()

    async def invoke(self, network):
        root = str(Path(camol.__file__).resolve().parents[1])
        policy = SandboxPolicy(policy_id="peer-fixture", workspace=str(self.workspace),
            read_paths=(str(self.workspace), str(self.endpoint.directory), root) + system_read_paths(sys.executable),
            write_paths=(str(self.workspace),), environment_names=("PATH",),
            network_destinations=network, credential_refs=(), trust_tier="developer_sandboxed")
        code = ("import sys,json;sys.path.insert(0," + repr(root) + ");"
            "from camol.peer_transport import request;from pathlib import Path;v=json.loads(sys.stdin.read());"
            "result=request(v['path'],v['token'],'list',{'offset':0,'limit':1},request_id='sandbox-read')\n"
            "try: Path(v['path']).unlink()\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('worker replaced owner socket')\n"
            "print(json.dumps(result))")
        result = await MacOSSandboxBackend().run([sys.executable, "-I", "-c", code], cwd=self.workspace,
            policy=policy, timeout_seconds=10,
            stdin_bytes=json.dumps(dict(path=str(self.endpoint.path), token=self.endpoint.token)).encode())
        self.assertNotIn(self.endpoint.token.encode(), result.stdout + result.stderr)
        return result

    async def test_denied_network_stays_denied_and_explicit_network_can_read_exact_peer(self):
        before = self.fixture.control.state(self.fixture.run)
        denied = await self.invoke(())
        self.assertNotEqual(denied.exit_code, 0)
        self.assertEqual(self.fixture.control.state(self.fixture.run), before)
        permitted = await self.invoke(("*",))
        self.assertEqual(permitted.exit_code, 0, permitted.stderr.decode())
        value = json.loads(permitted.stdout)
        self.assertIsNone(value["error"])
        self.assertEqual(value["result"]["subject"], self.endpoint.tools.caller)
        self.assertTrue(self.endpoint.path.exists())
