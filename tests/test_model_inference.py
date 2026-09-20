"""Owned-loopback inference tests using tiny real processes, never real models."""

import hashlib
import http.server
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camol.model_inference import ModelInference, ModelInferencePlan, prompt_file_identity, validate_prompt_file
from camol import model_host
from camol.models import ModelError
from tests import test_model_host as hosting_fixture


POST_HANDLER = r'''
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        import hashlib
        received = json.loads(body)
        record = {'path': self.path, 'authenticated': self.headers.get('Authorization') == 'Bearer ' + key,
                  'body_digest': 'sha256:' + hashlib.sha256(body).hexdigest(), 'fields': sorted(received),
                  'model': received.get('model'), 'max_tokens': received.get('max_tokens'),
                  'message_roles': [m.get('role') for m in received.get('messages', [])]}
        with open('fixture-posts.jsonl', 'a') as handle: handle.write(json.dumps(record) + '\n')
        if not record['authenticated'] or self.path != '/v1/chat/completions':
            self.send_response(401); self.send_header('Content-Length', '0'); self.end_headers(); return
        if mode == 'drop': return
        if mode == 'httpfail':
            self.send_response(500); self.send_header('Content-Length', '0'); self.end_headers(); return
        if mode == 'slow': time.sleep(5)
        data = {'model': 'wrong' if mode == 'wrongalias' else alias,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'PRIVATE_RESPONSE_82a1'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 9 if mode == 'overspend' else 2,
                          'total_tokens': 12 if mode == 'overspend' else 5}}
        if mode == 'nousage': data.pop('usage')
        if mode == 'badusage': data['usage'] = {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': 7}
        if mode == 'tools': data['choices'][0]['message']['tool_calls'] = [{'name': 'do_not_execute'}]
        if mode == 'keyecho': data['choices'][0]['message']['content'] = key
        raw = json.dumps(data).encode()
        if mode == 'duplicate': raw = raw.replace(b'"model":', b'"model":"duplicate", "model":', 1)
        if mode == 'nonfinite': raw = raw[:-1] + b',"invalid":NaN}'
        if mode == 'drip':
            try:
                for byte in b'HTTP/1.0 200 OK\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw:
                    self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(0.08)
            except (BrokenPipeError, ConnectionResetError): pass
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        if mode == 'headers': self.send_header('Content-Length', str(len(raw)))
        if mode == 'chunked': self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass
'''


