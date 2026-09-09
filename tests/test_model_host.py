"""Real tiny subprocess/loopback lifecycle fixtures, never a model runtime."""

import hashlib
import io
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from camol.model_host import LlamaCppModelHost, ModelHostUnload, ModelHostPlan, ModelHostReadbackError, _CHILDREN, _http_json
from camol.models import DownloadFile, DownloadPlan, ModelError, ModelStore


FIXTURE = r'''
import json, os, time
stages = {}
def stage(name):
    stages[name] = time.time()
    fd = os.open('fixture-stage.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'pid': os.getpid(), 'last_phase': name, 'stages': stages}, handle)
stage('python_started')
import http.server, signal, socketserver, sys
from pathlib import Path
stage('imports_ready')
args = sys.argv[1:]
def arg(name): return args[args.index(name) + 1]
mode = Path(arg('--model')).read_bytes()[4:].decode()
Path('fixture-invocation.json').write_text(json.dumps({'argv': args, 'env': dict(os.environ)}))
if mode == 'crash': raise SystemExit(17)
if mode == 'slow': time.sleep(3)
if mode == 'ignoreterm': signal.signal(signal.SIGTERM, signal.SIG_IGN)
key = Path(arg('--api-key-file')).read_text()
alias, model_path = arg('--alias'), arg('--model')
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path != '/health' and self.headers.get('Authorization') != 'Bearer ' + key:
            self.send_response(401); self.end_headers(); return
        if self.path == '/health': data = {'status': 'ok'}
        elif self.path == '/v1/models': data = {'data': [{'id': 'wrong' if mode == 'mismatch' else alias}]}
        elif self.path == '/props': data = {'model_path': model_path, 'total_slots': 1, 'is_sleeping': False}
        else: self.send_response(404); self.end_headers(); return
        body = json.dumps(data).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass
class FixtureServer(http.server.HTTPServer):
    def server_bind(self):
        stage('before_bind')
        # This numeric-loopback fixture has no hostname contract. HTTPServer's
        # default server_bind performs getfqdn and can stall on external DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]
        stage('bound')
    def server_activate(self):
        super().server_activate()
        stage('listening')
stage('before_server_init')
server = FixtureServer(('127.0.0.1', int(arg('--port'))), Handler)
stage('server_ready')
server.serve_forever()
'''


