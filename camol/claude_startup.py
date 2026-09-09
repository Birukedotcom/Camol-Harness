"""Bounded owner-side startup protocol; never grants a provider permission."""

import asyncio
import json
import hashlib
from uuid import uuid4

from .json_contracts import decode_contract
from .peer_mcp import NAMES
from .sandbox import SandboxInputError


class ClaudeStartup:
    def __init__(self, prompt, *, authorize, peer_ready, timeout_seconds=10):
        self.prompt, self.authorize, self.peer_ready = prompt, authorize, peer_ready
        self.timeout = timeout_seconds
        self.phase, self.failure = "created", None
        self.prompt_started = self.prompt_sent = False
        self.buffer = bytearray()
        self.frames = self.bytes = 0
        self.pending = self.expected = None
        self.stdin_digest, self.stdin_bytes = hashlib.sha256(), 0

    def snapshot(self):
        return dict(protocol="claude-stream-startup-v1", phase=self.phase, failure=self.failure,
            prompt_dispatch_started=self.prompt_started, prompt_sent=self.prompt_sent,
            startup_frames=self.frames, startup_bytes=self.bytes,
            stdin_attempted_sha256="sha256:" + self.stdin_digest.hexdigest(), stdin_attempted_bytes=self.stdin_bytes,
            prompt_sha256="sha256:" + hashlib.sha256(self.prompt).hexdigest(),
            provider_billing="not_established_by_startup")

    def abort(self, code):
        if self.failure is None:
            self.failure, self.phase = code, "failed"
        if self.pending is not None and not self.pending.done():
            self.pending.set_exception(SandboxInputError(self.failure))

    def _check(self):
        if self.failure:
            raise SandboxInputError(self.failure)

    def feed(self, chunk):
        # After release, ordinary provider output belongs to the adapter parser.
        if self.prompt_started or self.failure:
            return
        self.bytes += len(chunk)
        if self.bytes > 2 << 20:
            self.abort("STARTUP_OUTPUT_LIMIT")
            return
        self.buffer.extend(chunk)
        try:
            while b"\n" in self.buffer:
                raw, _, remainder = self.buffer.partition(b"\n")
                self.buffer = bytearray(remainder)
                self.frames += 1
                if self.frames > 128:
                    raise ValueError()
                value = decode_contract(raw, max_bytes=256 << 10)
                if not isinstance(value, dict):
                    raise ValueError()
                if value.get("type") == "system":
                    continue
                if value.get("type") != "control_response" or set(value) != {"type", "response"}:
                    self.abort("UNREQUESTED_STARTUP_OUTPUT")
                    return
                response = value["response"]
                if (not isinstance(response, dict) or self.pending is None or self.pending.done()
                        or response.get("request_id") != self.expected):
                    self.abort("UNMATCHED_CONTROL_RESPONSE")
                    return
                if (set(response) != {"subtype", "request_id", "response"}
                        or response["subtype"] != "success" or not isinstance(response["response"], dict)):
                    self.abort("CONTROL_REQUEST_REJECTED")
                    return
                self.pending.set_result(response["response"])
            if len(self.buffer) > 256 << 10:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self.abort("MALFORMED_STARTUP_OUTPUT")

    def eof(self):
        if not self.prompt_started:
            self.abort("STARTUP_EOF")

    async def _request(self, writer, request):
        self._check()
        self.expected = "camol-startup-" + uuid4().hex
        self.pending = asyncio.get_running_loop().create_future()
        self._write(writer, (json.dumps(dict(type="control_request", request_id=self.expected, request=request)) + "\n").encode())
        await writer.drain()
        try:
            value = await asyncio.wait_for(self.pending, self.timeout)
        except asyncio.TimeoutError:
            self.abort("STARTUP_TIMEOUT")
            raise SandboxInputError(self.failure)
        self._check()
        return value

    def _write(self, writer, raw):
        self.stdin_digest.update(raw)
        self.stdin_bytes += len(raw)
        writer.write(raw)

    def _status(self, value):
        servers = value.get("mcpServers")
        if set(value) != {"mcpServers"} or not isinstance(servers, list) or len(servers) != 1:
            raise SandboxInputError("PEER_STATUS_MISMATCH")
        server = servers[0]
        if (not isinstance(server, dict) or server.get("name") != "camol_peers"
                or server.get("status") != "connected" or server.get("serverInfo") != dict(name="camol-peers", version="1")):
            raise SandboxInputError("PEER_NOT_CONNECTED")
        tools = server.get("tools")
        if (not isinstance(tools, list) or len(tools) != len(NAMES)
                or any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in tools)
                or {tool["name"] for tool in tools} != set(NAMES)):
            raise SandboxInputError("PEER_TOOLS_MISMATCH")

    async def write(self, writer):
        if self.phase != "created":
            raise SandboxInputError("STARTUP_ALREADY_USED")
        try:
            self.phase = "initializing"
            await self._request(writer, dict(subtype="initialize", hooks=None, skills=[]))
            self.phase = "checking_peers"
            self._status(await self._request(writer, dict(subtype="mcp_status")))
            message = dict(type="user", session_id="", parent_tool_use_id=None,
                message=dict(role="user", content=self.prompt.decode("utf-8")))
            encoded = (json.dumps(message) + "\n").encode()
            if not self.peer_ready():
                raise SandboxInputError("OWNER_HANDSHAKE_MISSING")
            self.phase = "authorizing"
            await self.authorize()
            self._check()
            if not self.peer_ready():
                raise SandboxInputError("OWNER_HANDSHAKE_STALE")
            self.prompt_started, self.phase = True, "prompt_dispatch_started"
            self._write(writer, encoded)
            await writer.drain()
            self.prompt_sent, self.phase = True, "prompt_sent"
        except asyncio.CancelledError:
            self.abort("STARTUP_CANCELLED")
            raise
        except SandboxInputError as error:
            self.abort(str(error))
            raise
        except Exception as error:
            self.abort("STARTUP_AUTHORIZATION_OR_IO_FAILED")
            raise SandboxInputError(self.failure) from error
        finally:
            if self.pending is not None and self.pending.done() and not self.pending.cancelled():
                self.pending.exception()