class ModelInferenceTests(unittest.TestCase):
    setUp = hosting_fixture.ModelHostTests.setUp
    tearDown = hosting_fixture.ModelHostTests.tearDown
    plan = hosting_fixture.ModelHostTests.plan
    approved = hosting_fixture.ModelHostTests.approved

    def start(self, mode="normal"):
        # Threaded fixture keeps lifecycle readback independent of a deliberately
        # slow inference handler. This is not a model or a production server.
        fixture = hosting_fixture.FIXTURE.replace("    def do_GET(self):", POST_HANDLER + "\n    def do_GET(self):")
        fixture = fixture.replace("if mode == 'slow': time.sleep(3)", "# inference-only delay")
        fixture = fixture.replace("http.server.HTTPServer", "http.server.ThreadingHTTPServer")
        import sys
        self.executable.write_text("#!" + sys.executable + "\n" + fixture)
        self.executable.chmod(0o700)
        host_plan = self.plan(mode, lifetime_seconds=45)
        self.approved(host_plan)
        loaded = self.host.load(host_plan.digest(), "owner", operation_id="load-one")
        self.assertEqual(loaded["status"], "loaded", loaded)
        self.prompt = self.base / "private-prompt.txt"
        self.prompt.write_text("PRIVATE_PROMPT_59f3\n")
        self.inference = ModelInference(self.host_root)
        self.addCleanup(self.inference.close)
        self.load_plan = host_plan
        self.alias = loaded["operation"]["detail"]["alias"]
        return self.request()

    def request(self, operation_id="infer-one", **changes):
        now = datetime.now(timezone.utc)
        values = dict(operation_id=operation_id, host_plan_digest=self.load_plan.digest(), load_operation_id="load-one",
                      owner="owner", model_alias=self.alias, **prompt_file_identity(self.prompt), max_output_tokens=4,
                      timeout_seconds=3, issued_at=(now - timedelta(seconds=1)).isoformat(),
                      expires_at=(now + timedelta(seconds=20)).isoformat())
        values.update(changes)
        return ModelInferencePlan(**values)

    def approve_request(self, plan):
        self.inference.prepare(plan)
        self.inference.approve(plan.digest(), "owner")

    def posts(self):
        file = self.host._directory(self.load_plan.digest()) / "fixture-posts.jsonl"
        return [] if not file.exists() else [json.loads(line) for line in file.read_text().splitlines()]

    def infer(self, plan, **kwargs):
        return self.inference.infer(plan.digest(), "owner", prompt_path=self.prompt, **kwargs)

    def test_contract_passive_prepare_exact_owner_and_private_one_shot_success(self):
        plan = self.start()
        self.assertEqual(ModelInferencePlan.from_dict(plan.to_dict()), plan)
        for changes in ({"schema_version": True}, {"unknown": 1}, {"max_output_tokens": True},
                        {"prompt_size_bytes": (1 << 20) + 1}, {"model_alias": "arbitrary"}, {"timeout_seconds": 601}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ModelInferencePlan.from_dict(dict(plan.to_dict(), **changes))
        with patch("camol.model_host._http_json", side_effect=AssertionError("no readback during preparation")), patch(
                "camol.model_host.LlamaCppModelHost._credential", side_effect=AssertionError("no credential during preparation")):
            self.assertEqual(self.inference.prepare(plan)["status"], "prepared")
            with self.assertRaises(ModelError): self.infer(plan)
            with self.assertRaises(ModelError): self.inference.approve(plan.digest(), "worker")
            self.inference.approve(plan.digest(), "owner")
        result = self.infer(plan)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["response_text"], "PRIVATE_RESPONSE_82a1")
        self.assertEqual(result["receipt"]["usage"]["output_tokens"], 2)
        self.assertEqual(result["receipt"]["usage"]["source"], "provider_reported")
        self.assertIsNone(result["receipt"]["usage"]["total_cost_usd"])
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.posts()[0]["authenticated"])
        self.assertEqual(self.posts()[0]["fields"], ["max_tokens", "messages", "model", "n", "stream"])
        self.assertEqual(self.posts()[0]["message_roles"], ["user"])
        self.assertEqual(self.posts()[0]["body_digest"], result["receipt"]["request_body_digest"])
        self.assertFalse(result["capabilities"]["hard_output_token_cap"])
        for path in (self.inference.database, self.inference._lock_path(plan.digest())):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with patch("camol.model_inference._post", side_effect=AssertionError("no replay POST")):
            replay = self.infer(plan)
            self.assertIsNone(replay["response_text"])
            with ModelInference(self.host_root, read_only=True) as reopened:
                self.assertEqual(reopened.status(plan.digest())["status"], "completed")
                self.assertEqual(len(reopened.inventory()), 1)
        with self.assertRaisesRegex(ModelError, "already consumed"):
            self.inference.approve(plan.digest(), "owner")
        key = (self.host._directory(self.load_plan.digest()) / "api.key").read_text()
        metadata = json.dumps(self.inference.events(plan.digest())) + str(self.inference.database.read_bytes())
        for secret in (key, "PRIVATE_PROMPT_59f3", "PRIVATE_RESPONSE_82a1", str(self.prompt)):
            self.assertNotIn(secret, metadata)

    def test_future_expired_approval_tampered_prompt_and_operation_identity(self):
        plan = self.start()
        self.inference.prepare(plan)
        with patch("camol.model_inference.time.time", return_value=datetime.fromisoformat(plan.issued_at).timestamp() - 1):
            with self.assertRaisesRegex(ModelError, "future-issued"):
                self.inference.approve(plan.digest(), "owner")
        with patch("camol.model_inference.time.time", return_value=datetime.fromisoformat(plan.expires_at).timestamp()):
            with self.assertRaisesRegex(ModelError, "expired"):
                self.inference.approve(plan.digest(), "owner")
        with self.assertRaisesRegex(ModelError, "different contract"):
            self.inference.prepare(replace(plan, max_output_tokens=5))
        self.inference.approve(plan.digest(), "owner")
        self.prompt.write_text("changed prompt")
        with self.assertRaisesRegex(ModelError, "exact approved bytes"): self.infer(plan)
        self.assertEqual(self.inference.status(plan.digest())["status"], "approved")
        self.assertFalse(self.posts())
        self.inference.connection.execute("UPDATE requests SET document=? WHERE digest=?",
                                         (json.dumps(replace(plan, owner="worker").to_dict()), plan.digest()))
        self.inference.connection.commit()
        with self.assertRaisesRegex(ModelError, "integrity"): self.inference.status(plan.digest())

    def test_wrong_owned_listener_denied_before_any_prompt_or_key_release(self):
        plan = self.start()
        self.approve_request(plan)
        with patch("camol.model_host._assert_listener", side_effect=ModelError("wrong listener")), patch(
                "camol.model_host.LlamaCppModelHost._credential", side_effect=AssertionError("must not read key")):
            result = self.infer(plan)
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(result["receipt"]["observed"]["dispatch_attempted"])
        self.assertFalse(self.posts())

    def test_owned_listener_rechecked_after_connection_before_post(self):
        plan = self.start()
        self.approve_request(plan)
        with patch("camol.model_inference._assert_listener", side_effect=ModelError("listener changed")):
            result = self.infer(plan)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["receipt"]["observed"]["dispatch_attempted"])
        self.assertFalse(self.posts())

    def test_actual_listener_observation_rejects_a_different_process_identity(self):
        plan = self.start()
        self.approve_request(plan)
        self.host._update(self.load_plan.digest(), detail={"child_pid": os.getpid()})
        with patch("camol.model_host.LlamaCppModelHost._credential", side_effect=AssertionError("no key for foreign PID")):
            result = self.infer(plan)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["receipt"]["observed"]["dispatch_attempted"])
        self.assertFalse(self.posts())

    def test_listener_replacement_after_health_receives_no_private_key(self):
        plan = replace(self.start(), timeout_seconds=10)
        self.approve_request(plan)
        received, server = [], [None]
        original = model_host._http_json
        class Replacement(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                received.append({"path": self.path, "authorization_received": self.headers.get("Authorization") is not None})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        def switched(port, path, *args, **kwargs):
            response = original(port, path, *args, **kwargs)
            if path == "/health" and server[0] is None:
                request = self.host.propose_unload(self.load_plan.digest(), "review-unload")
                self.assertEqual(self.host.unload(request, "owner", approve_digest=request.digest())["status"], "unloaded")
                server[0] = http.server.ThreadingHTTPServer(("127.0.0.1", port), Replacement)
                threading.Thread(target=server[0].serve_forever, daemon=True).start()
            return response
        try:
            with patch("camol.model_host._http_json", side_effect=switched):
                result = self.infer(plan)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(received, [], "replacement must not receive authenticated readback requests")
            self.assertFalse(self.posts())
        finally:
            if server[0] is not None:
                server[0].shutdown()
                server[0].server_close()

    def test_database_contention_fails_fast_before_intent_or_dispatch(self):
        plan = replace(self.start(), timeout_seconds=1)
        self.approve_request(plan)
        blocker = sqlite3.connect(str(self.inference.database))
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ModelError, "busy or unavailable"):
                self.infer(plan)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(self.posts())
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.inference.status(plan.digest())["status"], "approved")
        self.assertFalse(any(event["type"] == "MODEL_INFERENCE_INTENT" for event in self.inference.events(plan.digest())))

    def test_uncommittable_outcome_retains_durable_unknown_intent_without_waiting(self):
        plan = self.start()
        self.approve_request(plan)
        blocker = sqlite3.connect(str(self.inference.database))
        from camol import model_inference
        original = model_inference._post
        def held_after_response(*args, **kwargs):
            result = original(*args, **kwargs)
            blocker.execute("BEGIN IMMEDIATE")
            return result
        try:
            with patch("camol.model_inference._post", side_effect=held_after_response):
                started = time.monotonic()
                result = self.infer(plan)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["receipt"]["outcome_persisted"])
            self.assertTrue(result["reservation_held"])
            self.assertIsNone(result["response_text"])
            self.assertEqual(result["receipt"]["usage"]["output_tokens"], 2)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.inference.status(plan.digest())["status"], "unknown")
        self.assertEqual(self.infer(plan)["status"], "unknown")
        self.assertEqual(len(self.posts()), 1)

    def test_cancellation_closes_owned_transport_and_unknown_blocks_new_id(self):
        plan = self.start("slow")
        self.approve_request(plan)
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        started = time.monotonic()
        try: result = self.infer(plan, cancel_event=cancel)
        finally: timer.cancel()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["receipt"]["error"], "cancelled")
        self.assertTrue(result["receipt"]["transport_closed"])
        self.assertEqual(result["receipt"]["server_execution_stopped"], "unverified")
        self.assertTrue(result["reservation_held"])
        self.assertEqual(result["reserved_output_token_request"], 4)
        other = self.request("infer-two")
        self.approve_request(other)
        with self.assertRaisesRegex(ModelError, "uncertain inference"): self.infer(other)
        with patch("camol.model_inference._post", side_effect=AssertionError("never retry")):
            self.assertEqual(self.infer(plan)["status"], "unknown")
        self.assertEqual(len(self.posts()), 1)

    def test_deadline_is_absolute_and_shutdown_does_not_claim_server_stop(self):
        plan = self.start("slow")
        plan = replace(plan, timeout_seconds=1)
        self.approve_request(plan)
        started = time.monotonic()
        result = self.infer(plan)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["receipt"]["error"], "deadline")
        self.assertTrue(result["receipt"]["transport_closed"])
        self.assertEqual(result["receipt"]["usage"]["source"], "unknown")
        self.assertIsNone(result["receipt"]["usage"]["total_cost_usd"])

    def test_header_slow_drip_does_not_restart_the_absolute_deadline(self):
        plan = replace(self.start("drip"), timeout_seconds=1)
        self.approve_request(plan)
        started = time.monotonic()
        result = self.infer(plan)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["receipt"]["error"], "deadline")
        self.assertTrue(result["receipt"]["transport_closed"])

    def test_independent_clients_cannot_dispatch_two_requests_for_one_load(self):
        first = self.start("slow")
        second = self.request("concurrent-two")
        self.approve_request(first)
        self.approve_request(second)
        cancel, results = threading.Event(), []
        def worker():
            with ModelInference(self.host_root) as client:
                results.append(client.infer(first.digest(), "owner", prompt_path=self.prompt, cancel_event=cancel))
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while not self.posts() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.inference.status(first.digest())["status"], "in_flight")
            with self.assertRaisesRegex(ModelError, "active or uncertain"):
                self.infer(second)
            self.assertEqual(len(self.posts()), 1)
        finally:
            cancel.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "unknown")

    def test_provider_usage_overrun_records_actual_not_requested_or_zero(self):
        plan = self.start("overspend")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "failed", result)
        self.assertTrue(result["receipt"]["limit_violation"])
        self.assertEqual(result["receipt"]["usage"]["output_tokens"], 9)
        self.assertEqual(result["receipt"]["usage"]["total_tokens"], 12)
        self.assertEqual(result["receipt"]["requested_output_tokens"], 4)
        self.assertIsNone(result["response_text"])
        self.assertFalse(result["reservation_held"])

    def test_missing_provider_usage_stays_unknown_despite_success(self):
        plan = self.start("nousage")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["receipt"]["usage"]["source"], "unknown")
        self.assertIsNone(result["receipt"]["usage"]["output_tokens"])
        self.assertEqual(result["usage_coverage"]["output_tokens"], "unknown")

    def test_invalid_counts_do_not_turn_into_zero_or_measured_usage(self):
        plan = self.start("badusage")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["receipt"]["usage"]["invalid_fields"])
        self.assertIsNone(result["receipt"]["usage"]["input_tokens"])
        self.assertIsNone(result["receipt"]["usage"]["output_tokens"])
        self.assertEqual(result["receipt"]["usage"]["total_tokens"], 7)
        self.assertEqual(result["usage_coverage"]["total_tokens"], "provider_reported")
        self.assertEqual(result["usage_coverage"]["output_tokens"], "unknown")

    def test_known_http_failure_after_dispatch_is_not_zero_usage_or_retry(self):
        plan = self.start("httpfail")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "unknown", result)
        self.assertTrue(result["receipt"]["observed"]["dispatch_attempted"])
        self.assertIsNone(result["receipt"]["usage"]["total_tokens"])
        self.assertIsNone(result["receipt"]["usage"]["total_cost_usd"])
        self.assertEqual(self.infer(plan)["status"], "unknown")
        self.assertEqual(len(self.posts()), 1)

    def test_ambiguous_json_is_unknown_and_never_leaks_response(self):
        plan = self.start("duplicate")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "unknown", result)
        self.assertIsNone(result["response_text"])
        self.assertNotIn("PRIVATE_RESPONSE", json.dumps(self.inference.events(plan.digest())))
        self.assertEqual(len(self.posts()), 1)

    def test_duplicate_headers_are_rejected_before_parsing_provider_usage(self):
        plan = self.start("headers")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["receipt"]["usage"]["source"], "unknown")

    def test_nonfinite_json_is_not_accepted_as_completion(self):
        plan = self.start("nonfinite")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["response_text"])

    def test_tool_calls_are_never_executed_or_treated_as_text_success(self):
        plan = self.start("tools")
        self.approve_request(plan)
        result = self.infer(plan)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["response_text"])
        self.assertEqual(result["receipt"]["usage"]["output_tokens"], 2)

    def test_server_cannot_echo_private_transport_key_into_public_result(self):
        plan = self.start("keyecho")
        self.approve_request(plan)
        result = self.infer(plan)
        key = (self.host._directory(self.load_plan.digest()) / "api.key").read_text()
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["response_text"])
        self.assertNotIn(key, json.dumps(result))
        self.assertNotIn(key, json.dumps(self.inference.events(plan.digest())))

    def test_readonly_missing_store_and_prompt_nonregular_inputs_are_passive(self):
        with self.assertRaises(OSError): ModelInference(self.base / "missing", read_only=True)
        self.assertFalse((self.base / "missing").exists())
        fifo = self.base / "prompt.fifo"
        os.mkfifo(str(fifo))
        with self.assertRaises(ModelError): prompt_file_identity(fifo)
        file = self.base / "prompt.txt"
        file.write_text("prompt")
        link = self.base / "prompt-link"
        link.symlink_to(file)
        with self.assertRaises(OSError): prompt_file_identity(link)
        file.write_bytes(b"\xff")
        with self.assertRaisesRegex(ModelError, "UTF-8"): prompt_file_identity(file)
        file.write_bytes(b"x" * ((1 << 20) + 1))
        with self.assertRaises(ModelError): prompt_file_identity(file)

    def test_lost_inflight_record_never_restarts_and_retains_reservation(self):
        plan = self.start()
        self.approve_request(plan)
        self.inference.connection.execute("UPDATE requests SET state='in_flight' WHERE digest=?", (plan.digest(),))
        self.inference.connection.commit()
        with patch("camol.model_inference._post", side_effect=AssertionError("never recover by retry")):
            result = self.infer(plan)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reservation_held"])
        self.assertFalse(self.posts())

    def test_actual_client_death_preserves_unknown_and_denies_new_operation_id(self):
        plan = self.start("slow")
        self.approve_request(plan)
        source = Path(model_host.__file__).resolve().parents[1]
        bootstrap = ("import sys;sys.path.insert(0,{!r});from camol.model_inference import ModelInference;"
                     "client=ModelInference({!r});client.infer({!r},'owner',prompt_path={!r})").format(
                         str(source), str(self.host_root), plan.digest(), str(self.prompt))
        child = subprocess.Popen([sys.executable, "-I", "-c", bootstrap], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while not self.posts() and time.monotonic() < deadline:
                self.assertIsNone(child.poll())
                time.sleep(0.02)
            self.assertEqual(len(self.posts()), 1)
            child.kill()
            child.wait(timeout=2)
            result = self.inference.status(plan.digest())
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["reservation_held"])
            self.assertEqual(self.infer(plan)["status"], "unknown")
            second = self.request("after-crash")
            self.approve_request(second)
            with self.assertRaisesRegex(ModelError, "retains"):
                self.infer(second)
            self.assertEqual(len(self.posts()), 1)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
