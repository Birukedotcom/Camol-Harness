"""Concurrent native relay identities; no model/provider account is used."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from camol.native_peers import NativePeerInvocation, admission_paths, runtime
from camol.peer_tools import PeerTools
from camol.state import project
from tests import test_native_peers


class NativeFleetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_native_peers.NativePeerLifetimeTests.asyncSetUp
    asyncTearDown = test_native_peers.NativePeerLifetimeTests.asyncTearDown

    async def test_three_simultaneous_relays_route_exact_inboxes_and_revoke_independently(self):
        invocations, clients, scopes = [], [], self.fixture.fixture.assignments
        counter = 0

        async def rpc(client, method, params):
            nonlocal counter
            counter += 1
            client.stdin.write((json.dumps(dict(jsonrpc="2.0", id=counter, method=method, params=params)) + "\n").encode())
            await client.stdin.drain()
            value = json.loads(await asyncio.wait_for(client.stdout.readline(), 5))
            self.assertEqual(value["id"], counter)
            return value

        async def call(index, name, **arguments):
            response = await rpc(clients[index], "tools/call", dict(name=name, arguments=arguments))
            self.assertNotIn("error", response)
            self.assertFalse(response["result"]["isError"], response)
            return json.loads(response["result"]["content"][0]["text"])["result"]

        async with AsyncExitStack() as lifetime:
            for scope in scopes:
                tools = PeerTools(self.fixture.control, self.fixture.run, scope, 1)
                lifetime.callback(tools.close)
                policy = replace(self.adapter.sandbox_policy, read_paths=(str(self.adapter.workspace),) +
                    admission_paths(self.adapter.state_dir, self.fixture.run, scope["task_id"], scope["agent_id"]))
                adapter = SimpleNamespace(**dict(vars(self.adapter), peer_tools=tools, sandbox_policy=policy))
                owned = await lifetime.enter_async_context(NativePeerInvocation(adapter, self.profile, scope, 1))
                invocations.append(owned)
                executable, arguments, _ = runtime()
                client = await asyncio.create_subprocess_exec(executable, *arguments, cwd=owned.parent,
                    env=dict(owned.environment), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                clients.append(client)

                async def stop(process=client):
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    self.assertEqual(process.returncode, 0)

                lifetime.push_async_callback(stop)
                initialized = await rpc(client, "initialize", dict(protocolVersion="2025-06-18",
                    capabilities={}, clientInfo=dict(name="native-fleet-fixture", version="1")))
                self.assertNotIn("error", initialized)
                client.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await client.stdin.drain()

            self.assertEqual(len(scopes), 3)
            self.assertEqual(len({item.endpoint.token for item in invocations}), 3)
            self.assertEqual(len({item.parent for item in invocations}), 3)
            for index in range(len(scopes)):
                fleet = await call(index, "list_boxes", request_id="list", offset=0, limit=50)
                self.assertEqual(fleet["subject"]["task_id"], scopes[index]["task_id"])
                target = await call(index, "observe_box", request_id="observe",
                    box_id=scopes[(index + 1) % len(scopes)]["agent_id"])
                arguments = dict(request_id="same-local-id", target=target["result"], body="note-{}".format(index),
                    kind="information", correlation_id=None, ttl_seconds=60)
                sent = await call(index, "send_message", **arguments)
                self.assertEqual(await call(index, "send_message", **arguments), sent)

            for index in range(len(scopes)):
                inbox = await call(index, "inbox", request_id="inbox", offset=0, limit=10)
                self.assertEqual(len(inbox["result"]["messages"]), 1)
                self.assertEqual(inbox["result"]["messages"][0]["message"]["body"], "note-{}".format((index - 1) % 3))

            self.fixture.control.cancel_lease(self.fixture.run, scopes[0], "fixture-owner")
            rejected = await rpc(clients[0], "tools/call", dict(name="list_boxes",
                arguments=dict(request_id="after-revoke", offset=0, limit=50)))
            self.assertTrue(rejected["result"]["isError"])
            await call(1, "list_boxes", request_id="still-owned", offset=0, limit=50)
            state = self.fixture.control.state(self.fixture.run)
            self.assertEqual(len(state["box_messages"]), 3)
            self.assertEqual(state["total_tokens"], 0)
            self.assertEqual(project(self.fixture.fixture.store.read(self.fixture.run)), state)
            for owned in invocations:
                self.assertNotIn(owned.endpoint.token, json.dumps(state))

        self.assertTrue(all(not item.parent.exists() for item in invocations))
