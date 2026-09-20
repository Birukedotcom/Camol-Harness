import http.server
import json
import socket
import socketserver
import tempfile
import threading
import time
import unittest
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from camol.connections import (ConnectionError, ConnectionRegistry, _record,
                              observation_label, read_local_catalog)


@contextmanager
def catalog_server(mode):
    paths, closed = [], threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            try:
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/forbidden-followup")
                    self.end_headers()
                elif mode == "headers":
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Trickle: ")
                    for _ in range(50):
                        self.connection.sendall(b"x")
                        time.sleep(.02)
                elif mode == "body":
                    self.send_response(200)
                    self.end_headers()
                    for _ in range(50):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(.02)
                else:
                    body = json.dumps({"data": []}).encode() if mode == "valid" else b"x" * 2048
                    self.send_response(200)
                    if mode != "unannounced-large":
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                closed.set()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port), paths, closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@contextmanager
def stalled_tls_server():
    closed = threading.Event()
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(.05)
            deadline = time.monotonic() + 1
            try:
                # Read the client hello but never answer it. The fixture itself
                # is bounded even if client cleanup regresses.
                while time.monotonic() < deadline:
                    try:
                        if not self.request.recv(8192):
                            break
                    except socket.timeout:
                        pass
            finally:
                closed.set()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield "https://127.0.0.1:{}".format(server.server_address[1]), closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class ConnectionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = ConnectionRegistry(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_legacy_ready_records_keep_schema_but_present_layer_and_observation_age(self):
        now = datetime.now(timezone.utc)
        for identity, layer in (("claude-cli", "auth-observed"), ("openai-api-env", "key-presence-observed"),
                                ("local-openai", "catalog-observed")):
            record = _record(identity, "fixture", "fixture", status="ready")
            record["observed_at"] = (now - timedelta(days=2)).isoformat()
            self.registry.save([record])
            self.assertEqual(self.registry.load(), [record])
            self.assertEqual(observation_label(record, now=now), layer + " 2d ago")
            record["observed_at"] = (now + timedelta(days=1)).isoformat()
            self.assertIn("future-dated; unverified", observation_label(record, now=now))
            record["observed_at"] = "invalid historical timestamp"
            self.assertIn("age unknown", observation_label(record, now=now))

    def test_selected_login_refresh_does_not_probe_other_provider_or_loopback(self):
        old = _record("codex-cli", "openai", "cli", status="ready")
        self.registry.save([old])
        new = _record("claude-cli", "anthropic", "cli", status="ready")
        self.registry.probe_claude = Mock(return_value=new)
        self.registry.probe_codex = Mock(side_effect=AssertionError("unselected Codex"))
        self.registry.probe_local = Mock(side_effect=AssertionError("ambient loopback"))
        self.assertEqual(self.registry.refresh("claude"), [new, old])
        self.registry.probe_claude.assert_called_once()
        self.registry.probe_codex.assert_not_called()
        self.registry.probe_local.assert_not_called()

    def test_empty_catalog_is_observation_not_inference_and_ignores_proxy_configuration(self):
        with catalog_server("valid") as (endpoint, paths, _):
            with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                record = self.registry.probe_local(endpoint)
        self.assertEqual(paths, ["/models"])
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["capabilities"], [])
        self.assertIn("0 model(s)", record["detail"])
        self.assertIn("unverified", record["detail"])

    def test_catalog_redirect_is_not_followed_even_to_another_loopback_path(self):
        with catalog_server("redirect") as (endpoint, paths, _):
            with self.assertRaisesRegex(ConnectionError, "redirects are refused"):
                read_local_catalog(endpoint)
        self.assertEqual(paths, ["/models"])

    def test_catalog_body_and_declared_size_are_bounded(self):
        for mode in ("declared-large", "unannounced-large"):
            with self.subTest(mode=mode), catalog_server(mode) as (endpoint, _, _):
                with self.assertRaisesRegex(ConnectionError, "response size"):
                    read_local_catalog(endpoint, maximum=1024)

    def test_boolean_timeout_is_not_a_numeric_deadline(self):
        with catalog_server("valid") as (endpoint, paths, _):
            with self.assertRaisesRegex(ConnectionError, "bounds are invalid"):
                read_local_catalog(endpoint, timeout=True)
        self.assertEqual(paths, [])

    def test_absolute_deadline_interrupts_headers_and_body_and_closes_owned_transport(self):
        for mode in ("headers", "body", "body"):
            with self.subTest(mode=mode), catalog_server(mode) as (endpoint, _, closed):
                started = time.monotonic()
                with self.assertRaises((ConnectionError, OSError, http.client.HTTPException)):
                    read_local_catalog(endpoint, timeout=.15)
                self.assertLess(time.monotonic() - started, .8)
                self.assertTrue(closed.wait(.8), "server did not observe the connection ending")
        self.assertFalse(any(thread.name == "camol-local-catalog-deadline" for thread in threading.enumerate()))

    def test_cli_probe_deadline_and_output_bound_reap_the_owned_subprocess(self):
        for program, timeout in (("import time; time.sleep(2)", .15), ("print('x' * (300 << 10))", 2)):
            started = time.monotonic()
            with self.assertRaisesRegex(ConnectionError, "deadline|output bound"):
                self.registry._run([sys.executable, "-c", program], timeout=timeout)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_cli_timeout_kills_retained_pipe_descendant_after_leader_exits(self):
        sentinel = Path(self.temporary.name) / "descendant-leaked"
        leader_done = Path(self.temporary.name) / "leader-finished"
        child = "import time,pathlib; time.sleep(.8); pathlib.Path({!r}).write_text('leaked')".format(str(sentinel))
        program = "import subprocess,sys,pathlib; subprocess.Popen([sys.executable,'-c',{!r}]); pathlib.Path({!r}).write_text('finished')".format(child, str(leader_done))
        with self.assertRaisesRegex(ConnectionError, "deadline"):
            self.registry._run([sys.executable, "-c", program], timeout=.2)
        self.assertTrue(leader_done.is_file())
        time.sleep(.9)
        self.assertFalse(sentinel.exists(), "same-group child survived the deadline after its leader exited")

    def test_tls_handshake_is_inside_absolute_deadline_and_cancellation_scope(self):
        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation), stalled_tls_server() as (endpoint, closed):
                cancelled = threading.Event()
                timer = threading.Timer(.1, cancelled.set) if cancellation else None
                if timer:
                    timer.start()
                try:
                    started = time.monotonic()
                    with self.assertRaises((ConnectionError, OSError, http.client.HTTPException)):
                        read_local_catalog(endpoint, timeout=1 if cancellation else .15, cancel_event=cancelled)
                    self.assertLess(time.monotonic() - started, .8)
                    self.assertTrue(closed.wait(.8), "stalled TLS peer did not observe connection cleanup")
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()

    def test_explicit_refresh_cancellation_closes_http_and_prevents_later_probes(self):
        cancelled = threading.Event()
        with catalog_server("body") as (endpoint, _, closed):
            timer = threading.Timer(.1, cancelled.set)
            timer.start()
            try:
                started = time.monotonic()
                with self.assertRaises((ConnectionError, OSError, http.client.HTTPException)):
                    read_local_catalog(endpoint, cancel_event=cancelled)
                self.assertLess(time.monotonic() - started, .8)
                self.assertTrue(closed.wait(.8))
            finally:
                timer.cancel()
                timer.join()
        self.registry.probe_claude = Mock(side_effect=AssertionError("cancelled refresh executed"))
        with self.assertRaisesRegex(ConnectionError, "cancelled"):
            self.registry.refresh(cancel_event=cancelled)
        self.registry.probe_claude.assert_not_called()

    def test_cancellation_during_the_last_probe_does_not_replace_cached_observations(self):
        old = _record("local-openai", "local", "openai_compatible", status="ready")
        self.registry.save([old])
        for target in ("local", "all"):
            cancelled = threading.Event()
            def local_probe():
                cancelled.set()
                return _record("local-openai", "local", "openai_compatible", status="unavailable")
            self.registry.probe_local = local_probe
            self.registry.probe_claude = Mock(return_value=_record("claude-cli", "anthropic", "cli", status="unavailable"))
            self.registry.probe_codex = Mock(return_value=_record("codex-cli", "openai", "cli", status="unavailable"))
            self.registry.probe_openai_environment = Mock(return_value=_record("openai-api-env", "openai", "api", status="auth_required"))
            with self.assertRaisesRegex(ConnectionError, "cancelled"):
                self.registry.refresh(target, cancel_event=cancelled)
            self.assertEqual(self.registry.load(), [old])


if __name__ == "__main__":
    unittest.main()
