"""Local worker-scoped IPC for native bridges; never an owner-control server.

An explicit embedding owner starts one endpoint for a currently owned PeerTools
turn. This is Camol's internal protocol, not an MCP or remote network listener.
"""

import asyncio
from collections import Counter
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import tempfile

from .json_contracts import decode_contract
from .mailbox import fields
from .schema import canonical_digest, require_identifier


MAX_REQUEST = 65536
MAX_RESPONSE = 131072
MAX_CONNECTIONS = 256
MAX_ACTIVE = 8
IO_TIMEOUT = 5
ERROR_CODES = {"INVALID_REQUEST", "AUTH_REQUIRED", "TURN_CLOSED", "TOOL_DENIED", "READ_TIMEOUT", "UNAVAILABLE", "RESPONSE_TOO_LARGE"}


class PeerTransportError(ValueError):
    pass


def _encode(value, maximum):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > maximum:
        raise PeerTransportError("peer transport frame exceeds its byte bound")
    return raw


def _identity(path):
    value = path.lstat()
    return value.st_dev, value.st_ino


def _response(request_id, *, result=None, code=None, dispatched=False):
    return dict(schema="camol.peer_response", schema_version=1, request_id=request_id,
        result=result, error=None if code is None else dict(code=code,
            effect="unknown" if dispatched else "not_dispatched"))


