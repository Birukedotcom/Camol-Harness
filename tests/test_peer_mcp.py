"""Real stdio/Unix-socket protocol fixtures; no provider, account or model."""

import asyncio
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import camol
from camol.peer_mcp import Relay, VERSION, serve, tool_definitions
from camol.peer_transport import MAX_REQUEST, PeerEndpoint
from tests import test_peer_tools


def rpc(identity, method, **params):
    return dict(jsonrpc="2.0", id=identity, method=method, params=params)


def initialization(identity=1):
    return rpc(identity, "initialize", protocolVersion=VERSION,
        capabilities={}, clientInfo=dict(name="fixture", version="1"))


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.relay = Relay("/not-used", "a" * 64)

    def ready(self):
        with patch("camol.peer_mcp.request", return_value=dict(error=None, result={})):
            result = self.relay.handle(initialization())
        self.assertEqual(result["result"]["protocolVersion"], VERSION)
        self.relay.handle(dict(jsonrpc="2.0", method="notifications/initialized"))

    def test_lifecycle_catalog_and_unknown_methods(self):
        self.assertIn("error", self.relay.handle(rpc(0, "tools/list")))
        self.ready()
        catalog = self.relay.handle(rpc(2, "tools/list"))["result"]["tools"]
        self.assertEqual({tool["name"] for tool in catalog}, {"list_boxes", "observe_box", "inbox", "send_message"})
        self.assertTrue(all(not tool["inputSchema"]["additionalProperties"] for tool in catalog))
        self.assertEqual(self.relay.handle(rpc(3, "approve"))["error"]["code"], -32601)
        self.assertEqual(self.relay.handle(initialization(4))["error"]["code"], -32602)
        self.assertEqual(self.relay.handle(rpc(2, "ping"))["error"]["code"], -32600)
        self.assertIsNone(self.relay.handle(rpc("a" * 64, "ping"))["id"])
        catalog[0]["inputSchema"]["properties"].clear()
        self.assertTrue(tool_definitions()[0]["inputSchema"]["properties"])

    def test_missing_unknown_and_invalid_logical_identity_never_dispatch(self):
        self.ready()
        with patch("camol.peer_mcp.request", side_effect=AssertionError("must not dispatch")):
            for identity, arguments in enumerate(({}, dict(request_id="x", offset=0, limit=1, subject={}),
                    dict(request_id=False, offset=0, limit=1), dict(request_id="a" * 64, offset=0, limit=1)), 2):
                result = self.relay.handle(rpc(identity, "tools/call", name="list_boxes", arguments=arguments))
                self.assertEqual(result["error"]["code"], -32602)

    def test_transport_loss_is_unknown_and_tool_denial_is_not_protocol_success(self):
        self.ready()
        with patch("camol.peer_mcp.request", side_effect=OSError("secret-prose")):
            result = self.relay.handle(rpc(2, "tools/call", name="list_boxes",
                arguments=dict(request_id="retry", offset=0, limit=1)))
        self.assertTrue(result["result"]["isError"])
        self.assertNotIn("secret-prose", json.dumps(result))
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["error"]["effect"], "unknown")

    def test_framing_duplicate_keys_bounds_and_stdout_protocol_only(self):
        for raw in (b'{"jsonrpc":"2.0","jsonrpc":"2.0"}\n', b'{\n', b'{}', b'x' * (MAX_REQUEST + 1)):
            destination = io.BytesIO()
            serve(self.relay, io.BytesIO(raw), destination)
            response = json.loads(destination.getvalue())
            self.assertEqual(response["error"]["code"], -32700)
        output = io.BytesIO()
        serve(self.relay, io.BytesIO((json.dumps(rpc(50, "ping")) + "\n").encode()), output)
        self.assertEqual(json.loads(output.getvalue())["result"], {})


class RelayProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_peer_tools.PeerToolTests()
        self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="camol-mcp-", dir="/tmp")
        self.endpoint = await PeerEndpoint(self.fixture.tools, Path(self.temp.name).resolve()).start()
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await self.endpoint.close()
        self.temp.cleanup()
        self.fixture.doCleanups()

    async def child(self, token=None):
        root = str(Path(camol.__file__).resolve().parents[1])
        code = "import sys;sys.path.insert(0," + repr(root) + ");from camol.peer_mcp import main;raise SystemExit(main())"
        env = dict(os.environ, CAMOL_PEER_ENDPOINT=str(self.endpoint.path), CAMOL_PEER_TOKEN=token or self.endpoint.token)
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code, env=env,
            cwd="/tmp", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        return process

    async def send(self, child, value):
        child.stdin.write((json.dumps(value) + "\n").encode())
        await child.stdin.drain()
        if "id" in value:
            raw = await asyncio.wait_for(child.stdout.readline(), 10)
            self.assertNotIn(self.endpoint.token.encode(), raw)
            return json.loads(raw)

    async def call(self, child, identity, name, **arguments):
        result = await self.send(child, rpc(identity, "tools/call", name=name, arguments=arguments))
        return json.loads(result["result"]["content"][0]["text"])

    async def test_real_stdio_to_socket_observe_send_retry_and_closed_turn(self):
        child = await self.child()
        before = self.fixture.control.state(self.fixture.run)
        initialized = await self.send(child, initialization())
        self.assertIn("tools", initialized["result"]["capabilities"])
        self.assertEqual(self.fixture.control.state(self.fixture.run), before)
        await self.send(child, dict(jsonrpc="2.0", method="notifications/initialized"))
        listing = await self.call(child, 2, "list_boxes", request_id="list", offset=0, limit=2)
        self.assertIsNone(listing["error"])
        box = self.fixture.fixture.assignments[1]["agent_id"]
        observed = await self.call(child, 3, "observe_box", request_id="observe", box_id=box)
        arguments = dict(request_id="message", target=observed["result"]["result"], body="fixture message",
            kind="information", correlation_id=None, ttl_seconds=300)
        first = await self.call(child, 4, "send_message", **arguments)
        second = await self.call(child, 5, "send_message", **arguments)
        self.assertEqual(first, second)
        self.assertIsNone(first["error"])
        self.assertEqual(len(self.fixture.control.state(self.fixture.run)["box_messages"]), 1)
        self.fixture.control.cancel_lease(self.fixture.run, self.fixture.fixture.assignment, "owner")
        denied = await self.call(child, 6, "inbox", request_id="closed", offset=0, limit=1)
        self.assertEqual(denied["error"]["code"], "TURN_CLOSED")
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 5)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(await child.stderr.read(), b"")

    async def test_wrong_capability_fails_initialization_without_ledger_effects(self):
        child = await self.child("b" * 64)
        before = self.fixture.control.state(self.fixture.run)
        result = await self.send(child, initialization())
        self.assertEqual(result["error"]["code"], -32000)
        self.assertEqual(before, self.fixture.control.state(self.fixture.run))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional independent MCP SDK acceptance environment")
    async def test_independent_sdk_negotiates_and_calls_real_stdio_relay(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        root = str(Path(camol.__file__).resolve().parents[1])
        code = "import sys;sys.path.insert(0," + repr(root) + ");from camol.peer_mcp import main;raise SystemExit(main())"
        parameters = StdioServerParameters(command=sys.executable, args=["-I", "-c", code],
            cwd="/tmp", env=dict(CAMOL_PEER_ENDPOINT=str(self.endpoint.path), CAMOL_PEER_TOKEN=self.endpoint.token))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.protocolVersion, VERSION)
                catalog = await session.list_tools()
                self.assertEqual(len(catalog.tools), 4)
                result = await session.call_tool("list_boxes", dict(request_id="sdk-list", offset=0, limit=2))
                self.assertFalse(result.isError)
                data = json.loads(result.content[0].text)
                self.assertIsNone(data["error"])
                self.assertEqual(data["result"]["subject"], self.endpoint.tools.caller)


if __name__ == "__main__":
    unittest.main()
