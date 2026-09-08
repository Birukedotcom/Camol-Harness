import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import camol
from camol import peer_transport
from camol.peer_transport import PeerEndpoint, PeerTransportError, request
from camol.peer_tools import PeerTools
from camol.state import project
from tests import test_peer_tools


class PeerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_peer_tools.PeerToolTests()
        self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="camol-peer-", dir="/tmp")
        self.parent = Path(self.temp.name).resolve()
        self.endpoints = []

    async def asyncTearDown(self):
        for endpoint in self.endpoints:
            await endpoint.close()
        self.temp.cleanup()
        self.fixture.doCleanups()

    async def endpoint(self, tools=None):
        endpoint = PeerEndpoint(tools or self.fixture.tools, self.parent)
        self.endpoints.append(endpoint)
        return await endpoint.start()

    async def call(self, endpoint, operation="list", arguments=None, request_id="list", token=None):
        return await asyncio.to_thread(request, endpoint.path, token or endpoint.token,
            operation, dict(offset=0, limit=2) if arguments is None else arguments, request_id=request_id)

    async def raw(self, endpoint, content):
        reader, writer = await asyncio.open_unix_connection(str(endpoint.path))
        try:
            writer.write(content)
            await writer.drain()
            return await asyncio.wait_for(reader.readline(), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    def state(self):
        return self.fixture.control.state(self.fixture.run)

    async def test_real_subprocess_observes_with_bound_token_without_owner_control(self):
        endpoint = await self.endpoint()
        token = endpoint.token
        package_root = str(Path(camol.__file__).resolve().parents[1])
        code = "import sys,json;sys.path.insert(0," + repr(package_root) + ");from camol.peer_transport import request;v=json.loads(sys.stdin.read());print(json.dumps(request(v['path'],v['token'],'list',{'offset':0,'limit':2},request_id='child-list')))"
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code,
            cwd="/tmp", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(json.dumps(dict(path=str(endpoint.path), token=token)).encode()), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, error)
        response = json.loads(output)
        self.assertIsNone(response["error"])
        self.assertEqual(response["request_id"], "child-list")
        self.assertEqual(response["result"]["subject"], endpoint.tools.caller)
        self.assertEqual(len(response["result"]["result"]["boxes"]), 2)
        events = self.fixture.fixture.store.read(self.fixture.run)
        self.assertEqual(project(events), self.state())
        self.assertNotIn(token, json.dumps(events))
        self.assertNotIn(token, json.dumps(endpoint.snapshot()))
        self.assertNotIn(token.encode(), output + error)
        self.assertEqual(endpoint.snapshot()["counts"]["success"], 1)

    async def test_wrong_endpoint_token_impersonation_and_control_commands_are_denied(self):
        endpoint = await self.endpoint()
        other = PeerTools(self.fixture.control, self.fixture.run, self.fixture.fixture.assignments[1], 1)
        peer = await self.endpoint(other)
        before = copy.deepcopy(self.state())
        denied = await self.call(peer, token=endpoint.token)
        self.assertEqual(denied["error"], dict(code="AUTH_REQUIRED", effect="not_dispatched"))
        self.assertEqual(self.state(), before)
        forged = dict(schema="camol.peer_request", schema_version=1, token=endpoint.token,
            request_id="impersonate", operation="list", arguments=dict(offset=0, limit=2), subject=other.caller)
        self.assertEqual(json.loads(await self.raw(endpoint, json.dumps(forged).encode() + b"\n"))["error"]["code"], "INVALID_REQUEST")
        result = await self.call(endpoint, "approve", {}, "control")
        self.assertEqual(result["error"]["code"], "TOOL_DENIED")
        self.assertEqual(self.state()["approved_by"], before["approved_by"])
        self.assertEqual(len(self.state()["peer_tool_calls"]), 1)

    async def test_handshake_is_passive_bound_and_revocable(self):
        endpoint = await self.endpoint()
        before = copy.deepcopy(self.state())
        result = await self.call(endpoint, "handshake", {}, "hello")
        self.assertEqual(result["result"]["subject"], endpoint.tools.caller)
        self.assertEqual(result["result"]["turn_number"], endpoint.tools.turn)
        self.assertFalse(result["result"]["readiness_proven"])
        self.assertEqual(before, self.state())
        self.fixture.control.cancel_lease(self.fixture.run, self.fixture.fixture.assignment, "owner")
        self.assertEqual((await self.call(endpoint, "handshake", {}, "hello"))["error"]["code"], "TURN_CLOSED")

    async def test_client_rejects_unbound_malformed_and_oversized_server_replies(self):
        endpoint = await self.endpoint()
        valid = dict(schema="camol.peer_response", schema_version=1, request_id="read", result={}, error=None)
        malformed = [dict(valid, schema_version=True), dict(valid, request_id="wrong"),
            dict(valid, request_id=None), dict(valid, result=[]), dict(valid, extra=True),
            dict(valid, result=None, error=dict(code="UNAVAILABLE", effect="success")),
            dict(valid, request_id=None, result=None, error=dict(code="UNAVAILABLE", effect="unknown"))]
        encoded = [json.dumps(item).encode() + b"\n" for item in malformed]
        encoded.append(b"x" * (peer_transport.MAX_RESPONSE + 1) + b"\n")
        for reply in encoded:
            async def substituted(writer, raw):
                writer.write(reply)
                await writer.drain()
            with patch.object(endpoint, "_send", substituted):
                with self.assertRaises(ValueError):
                    await self.call(endpoint, request_id="read")
        # The operation was admitted despite failed delivery; exact retries reuse it.
        self.assertEqual(len(self.state()["peer_tool_reads"]), 1)

    async def test_observe_send_retry_and_revocation_preserve_mailbox_authority(self):
        endpoint = await self.endpoint()
        box = self.fixture.fixture.assignments[1]["agent_id"]
        target = self.fixture.fixture.mailbox.observe(box)
        arguments = dict(target=target, body="note", kind="information", correlation_id=None, ttl_seconds=300)
        self.assertEqual((await self.call(endpoint, "send", arguments, "message"))["error"]["code"], "TOOL_DENIED")
        observed = await self.call(endpoint, "observe", dict(box_id=box), "observe")
        arguments["target"] = observed["result"]["result"]
        first = await self.call(endpoint, "send", arguments, "message")
        retry = await self.call(endpoint, "send", arguments, "message")
        self.assertIsNone(first["error"])
        self.assertEqual(first, retry)
        self.assertEqual(len(self.state()["box_messages"]), 1)
        self.fixture.control.cancel_lease(self.fixture.run, self.fixture.fixture.assignment, "owner")
        before = copy.deepcopy(self.state())
        self.assertEqual((await self.call(endpoint))["error"]["code"], "TURN_CLOSED")
        self.assertEqual(self.state(), before)

    async def test_invalid_duplicate_oversized_partial_frames_and_secret_echo_never_dispatch(self):
        endpoint = await self.endpoint()
        before = copy.deepcopy(self.state())
        for raw in (b'{"schema":1,"schema":2}\n', b"[1,2]\n", b"{" + b"x" * peer_transport.MAX_REQUEST + b"\n"):
            response = json.loads(await self.raw(endpoint, raw))
            self.assertEqual(response["error"]["code"], "INVALID_REQUEST")
        with patch.object(peer_transport, "IO_TIMEOUT", .15):
            response = json.loads(await self.raw(endpoint, b"{"))
        self.assertEqual(response["error"]["code"], "READ_TIMEOUT")
        response = await self.call(endpoint, request_id=endpoint.token)
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.state(), before)

    async def test_connection_caps_and_shutdown_close_pending_clients(self):
        endpoint = await self.endpoint()
        with patch.object(peer_transport, "MAX_ACTIVE", 1), patch.object(peer_transport, "MAX_CONNECTIONS", 2):
            reader, writer = await asyncio.open_unix_connection(str(endpoint.path))
            await asyncio.sleep(.01)
            self.assertEqual(await self.raw(endpoint, b"{}\n"), b"")
            old_path, old_token = endpoint.path, endpoint.token
            await endpoint.close()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            writer.close()
            await writer.wait_closed()
        self.assertFalse(old_path.exists())
        self.assertEqual(endpoint.snapshot()["counts"]["connection_limit_denied"], 1)
        self.assertTrue(endpoint.snapshot()["closed"])
        self.assertEqual(endpoint.snapshot()["active_connections"], 0)
        with self.assertRaises(OSError):
            await asyncio.to_thread(request, old_path, old_token, "list", {}, request_id="closed")

    async def test_private_socket_requirements_and_replaced_cleanup_target(self):
        endpoint = await self.endpoint()
        token = endpoint.token
        os.chmod(endpoint.path, 0o666)
        with self.assertRaises(PeerTransportError):
            await self.call(endpoint)
        os.chmod(endpoint.path, 0o600)
        alias = self.parent / "alias"
        alias.symlink_to(endpoint.path)
        with self.assertRaises(PeerTransportError):
            await asyncio.to_thread(request, alias, token, "list", {}, request_id="alias")
        endpoint.path.unlink()
        endpoint.path.write_text("foreign-file")
        with self.assertRaisesRegex(PeerTransportError, "refusing cleanup"):
            await endpoint.close()
        self.assertEqual(endpoint.path.read_text(), "foreign-file")
        endpoint.path.unlink()
        alias.unlink()

    async def test_lost_response_does_not_cancel_committed_read_or_retry_identity(self):
        endpoint = await self.endpoint()
        async def lose(writer, raw):
            writer.close()
            raise ConnectionResetError("fixture lost transport reply")
        with patch.object(endpoint, "_send", side_effect=lose):
            with self.assertRaisesRegex(PeerTransportError, "lost"):
                await self.call(endpoint)
        saved = next(iter(self.state()["peer_tool_reads"].values()))
        # Unknown transport delivery must not cause a new logical request or
        # silently refresh a target. The same retry returns its recorded cut.
        self.assertEqual((await self.call(endpoint))["result"], saved)
        self.assertEqual(len(self.state()["peer_tool_reads"]), 1)
        self.assertEqual(len(self.state()["peer_tool_calls"]), 2)
        self.assertEqual(endpoint.snapshot()["counts"]["response_delivery_unknown"], 1)