class MemoryTransport:
    def __init__(self, payload):
        self.payload = payload

    def open(self, file, *, offset, **kwargs):
        payload = self.payload[offset:]
        class Stream(io.BytesIO):
            status = 206 if offset else 200
            source_origin = "https://fixtures.invalid"
            headers = {"content-length": str(len(payload))}
        result = Stream(payload)
        if offset:
            result.headers["content-range"] = "bytes {}-{}/{}".format(offset, len(self.payload) - 1, len(self.payload))
        return result


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ModelHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.host_root = self.base / "hosts"
        self.models = ModelStore(self.base / "models")
        self.host = LlamaCppModelHost(self.host_root)
        self.executable = self.base / "fixture-server"
        self.executable.write_text("#!" + sys.executable + "\n" + FIXTURE)
        self.executable.chmod(0o700)
        self.plans = []

    def tearDown(self):
        for plan in self.plans:
            try:
                operation = self.host._operation(plan.digest())
                if operation and self.host._helper_alive(plan.digest()):
                    request = self.host.propose_unload(plan.digest(), "cleanup-" + plan.plan_id)
                    self.host.unload(request, "owner", approve_digest=request.digest())
            except (ModelError, OSError):
                pass
        for child in _CHILDREN:
            if child.poll() is not None:
                child.wait(timeout=2)
        self.host.close()
        self.models.close()
        self.temp.cleanup()

    def plan(self, mode="normal", **kwargs):
        payload = b"GGUF" + mode.encode()
        file = DownloadFile("fixture.gguf", "https://fixtures.invalid/model", "sha256:" + hashlib.sha256(payload).hexdigest(), len(payload))
        download = DownloadPlan("download-" + mode, "fixture/" + mode, "pinned-fixture", "owner", (file,),
                                ("https://fixtures.invalid",), len(payload), len(payload))
        self.models.prepare(download)
        self.models.approve(download.digest(), "owner")
        self.models.download(download.digest(), "owner", transport=MemoryTransport(payload))
        values = dict(plan_id="host-" + str(len(self.plans)), owner="owner", model_store_root=str(self.models.root),
                      download_plan_digest=download.digest(), logical_path=file.path, artifact_digest=file.digest,
                      artifact_size_bytes=file.size_bytes, executable=str(self.executable),
                      executable_digest="sha256:" + hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                      port=free_port(), context_tokens=512, load_timeout_seconds=5, stop_timeout_seconds=1, lifetime_seconds=30)
        values.update(kwargs)
        plan = ModelHostPlan(**values)
        self.plans.append(plan)
        return plan

    def approved(self, plan):
        self.host.prepare(plan)
        self.host.approve(plan.digest(), "owner")

    def fixture_stage(self, plan):
        # Only fixed diagnostic fields from our tiny fixture, never its argv,
        # environment, credentials or arbitrary output. Bound the read even on
        # a failed/cold startup where the stage file may not exist yet.
        path = self.host._directory(plan.digest()) / "fixture-stage.json"
        try:
            with path.open("rb") as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                return {"available": False}
            value = json.loads(raw)
            phases = {"python_started", "imports_ready", "before_server_init", "before_bind",
                      "before_getfqdn", "after_getfqdn", "bound", "listening", "server_ready"}
            if not isinstance(value, dict) or value.get("last_phase") not in phases or type(value.get("pid")) is not int:
                return {"available": False}
            stages = value.get("stages", {})
            if not isinstance(stages, dict) or set(stages) - phases or any(type(item) not in (int, float) for item in stages.values()):
                return {"available": False}
            return {"available": True, "pid": value["pid"], "last_phase": value["last_phase"], "stages": stages}
        except (OSError, ValueError):
            return {"available": False}

    def wait_terminal(self, plan, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.host.status(plan.digest())
            if result["status"] in {"unloaded", "failed", "cancelled", "expired", "unknown"}:
                return result
            time.sleep(0.05)
        self.fail("fixture did not terminate: " + str(result))

    def test_concurrent_sidecar_removal_is_allowed_but_unsafe_existing_files_are_not(self):
        sidecar = self.host.database.with_name("host.sqlite3-journal")
        sidecar.touch(mode=0o600)
        original = Path.lstat

        def removed_before_inspection(path):
            if path == sidecar:
                sidecar.unlink(missing_ok=True)
                raise FileNotFoundError(str(path))
            return original(path)

        with patch.object(Path, "lstat", removed_before_inspection):
            self.host._check()
        sidecar.symlink_to(self.base / "absent")
        with self.assertRaises(ModelError):
            self.host._check()
        sidecar.unlink()
        sidecar.touch(mode=0o644)
        with self.assertRaises(ModelError):
            self.host._check()
        sidecar.unlink()
        with patch("camol.model_host._private", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.host._check()

    def test_prepare_approve_are_passive_and_contracts_are_strict(self):
        plan = self.plan()
        with patch("camol.model_host.subprocess.Popen", side_effect=AssertionError("must not execute")), patch(
                "camol.model_host._http_json", side_effect=AssertionError("must not inspect")):
            self.assertEqual(self.host.prepare(plan)["status"], "prepared")
            with self.assertRaises(ModelError):
                self.host.load(plan.digest(), "owner", operation_id="denied")
            with self.assertRaises(ModelError):
                self.host.approve(plan.digest(), "worker")
            self.assertEqual(self.host.approve(plan.digest(), "owner")["status"], "approved")
            self.assertEqual(self.host.inventory()[0]["inference_ready"], "unverified")
        self.assertEqual(ModelHostPlan.from_dict(plan.to_dict()), plan)
        for update in ({"schema_version": True}, {"unknown": []}, {"port": True}, {"backend": "ollama"},
                       {"executable": "relative"}, {"model_store_root": "https://remote.invalid"}, {"gpu_layers": -1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                ModelHostPlan.from_dict(dict(plan.to_dict(), **update))

    def test_real_owned_load_readback_restart_unload_and_one_shot(self):
        plan = self.plan()
        self.approved(plan)
        with patch.dict(os.environ, {"HF_TOKEN": "synthetic-secret", "LLAMA_ARG_MODEL_URL": "https://external.invalid/model",
                                     "HTTP_PROXY": "http://external.invalid:42", "LLAMA_API_KEY": "foreign-key"}):
            loaded = self.host.load(plan.digest(), "owner", operation_id="load-one")
        self.assertEqual(loaded["status"], "loaded", dict(loaded, fixture_stage=self.fixture_stage(plan)))
        self.assertEqual(loaded["loaded"], "observed")
        self.assertEqual(loaded["inference_ready"], "unverified")
        self.assertIsNone(loaded["readback"]["provider_weight_digest"])
        directory = self.host._directory(plan.digest())
        invocation = json.loads((directory / "fixture-invocation.json").read_text())
        self.assertIn("--offline", invocation["argv"])
        self.assertIn("--no-warmup", invocation["argv"])
        self.assertEqual(invocation["argv"][invocation["argv"].index("--host") + 1], "127.0.0.1")
        for key in ("HF_TOKEN", "HTTP_PROXY", "LLAMA_ARG_MODEL_URL", "LLAMA_API_KEY", "PYTHONPATH"):
            self.assertNotIn(key, invocation["env"])
        secret = (directory / "api.key").read_text()
        self.assertEqual(stat.S_IMODE((directory / "api.key").stat().st_mode), 0o600)
        self.assertNotIn(secret, json.dumps(self.host.events(plan.digest())) + json.dumps(loaded))
        self.assertEqual(self.host.load(plan.digest(), "owner", operation_id="load-one")["load_operation_id"], "load-one")
        with self.assertRaisesRegex(ModelError, "one-shot"):
            self.host.load(plan.digest(), "owner", operation_id="load-two")
        self.host.close()
        self.host = LlamaCppModelHost(self.host_root)
        self.assertEqual(self.host.status(plan.digest(), live=True)["status"], "loaded")
        with LlamaCppModelHost(self.host_root, read_only=True) as observer:
            self.assertEqual(observer.status(plan.digest(), live=True)["loaded"], "observed")
            with self.assertRaisesRegex(ModelError, "read-only"):
                observer.approve(plan.digest(), "owner")
        request = self.host.propose_unload(plan.digest(), "unload-one")
        self.assertEqual(ModelHostUnload.from_dict(request.to_dict()), request)
        for actor, digest in (("worker", request.digest()), ("owner", plan.digest())):
            with self.assertRaises(ModelError):
                self.host.unload(request, actor, approve_digest=digest)
        self.assertEqual(self.host.unload(request, "owner", approve_digest=request.digest())["status"], "unloaded")
        self.assertEqual(self.host.unload(request, "owner", approve_digest=request.digest())["status"], "unloaded")
        self.assertEqual(self.host.connection.execute("SELECT state FROM operations WHERE operation_id='unload-one'").fetchone()[0], "completed")
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", plan.port)), 0)
        self.assertEqual(len([e for e in self.host.events(plan.digest()) if e["type"] == "MODEL_HOST_LOAD_INTENT"]), 1)

    def test_numeric_loopback_fixture_load_never_requires_dns(self):
        self.executable.write_text(self.executable.read_text().replace(
            "stage('imports_ready')", "stage('imports_ready')\n"
            "def forbidden_dns(*args, **kwargs):\n"
            "    raise AssertionError('loopback fixture must not resolve hostnames')\n"
            "http.server.socket.getfqdn = forbidden_dns\n"))
        plan = self.plan()
        self.approved(plan)
        loaded = self.host.load(plan.digest(), "owner", operation_id="load-no-dns")
        self.assertEqual(loaded["status"], "loaded", dict(loaded, fixture_stage=self.fixture_stage(plan)))
        self.assertEqual(loaded["loaded"], "observed")

    def test_input_substitution_and_busy_port_never_spawn(self):
        plan = self.plan()
        self.approved(plan)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", plan.port))
            listener.listen()
            with patch("camol.model_host.subprocess.Popen", side_effect=AssertionError("must not execute")):
                with self.assertRaisesRegex(ModelError, "occupied"):
                    self.host.load(plan.digest(), "owner", operation_id="busy")
        self.executable.write_text(self.executable.read_text() + "\n# substituted\n")
        with patch("camol.model_host.subprocess.Popen", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(ModelError, "approved digest"):
                self.host.load(plan.digest(), "owner", operation_id="changed")
        self.assertIsNone(self.host._operation(plan.digest()))

    def test_mismatched_runtime_never_becomes_ready_or_retries(self):
        plan = self.plan("mismatch", load_timeout_seconds=1)
        self.approved(plan)
        result = self.host.load(plan.digest(), "owner", operation_id="mismatch")
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["loaded"], "no")
        self.assertGreater(result["operation"]["detail"]["readback_attempts"], 0)
        self.assertEqual(set(result["operation"]["detail"]["last_readback_error"]), {"phase", "kind"})
        self.assertFalse(any(event["type"] == "MODEL_HOST_LOADED" for event in self.host.events(plan.digest())))
        self.assertEqual(self.host.load(plan.digest(), "owner", operation_id="mismatch")["status"], "cancelled")

    def test_readback_diagnostics_capture_only_fixed_phase_and_error_kind(self):
        plan = self.plan()
        with patch("camol.model_host._assert_listener", side_effect=ModelError("PRIVATE_RAW_ERROR_912f")):
            with self.assertRaises(ModelHostReadbackError) as caught:
                self.host._readback(plan, {"detail": {"alias": "fixture", "child_pid": 12345}})
        self.assertEqual(caught.exception.phase, "listener")
        self.assertEqual(caught.exception.kind, "invalid_readback")
        self.assertNotIn("PRIVATE_RAW_ERROR", str(caught.exception))
        diagnostic = ModelHostReadbackError("health", TimeoutError("private timeout"))
        self.assertEqual(diagnostic.phase, "health")
        self.assertEqual(diagnostic.kind, "timeout")
        self.assertNotIn("private timeout", str(diagnostic))

    def test_load_cancellation_awaits_owned_child_and_ttl_expiry(self):
        plan = self.plan("slow")
        self.approved(plan)
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        try:
            result = self.host.load(plan.digest(), "owner", operation_id="cancel", cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertEqual(result["status"], "cancelled", result)
        self.assertTrue(result["operation"]["detail"].get("owned_child_reaped"))
        plan = self.plan(lifetime_seconds=2)
        self.approved(plan)
        self.assertEqual(self.host.load(plan.digest(), "owner", operation_id="ttl")["status"], "loaded")
        result = self.wait_terminal(plan)
        self.assertEqual(result["status"], "expired", result)
        self.assertTrue(result["operation"]["detail"]["owned_child_reaped"])

    def test_noncooperative_owned_child_is_killed_and_reaped(self):
        plan = self.plan("ignoreterm")
        self.approved(plan)
        self.assertEqual(self.host.load(plan.digest(), "owner", operation_id="stubborn")["status"], "loaded")
        request = self.host.propose_unload(plan.digest(), "stop-stubborn")
        result = self.host.unload(request, "owner", approve_digest=request.digest())
        self.assertEqual(result["status"], "unloaded")
        self.assertEqual(result["operation"]["detail"]["exit_code"], -signal.SIGKILL)

    def test_helper_death_is_unknown_and_does_not_resume_or_kill_a_saved_pid(self):
        plan = self.plan()
        self.approved(plan)
        loaded = self.host.load(plan.digest(), "owner", operation_id="orphan")
        self.assertEqual(loaded["status"], "loaded")
        helper_pid = loaded["operation"]["detail"]["helper_pid"]
        owned_fixture_pid = loaded["operation"]["detail"]["child_pid"]
        helper = next(child for child in _CHILDREN if child.pid == helper_pid)
        helper.kill()
        helper.wait(timeout=3)
        try:
            self.assertEqual(self.host.status(plan.digest(), live=True)["status"], "unknown")
            with patch("camol.model_host.subprocess.Popen", side_effect=AssertionError("must not restart")):
                result = self.host.load(plan.digest(), "owner", operation_id="orphan")
                self.assertTrue(result["reconciliation_required"])
                request = self.host.propose_unload(plan.digest(), "orphan-unload")
                self.assertEqual(self.host.unload(request, "owner", approve_digest=request.digest())["status"], "unknown")
                other = self.plan()
                self.approved(other)
                with self.assertRaisesRegex(ModelError, "uncertain host"):
                    self.host.load(other.digest(), "owner", operation_id="different-plan")
            # The adapter did not touch the orphan just because its old PID was
            # saved. This test alone owns this tiny fixture and cleans it up.
            with socket.socket() as sock:
                self.assertEqual(sock.connect_ex(("127.0.0.1", plan.port)), 0)
        finally:
            os.kill(owned_fixture_pid, signal.SIGKILL)

    def test_uncertain_spawn_consumes_operation_and_never_auto_retries(self):
        plan = self.plan()
        self.approved(plan)
        with patch("camol.model_host.subprocess.Popen", side_effect=OSError("private raw failure secret")):
            with self.assertRaisesRegex(ModelError, "uncertain"):
                self.host.load(plan.digest(), "owner", operation_id="uncertain")
        self.assertTrue(self.host.status(plan.digest())["reconciliation_required"])
        self.assertNotIn("private raw failure", str(self.host.events(plan.digest())))
        with patch("camol.model_host.subprocess.Popen", side_effect=AssertionError("must not retry")):
            self.host.load(plan.digest(), "owner", operation_id="uncertain")

    def test_readback_transport_rejects_redirects_duplicates_and_slow_drips(self):
        def inspect(response, *, drip=False):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                def peer():
                    connection, _ = listener.accept()
                    with connection:
                        connection.recv(8192)
                        try:
                            if drip:
                                for byte in response:
                                    connection.sendall(bytes([byte]))
                                    time.sleep(0.1)
                            else:
                                connection.sendall(response)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                worker = threading.Thread(target=peer)
                worker.start()
                started = time.monotonic()
                with self.assertRaises((ModelError, OSError)):
                    _http_json(port, "/health")
                elapsed = time.monotonic() - started
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                return elapsed
        inspect(b"HTTP/1.1 302 Found\r\nLocation: https://external.invalid/secret\r\nContent-Length: 0\r\n\r\n")
        body = b'{"status":"no","status":"ok"}'
        inspect(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        inspect(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 2\r\n\r\n{}")
        self.assertLess(inspect(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", drip=True), 1.8)

    def test_changed_listener_is_not_sent_private_credential(self):
        plan = self.plan()
        self.approved(plan)
        self.assertEqual(self.host.load(plan.digest(), "owner", operation_id="listener")["status"], "loaded")
        with patch("camol.model_host._assert_listener", side_effect=ModelError("different child")), patch(
                "camol.model_host._http_json", side_effect=AssertionError("must not send any request")):
            result = self.host.status(plan.digest(), live=True)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["loaded"], "unverified")

    def test_private_store_readonly_and_symlink_boundaries(self):
        missing = self.base / "missing"
        with self.assertRaises(OSError):
            LlamaCppModelHost(missing, read_only=True)
        self.assertFalse(missing.exists())
        link = self.base / "linked-hosts"
        link.symlink_to(self.host_root)
        with self.assertRaises(ModelError):
            LlamaCppModelHost(link)
        plan = self.plan()
        self.approved(plan)
        directory = self.host._directory(plan.digest())
        directory.symlink_to(self.base)
        with self.assertRaises(ModelError):
            self.host.load(plan.digest(), "owner", operation_id="symlink")
        self.assertFalse((self.base / "api.key").exists())


if __name__ == "__main__":
    unittest.main()
