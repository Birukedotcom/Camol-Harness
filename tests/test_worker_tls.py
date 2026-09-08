import asyncio
import copy
import contextlib
import hashlib
import io
import json
import os
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from camol.worker_delivery import DeliveryError, FRAME_BYTES
from camol.worker_tls import WorkerTLSClient, WorkerTLSServer, WorkerTransportError, certificate_digest, endpoint, server_context, _client_context, _context
from tests import test_worker_enrollment as enrollment_fixture


class WorkerTLSTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not ssl.HAS_TLSv1_3:
            raise unittest.SkipTest("real TLS delivery requires a TLS 1.3 capable Python runtime")
        executable = shutil.which("openssl")
        if executable is None:
            raise unittest.SkipTest("OpenSSL CLI required for ephemeral TLS test certificates")
        cls.temporary = tempfile.TemporaryDirectory(prefix="camol-tls-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.cert, cls.key = cls.root / "cert.pem", cls.root / "key.pem"
        config = cls.root / "openssl.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                          "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
                          "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
                          "extendedKeyUsage=serverAuth\n")
        result = subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                 "-config", str(config), "-keyout", str(cls.key), "-out", str(cls.cert)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise AssertionError("ephemeral test certificate generation failed")
        cls.key.chmod(0o600)
        cls.cert.chmod(0o600)

    async def asyncSetUp(self):
        self.fixture = enrollment_fixture.WorkerEnrollmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.value = self.fixture.approved()
        self.producer = self.fixture.producer(self.value)
        self.producer.queue("checkpoint", {"message": "TLS fixture progress"}, occurred_at=self.fixture.orch._now())
        self.server = WorkerTLSServer(self.fixture.service, certificate=self.cert, private_key=self.key, timeout=2)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.profile = dict(schema="camol.worker_tls_endpoint", schema_version=1, address="127.0.0.1",
            port=self.server.port, server_name="localhost", ca_file=str(self.cert),
            ca_digest="sha256:" + hashlib.sha256(self.cert.read_bytes()).hexdigest(),
            certificate_digest=certificate_digest(self.cert.read_bytes()), scope=self.value["scope"], timeout_seconds=2)

    async def settle(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail("TLS fixture state did not settle")

    async def test_real_tls_authenticated_receipt_and_restart_do_not_promote_worker_claims(self):
        before = self.fixture.orch.state(self.fixture.fixture.run_id)
        client = WorkerTLSClient(self.profile)
        report = await client.deliver(self.producer, allow_network=True)
        self.assertEqual(report["cursor"], 1)
        self.assertEqual(report["outcome"], "acknowledged")
        self.assertEqual(report["tls_version"], "TLSv1.3")
        self.assertIsNone(report["provider_tokens"])
        self.assertIsNone(report["provider_cost"])
        self.assertFalse(report["kernel_promoted"])
        self.assertGreater(report["request_bytes"], 4)
        self.assertEqual(self.fixture.orch.state(self.fixture.fixture.run_id), before)
        await self.server.close()
        await self.server.start()
        restarted = WorkerTLSClient(dict(self.profile, port=self.server.port))
        self.assertEqual((await restarted.deliver(self.producer, allow_network=True))["cursor"], 1)
        self.assertEqual(self.producer.inspect()["unacknowledged"], 0)

    async def test_opt_in_scope_ca_pin_hostname_and_leaf_pin_fail_before_delivery(self):
        receive = Mock(wraps=self.fixture.service.receive)
        self.server.service = Mock(receive=receive)
        for changes, allow in (({}, False), ({"scope": "sha256:" + "f" * 64}, True),
                ({"ca_digest": "sha256:" + "f" * 64}, True), ({"server_name": "wrong.invalid"}, True),
                ({"certificate_digest": "sha256:" + "f" * 64}, True)):
            with self.subTest(changes=changes):
                client = WorkerTLSClient(dict(self.profile, **changes))
                with self.assertRaises(DeliveryError):
                    await client.deliver(self.producer, allow_network=allow)
                self.assertEqual(client.last_attempt["outcome"], "not_sent")
        receive.assert_not_called()
        self.assertEqual(self.producer.inspect()["unacknowledged"], 1)

    async def test_revocation_wrong_mac_and_service_exceptions_preserve_outbox_and_hide_details(self):
        client = WorkerTLSClient(self.profile)
        message = json.loads(self.producer.batch())
        message["mac"] = "sha256:" + "f" * 64
        with self.assertRaises(WorkerTransportError) as failed:
            await client.exchange(json.dumps(message).encode(), allow_network=True)
        self.assertEqual(failed.exception.code, "REJECTED_OR_UNAVAILABLE")
        self.fixture.service.revoke(self.value["scope"], by="test-owner", reason="fixture revocation")
        with self.assertRaises(WorkerTransportError):
            await client.deliver(self.producer, allow_network=True)
        self.server.service = Mock(receive=Mock(side_effect=RuntimeError("private exception sentinel")))
        with self.assertRaises(WorkerTransportError) as failed:
            await client.deliver(self.producer, allow_network=True)
        self.assertNotIn("sentinel", str(failed.exception))
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)

    async def test_lost_ack_is_recoverable_without_duplicate_evidence(self):
        original = self.fixture.service.receive
        received = []
        def lose_reply(scope, raw):
            received.append(original(scope, raw))
            raise OSError("fixture loses reply after spool commit")
        self.server.service = Mock(receive=lose_reply)
        client = WorkerTLSClient(self.profile)
        with self.assertRaises(WorkerTransportError):
            await client.deliver(self.producer, allow_network=True)
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        self.server.service = self.fixture.service
        report = await client.deliver(self.producer, allow_network=True)
        self.assertEqual(report["cursor"], 1)
        self.assertEqual(original(self.value["scope"], self.producer.batch()), received[0])

    async def test_cancel_slow_response_keeps_pending_and_clears_client_busy(self):
        arrived = asyncio.Event()
        stopped = asyncio.Event()
        async def slow(reader, writer):
            try:
                await reader.read(65536)
                arrived.set()
                await stopped.wait()
            finally:
                writer.close()
                await writer.wait_closed()
        listener = await asyncio.start_server(slow, "127.0.0.1", 0, ssl=self.server.context)
        client = WorkerTLSClient(dict(self.profile, port=listener.sockets[0].getsockname()[1]))
        task = asyncio.create_task(client.deliver(self.producer, allow_network=True))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            with self.assertRaises(WorkerTransportError) as busy:
                await client.deliver(self.producer, allow_network=True)
            self.assertEqual(busy.exception.code, "CLIENT_BUSY")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(client._busy)
            self.assertEqual(client.last_attempt["outcome"], "unknown")
            self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        finally:
            stopped.set()
            listener.close()
            await listener.wait_closed()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_pre_handshake_capacity_slow_clients_and_shutdown(self):
        await self.server.close()
        self.server.max_connections = 1
        await self.server.start()
        reader1, writer1 = await asyncio.open_connection("127.0.0.1", self.server.port)
        reader2, writer2 = None, None
        try:
            await self.settle(lambda: self.server.status()["in_flight"] == 1)
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", self.server.port)
            self.assertEqual(await asyncio.wait_for(reader2.read(1), 1), b"")
            self.assertEqual(self.server.status()["busy"], 1)
            await asyncio.wait_for(self.server.close(), 1)
            self.assertEqual(self.server.status()["in_flight"], 0)
            self.assertFalse(self.server.status()["listening"])
        finally:
            for writer in (writer1, writer2):
                if writer:
                    writer.close()
                    await writer.wait_closed()

    async def test_oversized_frame_is_rejected_without_service_call(self):
        receive = Mock(wraps=self.fixture.service.receive)
        self.server.service = Mock(receive=receive)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port,
            ssl=_client_context(self.profile), server_hostname="localhost")
        try:
            writer.write(struct.pack("!I", FRAME_BYTES + 1))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(1), 2), b"")
            receive.assert_not_called()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_invalid_ack_and_local_commit_failure_never_advance_producer_cursor(self):
        original = self.fixture.service.receive
        def forge(scope, raw):
            message = json.loads(original(scope, raw))
            message["mac"] = "f" * 64
            return json.dumps(message).encode()
        self.server.service = Mock(receive=forge)
        client = WorkerTLSClient(self.profile)
        with self.assertRaises(WorkerTransportError) as failure:
            await client.deliver(self.producer, allow_network=True)
        self.assertEqual(failure.exception.code, "LOCAL_ACK_PENDING_OR_INVALID")
        self.assertEqual(failure.exception.outcome, client.last_attempt["outcome"])
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        self.server.service = self.fixture.service
        with patch.object(self.producer, "acknowledge", side_effect=DeliveryError("fixture disk busy")):
            with self.assertRaises(WorkerTransportError):
                await client.deliver(self.producer, allow_network=True)
        self.assertEqual(self.producer.inspect()["acknowledged"], 0)
        self.assertEqual((await client.deliver(self.producer, allow_network=True))["cursor"], 1)

    async def test_invalid_extra_and_truncated_responses_never_acknowledge(self):
        for wire in (struct.pack("!I", 4097), struct.pack("!I", 9) + b"{}",
                     struct.pack("!I", 2) + b"{}extra", struct.pack("!I", 2) + b"xx"):
            finished = asyncio.Event()
            async def invalid(reader, writer):
                try:
                    await reader.read(65536)
                    writer.write(wire)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    finished.set()
            listener = await asyncio.start_server(invalid, "127.0.0.1", 0, ssl=self.server.context)
            try:
                client = WorkerTLSClient(dict(self.profile, port=listener.sockets[0].getsockname()[1]))
                with self.assertRaises(WorkerTransportError):
                    await client.deliver(self.producer, allow_network=True)
                self.assertEqual(client.last_attempt["outcome"], "unknown")
                self.assertEqual(self.producer.inspect()["acknowledged"], 0)
                await asyncio.wait_for(finished.wait(), 2)
            finally:
                listener.close()
                await listener.wait_closed()

    async def test_timeout_retains_unknown_delivery_and_client_is_reusable(self):
        finished = asyncio.Event()
        async def stall(reader, writer):
            try:
                await reader.read(65536)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        listener = await asyncio.start_server(stall, "127.0.0.1", 0, ssl=self.server.context)
        client = WorkerTLSClient(dict(self.profile, timeout_seconds=.2, port=listener.sockets[0].getsockname()[1]))
        try:
            with self.assertRaises(WorkerTransportError) as failure:
                await client.deliver(self.producer, allow_network=True)
            self.assertEqual(failure.exception.outcome, "unknown")
            self.assertFalse(client._busy)
            self.assertEqual(self.producer.inspect()["acknowledged"], 0)
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            listener.close()
            await listener.wait_closed()
        client.profile = copy.deepcopy(self.profile)
        self.assertEqual((await client.deliver(self.producer, allow_network=True))["cursor"], 1)

    async def test_cli_flush_uses_explicit_profile_and_preserves_private_key(self):
        root = self.fixture.fixture.state_dir
        binding_path, profile_path = root / "binding.json", root / "endpoint.json"
        binding_path.write_text(json.dumps(self.fixture.stream))
        profile_path.write_text(json.dumps(self.profile))
        key_path = self.fixture.service._directory(self.value["scope"]) / "key"
        child = await asyncio.create_subprocess_exec(sys.executable, "-m", "camol", "worker-delivery", "flush",
            "--root", str(root / "test-producer"), "--binding", str(binding_path), "--key-file", str(key_path),
            "--role", "producer", "--target", str(profile_path), "--allow-network",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        self.assertEqual(child.returncode, 0, stderr.decode()[:500])
        self.assertEqual(json.loads(stdout)["cursor"], 1)
        self.assertNotIn(key_path.read_bytes().hex().encode(), stdout + stderr)

    async def test_configuration_boundaries_and_no_ambient_keylog(self):
        for changes in ({"address": "localhost"}, {"address": "127.0.0.1%zone"}, {"port": True},
                        {"timeout_seconds": float("nan")}, {"extra": True}, {"ca_file": "relative.pem"}):
            with self.assertRaises((DeliveryError, ValueError)):
                endpoint(dict(self.profile, **changes))
        with self.assertRaises(WorkerTransportError):
            WorkerTLSServer(self.fixture.service, certificate=self.cert, private_key=self.key, address="0.0.0.0")
        keylog = self.fixture.fixture.state_dir / "tls-keylog"
        with patch.dict(os.environ, SSLKEYLOGFILE=str(keylog), HTTPS_PROXY="http://127.0.0.1:1"):
            report = await WorkerTLSClient(self.profile).deliver(self.producer, allow_network=True)
        self.assertEqual(report["outcome"], "acknowledged")
        self.assertFalse(keylog.exists())
        client = WorkerTLSClient(self.profile)
        await client.deliver(self.producer, allow_network=True)
        with self.assertRaises(WorkerTransportError):
            await client.deliver(self.producer)
        self.assertEqual(client.last_attempt["outcome"], "not_sent")

    async def test_unsafe_tls_material_never_opens_a_connection(self):
        root = self.fixture.fixture.state_dir
        linked = root / "linked-cert"
        linked.symlink_to(self.cert)
        client = WorkerTLSClient(dict(self.profile, ca_file=str(linked)))
        with patch("camol.worker_tls.asyncio.open_connection") as connect:
            with self.assertRaises(WorkerTransportError):
                await client.deliver(self.producer, allow_network=True)
            connect.assert_not_called()
        private = root / "public-key"
        private.write_bytes(self.key.read_bytes())
        private.chmod(0o644)
        with self.assertRaises(WorkerTransportError):
            server_context(self.cert, private)
        cert_with_key = root / "mixed-pem"
        cert_with_key.write_bytes(self.cert.read_bytes() + self.key.read_bytes())
        cert_with_key.chmod(0o600)
        with self.assertRaises(WorkerTransportError):
            server_context(cert_with_key, self.key)
        fifo = root / "cert-fifo"
        os.mkfifo(str(fifo), 0o600)
        with self.assertRaises(WorkerTransportError):
            server_context(fifo, self.key)


class WorkerTLSCapabilityTests(unittest.TestCase):
    def test_unsupported_tls_runtime_fails_explicitly_without_creating_context(self):
        with patch("camol.worker_tls.ssl.HAS_TLSv1_3", False), patch("camol.worker_tls.ssl.SSLContext") as context:
            with self.assertRaises(WorkerTransportError) as failure:
                _context(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(failure.exception.code, "TLS13_RUNTIME_REQUIRED")
        context.assert_not_called()

    def test_cli_network_approval_precedes_any_spool_or_key_read(self):
        from camol.cli import main
        with patch("camol.worker_delivery.read_enrollment_key") as key_read, \
                patch("camol.worker_delivery.WorkerDelivery") as delivery, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["worker-delivery", "flush", "--root", "/does-not-exist", "--binding", "/does-not-exist",
                         "--key-file", "/does-not-exist", "--role", "producer"])
        self.assertEqual(code, 2)
        key_read.assert_not_called()
        delivery.assert_not_called()