class PeerEndpoint:
    """One disposable capability, fixed caller/turn, bounded local connections.

    Use only on the owning event-loop thread. The token is deliberately absent
    from repr, paths, audit counters and ledger metadata. It must be delivered to
    a worker through its explicitly approved native transport, not global config.
    """

    def __init__(self, tools, parent):
        self.tools, self.parent = tools, Path(parent)
        self.token = secrets.token_hex(32)
        self.path = self.directory = self.server = None
        self._directory_identity = self._socket_identity = None
        self._handlers, self._writers = set(), set()
        self._counts = Counter()
        self._closed = False

    async def start(self):
        if self.server is not None or self._closed:
            raise PeerTransportError("peer endpoint cannot be reopened")
        self.tools._check()
        root = self.parent
        info = root.lstat()
        if (not root.is_absolute() or root.resolve() != root or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise PeerTransportError("peer endpoint parent must be a canonical owner-private directory")
        self.directory = Path(tempfile.mkdtemp(prefix="peer-", dir=str(root)))
        self._directory_identity = _identity(self.directory)
        self.path = self.directory / "rpc.sock"
        try:
            if len(os.fsencode(self.path)) > 100:
                raise PeerTransportError("peer endpoint path exceeds the portable Unix socket bound")
            self.server = await asyncio.start_unix_server(self._accepted, path=str(self.path), limit=MAX_REQUEST)
            self._socket_identity = _identity(self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            await self.close()
            raise
        return self

    def snapshot(self):
        """Ephemeral content-free connection counters, not durable tool outcomes."""
        return dict(schema="camol.peer_transport_counts", schema_version=1,
            caller=dict(self.tools.caller), turn_number=self.tools.turn, closed=self._closed,
            active_connections=len(self._handlers), counts=dict(sorted(self._counts.items())),
            persistence="owner_memory_only", tool_outcomes="durable_peer_call_events")

    def _accepted(self, reader, writer):
        self._counts["connections"] += 1
        if self._closed or len(self._handlers) >= MAX_ACTIVE or self._counts["connections"] > MAX_CONNECTIONS:
            self._counts["connection_limit_denied"] += 1
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        self._writers.add(writer)
        task.add_done_callback(self._finished)

    def _finished(self, task):
        self._handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._counts["handler_failure"] += 1

    async def _send(self, writer, raw):
        writer.write(raw)
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)

    async def _handle(self, reader, writer):
        request_id, dispatched, cancelled = None, False, False
        response = _response(None, code="INVALID_REQUEST")
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), IO_TIMEOUT)
            value = decode_contract(raw, max_bytes=MAX_REQUEST)
            fields(value, {"schema", "schema_version", "token", "request_id", "operation", "arguments"})
            if value["schema"] != "camol.peer_request" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise PeerTransportError("unsupported peer request")
            token = value["token"]
            if not isinstance(token, str) or len(token) != 64 or not hmac.compare_digest(token, self.token):
                response = _response(None, code="AUTH_REQUIRED")
            else:
                if self.token in json.dumps({key: item for key, item in value.items() if key != "token"}, sort_keys=True):
                    raise PeerTransportError("peer capability cannot be copied into tool data")
                require_identifier(value["request_id"], "peer request ID")
                if len(value["request_id"]) > 200:
                    raise PeerTransportError("peer request ID is too long")
                request_id = value["request_id"]
                if self._closed:
                    response = _response(request_id, code="TURN_CLOSED")
                else:
                    # Check before dispatch, then PeerTools checks again before
                    # its durable attempt. A revoked capability cannot reopen.
                    try:
                        self.tools._check()
                    except ValueError:
                        response = _response(request_id, code="TURN_CLOSED")
                    else:
                        dispatched = True
                        if value["operation"] == "handshake":
                            fields(value["arguments"], set())
                            scope = dict(subject=dict(self.tools.caller), turn_number=self.tools.turn)
                            result = dict(scope, scope_digest=canonical_digest(scope),
                                operations=["list", "observe", "inbox", "send"], readiness_proven=False)
                        elif value["operation"] == "send":
                            args = value["arguments"]
                            fields(args, {"target", "body", "kind", "correlation_id", "ttl_seconds"})
                            result = self.tools.send(request_id=request_id, **args)
                        else:
                            result = self.tools.call(value["operation"], value["arguments"], request_id=request_id)
                        response = _response(request_id, result=result)
        except asyncio.CancelledError:
            cancelled = True
            self._counts["interrupted_connections"] += 1
            raise
        except (ValueError, TypeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            response = _response(request_id, code="TOOL_DENIED" if dispatched else "INVALID_REQUEST", dispatched=dispatched)
        except asyncio.TimeoutError:
            response = _response(request_id, code="READ_TIMEOUT")
        except Exception:
            response = _response(request_id, code="UNAVAILABLE", dispatched=dispatched)
        finally:
            try:
                if not cancelled:
                    try:
                        raw = _encode(response, MAX_RESPONSE)
                    except (ValueError, TypeError, RecursionError):
                        response = _response(request_id, code="RESPONSE_TOO_LARGE", dispatched=dispatched)
                        raw = _encode(response, MAX_RESPONSE)
                    self._counts[(response["error"] or {}).get("code", "success")] += 1
                    try:
                        await self._send(writer, raw)
                    except (OSError, asyncio.TimeoutError, ConnectionError):
                        self._counts["response_delivery_unknown"] += 1
            finally:
                writer.close()
                self._writers.discard(writer)
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except (OSError, asyncio.TimeoutError, ConnectionError):
                    pass

    async def close(self):
        self._closed = True
        self.token = ""  # No capability may be issued after closure.
        if self.server is not None:
            self.server.close()
        handlers = tuple(self._handlers)
        for writer in tuple(self._writers):
            writer.close()
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
        if self.directory is not None:
            if self.directory.is_symlink() or _identity(self.directory) != self._directory_identity:
                raise PeerTransportError("peer endpoint directory changed; refusing cleanup")
            if self.path.exists() or self.path.is_symlink():
                if not stat.S_ISSOCK(self.path.lstat().st_mode) or _identity(self.path) != self._socket_identity:
                    raise PeerTransportError("peer endpoint socket changed; refusing cleanup")
                self.path.unlink()
            self.directory.rmdir()
            self.directory = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.close()


def request(path, token, operation, arguments, *, request_id):
    """Synchronous relay primitive; call off the endpoint's owner event loop.

    No automatic retries. A transport exception does not prove absence of effects;
    the caller must retain the same logical request ID/content when retrying.
    """
    endpoint = Path(path)
    info, parent = endpoint.lstat(), endpoint.parent.lstat()
    if (not endpoint.is_absolute() or endpoint.resolve() != endpoint or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
        raise PeerTransportError("peer endpoint must be an exact owner-private Unix socket")
    raw = _encode(dict(schema="camol.peer_request", schema_version=1, token=token,
        request_id=request_id, operation=operation, arguments=arguments), MAX_REQUEST)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(IO_TIMEOUT)
        connection.connect(os.fspath(path))
        connection.sendall(raw)
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(min(8192, MAX_RESPONSE + 1 - len(data)))
            if not chunk:
                raise PeerTransportError("peer response was lost; effect is unknown")
            data.extend(chunk)
            if len(data) > MAX_RESPONSE:
                raise PeerTransportError("peer response exceeds its bound; effect is unknown")
    response = decode_contract(data, max_bytes=MAX_RESPONSE)
    fields(response, {"schema", "schema_version", "request_id", "result", "error"})
    if (response["schema"] != "camol.peer_response" or type(response["schema_version"]) is not int
            or response["schema_version"] != 1 or response["request_id"] not in (None, request_id)):
        raise PeerTransportError("peer response identity differs; effect is unknown")
    error = response["error"]
    if error is None:
        if response["request_id"] != request_id or not isinstance(response["result"], dict):
            raise PeerTransportError("peer success is unbound; effect is unknown")
    else:
        fields(error, {"code", "effect"})
        if (response["result"] is not None or not isinstance(error["code"], str) or error["code"] not in ERROR_CODES
                or not isinstance(error["effect"], str) or error["effect"] not in {"unknown", "not_dispatched"}
                or (response["request_id"] is None and error["effect"] != "not_dispatched")):
            raise PeerTransportError("peer failure is malformed; effect is unknown")
    return response
