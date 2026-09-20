"""Explicit TLS transport for enrolled evidence batches, not remote execution."""

import asyncio
import hashlib
import ipaddress
import math
import os
import re
import socket
import ssl
import stat
import struct
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from .json_contracts import decode_contract
from .schema import canonical_digest, require_digest
from .worker_delivery import DeliveryError, FRAME_BYTES, _bytes, _fields


ACK_BYTES = 4096
ERROR = _bytes(dict(schema="camol.worker_transport_error", schema_version=1, code="REJECTED_OR_UNAVAILABLE"))


class WorkerTransportError(DeliveryError):
    def __init__(self, code, *, outcome="not_sent"):
        super().__init__("worker evidence transport: " + code)
        self.code, self.outcome = code, outcome


def _address(value):
    if not isinstance(value, str) or "%" in value:
        raise WorkerTransportError("LITERAL_IP_REQUIRED")
    try:
        return ipaddress.ip_address(value)
    except ValueError as error:
        raise WorkerTransportError("LITERAL_IP_REQUIRED") from error


def _seconds(value):
    if type(value) not in {float, int} or not math.isfinite(value) or not .1 <= value <= 60:
        raise WorkerTransportError("INVALID_TIMEOUT")
    return float(value)


def _read_tls_file(path, *, private=False, maximum=1 << 20):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise WorkerTransportError("UNSAFE_TLS_FILE")
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum
                or before.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
                or before.st_mode & (0o077 if private else 0o022)):
            raise WorkerTransportError("UNSAFE_TLS_FILE")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(maximum + 1)
        after, current = os.fstat(descriptor), path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid", "st_nlink", "st_mtime_ns", "st_ctime_ns")
        if len(raw) != before.st_size or len(raw) > maximum or any(getattr(before, field) != getattr(after, field) or getattr(before, field) != getattr(current, field) for field in fields):
            raise WorkerTransportError("CHANGED_TLS_FILE")
        if not private and b"PRIVATE KEY" in raw:
            raise WorkerTransportError("PRIVATE_KEY_IN_PUBLIC_CERTIFICATE")
        return raw
    except OSError as error:
        raise WorkerTransportError("TLS_FILE_UNAVAILABLE") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def certificate_digest(pem):
    try:
        match = re.search(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
        if match is None:
            raise ValueError("no certificate")
        der = ssl.PEM_cert_to_DER_cert(match.group().decode("ascii"))
        return "sha256:" + hashlib.sha256(der).hexdigest()
    except (ValueError, UnicodeError) as error:
        raise WorkerTransportError("INVALID_CERTIFICATE") from error


def _context(protocol):
    if not ssl.HAS_TLSv1_3:
        raise WorkerTransportError("TLS13_RUNTIME_REQUIRED")
    context = ssl.SSLContext(protocol)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.keylog_filename = None  # Never honor ambient TLS key logging.
    context.options |= ssl.OP_NO_COMPRESSION
    return context


def server_context(certificate, private_key):
    cert = _read_tls_file(certificate)
    key = _read_tls_file(private_key, private=True, maximum=65536)
    pin = certificate_digest(cert)
    try:
        context = _context(ssl.PROTOCOL_TLS_SERVER)
        context.num_tickets = 0
        # SSLContext requires paths. Load stable private snapshots, not paths
        # that can change after validation. Never let OpenSSL prompt for a key.
        with tempfile.TemporaryDirectory(prefix="camol-tls-") as temporary:
            root = Path(temporary).resolve()
            for name, content in (("cert", cert), ("key", key)):
                fd = os.open(str(root / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
            context.load_cert_chain(str(root / "cert"), str(root / "key"), password=lambda: "")
        return context, pin
    except WorkerTransportError:
        raise
    except (OSError, ValueError) as error:
        raise WorkerTransportError("TLS_CONFIGURATION_DENIED") from error


def endpoint(value):
    _fields(value, {"schema", "schema_version", "address", "port", "server_name", "ca_file", "ca_digest", "certificate_digest", "scope", "timeout_seconds"})
    if value["schema"] != "camol.worker_tls_endpoint" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise WorkerTransportError("INVALID_ENDPOINT")
    _address(value["address"])
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise WorkerTransportError("INVALID_ENDPOINT_PORT")
    if not isinstance(value["server_name"], str) or not re.fullmatch(r"[A-Za-z0-9:][A-Za-z0-9.:-]{0,252}", value["server_name"]):
        raise WorkerTransportError("INVALID_CERTIFICATE_NAME")
    if not isinstance(value["ca_file"], str) or not Path(value["ca_file"]).is_absolute():
        raise WorkerTransportError("ABSOLUTE_CA_PATH_REQUIRED")
    for field in ("scope", "ca_digest", "certificate_digest"):
        require_digest(value[field], field)
    _seconds(value["timeout_seconds"])
    return deepcopy(value)


def _client_context(profile):
    ca = _read_tls_file(profile["ca_file"])
    if "sha256:" + hashlib.sha256(ca).hexdigest() != profile["ca_digest"]:
        raise WorkerTransportError("CA_PIN_CHANGED")
    try:
        context = _context(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca.decode("ascii"))
        return context
    except WorkerTransportError:
        raise
    except (ValueError, OSError, UnicodeError) as error:
        raise WorkerTransportError("TLS_CONFIGURATION_DENIED") from error


async def _read_frame(reader, maximum):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 2 <= size <= maximum:
        raise WorkerTransportError("FRAME_LIMIT")
    return await reader.readexactly(size)


def _frame(raw, maximum):
    if not isinstance(raw, bytes) or not 2 <= len(raw) <= maximum:
        raise WorkerTransportError("FRAME_LIMIT")
    return struct.pack("!I", len(raw)) + raw


async def _close(writer):
    if writer is None:
        return
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), .25)
    except asyncio.CancelledError:
        writer.transport.abort()
        raise
    except Exception:
        writer.transport.abort()


class WorkerTLSClient:
    def __init__(self, profile):
        self.profile = endpoint(profile)
        self.last_attempt = None
        self._busy = False

    async def exchange(self, raw, *, allow_network=False):
        if self._busy:
            raise WorkerTransportError("CLIENT_BUSY")
        started, writer = time.monotonic_ns(), None
        attempt = dict(endpoint_digest=None, scope=None, outcome="not_sent",
                       elapsed_ms=None, request_bytes=0, response_bytes=0, tls_version=None,
                       peer_certificate_digest=None, provider_tokens=None, provider_cost=None,
                       basis="local_plaintext_protocol_measurement_not_network_traffic_or_billing")
        self.last_attempt = attempt
        try:
            if allow_network is not True:
                raise WorkerTransportError("EXPLICIT_NETWORK_APPROVAL_REQUIRED")
            profile = endpoint(self.profile)
            attempt.update(endpoint_digest=canonical_digest(profile), scope=profile["scope"])
            raw = bytes(raw) if isinstance(raw, bytearray) else raw
            wire = _frame(raw, FRAME_BYTES)
            message = decode_contract(raw, max_bytes=FRAME_BYTES)
            if not isinstance(message, dict) or message.get("scope") != profile["scope"]:
                raise WorkerTransportError("FOREIGN_STREAM")
            context = _client_context(profile)
        except Exception:
            attempt["elapsed_ms"] = max(0, (time.monotonic_ns() - started) // 1000000)
            raise
        self._busy = True
        async def perform():
            nonlocal writer
            reader, writer = await asyncio.open_connection(profile["address"], profile["port"], ssl=context,
                server_hostname=profile["server_name"], ssl_handshake_timeout=min(10, profile["timeout_seconds"]), limit=ACK_BYTES)
            tls = writer.get_extra_info("ssl_object")
            observed = "sha256:" + hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() if tls else None
            if observed != profile["certificate_digest"]:
                raise WorkerTransportError("CERTIFICATE_PIN_CHANGED")
            attempt.update(tls_version=tls.version(), peer_certificate_digest=observed, outcome="unknown", request_bytes=len(wire))
            writer.write(wire)
            await writer.drain()
            reply = await _read_frame(reader, ACK_BYTES)
            attempt["response_bytes"] = len(reply) + 4
            if await reader.read(1):
                raise WorkerTransportError("EXTRA_RESPONSE_DATA", outcome="unknown")
            response = decode_contract(reply, max_bytes=ACK_BYTES)
            if isinstance(response, dict) and response.get("schema") == "camol.worker_transport_error":
                raise WorkerTransportError("REJECTED_OR_UNAVAILABLE", outcome="unknown")
            attempt["outcome"] = "receipt_received_unverified"
            return reply
        try:
            return await asyncio.wait_for(perform(), profile["timeout_seconds"])
        except asyncio.CancelledError:
            # Preserve cancellation. last_attempt retains whether bytes may have
            # left; the producer's unacknowledged rows are never removed here.
            raise
        except WorkerTransportError as error:
            error.outcome = attempt["outcome"]
            raise
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
            raise WorkerTransportError("UNAVAILABLE_OR_INVALID", outcome=attempt["outcome"]) from error
        finally:
            try:
                await _close(writer)
            finally:
                attempt["elapsed_ms"] = max(0, (time.monotonic_ns() - started) // 1000000)
                self._busy = False

    async def deliver(self, producer, *, allow_network=False):
        if self._busy:
            raise WorkerTransportError("CLIENT_BUSY")
        self.last_attempt = dict(outcome="not_sent", request_bytes=0, response_bytes=0)
        if producer.role != "producer" or producer.scope != self.profile["scope"]:
            raise WorkerTransportError("FOREIGN_PRODUCER")
        reply = await self.exchange(producer.batch(), allow_network=allow_network)
        try:
            cursor = producer.acknowledge(reply)
        except (DeliveryError, ValueError) as error:
            self.last_attempt["outcome"] = "local_ack_pending_or_invalid"
            raise WorkerTransportError("LOCAL_ACK_PENDING_OR_INVALID", outcome="local_ack_pending_or_invalid") from error
        self.last_attempt["outcome"] = "acknowledged"
        return dict(self.last_attempt, cursor=cursor, execution_authority=False, kernel_promoted=False)


class WorkerTLSServer:
    """Embed on the controller's event loop; off by default, no auto enrollment."""

    def __init__(self, service, *, certificate, private_key, address="127.0.0.1", port=0,
                 allow_non_loopback=False, max_connections=16, timeout=10):
        ip = _address(address)
        if type(allow_non_loopback) is not bool or (not ip.is_loopback and not allow_non_loopback):
            raise WorkerTransportError("NON_LOOPBACK_APPROVAL_REQUIRED")
        if type(port) is not int or not 0 <= port <= 65535 or type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise WorkerTransportError("INVALID_LISTENER_LIMIT")
        if not callable(getattr(service, "receive", None)):
            raise WorkerTransportError("ENROLLED_RECEIVER_REQUIRED")
        self.service, self.address, self.port = service, str(ip), port
        self.max_connections, self.timeout = max_connections, _seconds(timeout)
        self.context, self.certificate_digest = server_context(certificate, private_key)
        self.listener, self._accept_task = None, None
        self._connections = set()
        self.stats = dict(accepted=0, busy=0, completed=0, rejected=0, network_failures=0, cancelled=0)

    async def start(self):
        if self.listener is not None:
            raise WorkerTransportError("LISTENER_ALREADY_STARTED")
        listener = socket.socket(socket.AF_INET6 if _address(self.address).version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.address, self.port))
            listener.listen(self.max_connections)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            raise
        self.listener = listener
        self.port = listener.getsockname()[1]
        self._accept_task = asyncio.create_task(self._accept())
        return self

    async def _accept(self):
        while self.listener is not None:
            try:
                connection, _ = await asyncio.get_running_loop().sock_accept(self.listener)
            except OSError:
                self.stats["network_failures"] += 1
                if self.listener is not None:
                    self.listener.close()
                    self.listener = None
                return
            if len(self._connections) >= self.max_connections:
                self.stats["busy"] += 1
                connection.close()
                await asyncio.sleep(0)
                continue
            connection.setblocking(False)
            self.stats["accepted"] += 1
            task = asyncio.create_task(self._connection(connection))
            self._connections.add(task)
            task.add_done_callback(self._connections.discard)
            await asyncio.sleep(0)

    async def _connection(self, connection):
        writer = None
        async def perform():
            nonlocal writer
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(limit=FRAME_BYTES)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, connection, ssl=self.context,
                ssl_handshake_timeout=min(10, self.timeout))
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            raw = await _read_frame(reader, FRAME_BYTES)
            try:
                header = decode_contract(raw, max_bytes=FRAME_BYTES)
                if not isinstance(header, dict):
                    raise DeliveryError("invalid envelope")
                require_digest(header.get("scope"), "worker TLS scope")
                reply = self.service.receive(header["scope"], raw)
                wire = _frame(reply, ACK_BYTES)
            except Exception:
                self.stats["rejected"] += 1
                wire = _frame(ERROR, ACK_BYTES)
            else:
                self.stats["completed"] += 1
            writer.write(wire)
            await writer.drain()
        try:
            await asyncio.wait_for(perform(), self.timeout)
        except asyncio.CancelledError:
            self.stats["cancelled"] += 1
            raise
        except Exception:
            self.stats["network_failures"] += 1
        finally:
            try:
                await _close(writer)
            finally:
                connection.close()

    async def close(self):
        if self._accept_task is not None:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
            self._accept_task = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        pending = tuple(self._connections)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._connections.clear()

    def status(self):
        return dict(self.stats, address=self.address, port=self.port,
            listening=self.listener is not None and self._accept_task is not None and not self._accept_task.done(),
            in_flight=len(self._connections), certificate_digest=self.certificate_digest,
            counters="this_server_lifetime_only_not_durable_usage", execution_authority=False)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()
