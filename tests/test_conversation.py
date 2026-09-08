import json
import subprocess
import tempfile
import unittest
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from camol.conversation import ConversationCancelled, ConversationError, ConversationReply, _environment, _prompt, converse, parse_selection, provider_argv


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_selector_is_strict_and_does_not_allow_option_injection(self):
        self.assertEqual(parse_selection("claude:fable").model, "fable")
        self.assertEqual(parse_selection("manual").provider, "manual")
        for value in ("claude:--dangerous", "unknown:model", "local"):
            with self.subTest(value=value), self.assertRaises(ConversationError):
                parse_selection(value)

    def test_unusable_usage_and_model_metadata_stay_unknown(self):
        for count in (-1, True, "12", float("nan"), 1 << 80):
            reply = ConversationReply("text", "local", "fixture", {"not": "an identity"}, count, count)
            self.assertIsNone(reply.input_tokens)
            self.assertIsNone(reply.output_tokens)
            self.assertIsNone(reply.resolved_model)

    def test_provider_context_contains_dialogue_but_not_slash_command_noise(self):
        prompt = _prompt(
            "next question",
            [
                {"role": "human", "content": "/connections", "kind": "command"},
                {"role": "system", "content": "connection details", "kind": "notice"},
                {"role": "human", "content": "design the API", "kind": "conversation"},
                {"role": "orchestrator", "content": "Which invariant?", "kind": "conversation"},
                {"role": "orchestrator", "content": "model identity: default -> old", "kind": "conversation"},
            ],
        )
        self.assertNotIn("/connections", prompt)
        self.assertNotIn("connection details", prompt)
        self.assertNotIn("model identity", prompt)
        self.assertIn("design the API", prompt)
        self.assertIn("Which invariant?", prompt)

    def test_claude_conversation_is_planning_only_and_parses_identity(self):
        payload = {
            "result": "Ask what must not change.",
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "modelUsage": {"claude-fable-5": {}},
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode(), stderr=b"")
        with patch("camol.conversation.shutil.which", return_value="/usr/bin/claude"):
            reply = converse(
                "claude:fable", "help plan", [], effort="high", workspace=self.workspace,
                runner=lambda *args, **kwargs: completed,
            )
            argv = provider_argv(parse_selection("claude:fable"), "high", self.workspace)
        self.assertIn("--permission-mode", argv)
        self.assertIn("--safe-mode", argv)
        self.assertEqual(reply.resolved_model, "claude-fable-5")
        self.assertEqual(reply.output_tokens, 7)

    def test_cli_conversation_preserves_login_identity_but_not_arbitrary_environment(self):
        with patch.dict(
            "os.environ",
            {"USER": "owner", "LOGNAME": "owner", "CODEX_HOME": "/safe/config", "SECRET_VALUE": "no"},
            clear=True,
        ):
            environment = _environment()
        self.assertEqual(environment["USER"], "owner")
        self.assertEqual(environment["LOGNAME"], "owner")
        self.assertEqual(environment["CODEX_HOME"], "/safe/config")
        self.assertNotIn("SECRET_VALUE", environment)

    def test_codex_conversation_uses_read_only_ephemeral_exec(self):
        output = (
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Define the invariant."}})
            + "\n" + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 3}})
        ).encode()
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr=b"")
        with patch("camol.conversation.shutil.which", return_value="/usr/bin/codex"):
            reply = converse(
                "codex:gpt-5.4", "plan", [], effort="xhigh", workspace=self.workspace,
                runner=lambda *args, **kwargs: completed,
            )
            argv = provider_argv(parse_selection("codex:gpt-5.4"), "xhigh", self.workspace)
        self.assertIn("read-only", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(reply.text, "Define the invariant.")

    def test_manual_and_openai_execution_are_truthfully_unavailable(self):
        for selection in ("manual", "openai:gpt-5.4"):
            with self.subTest(selection=selection), self.assertRaises(ConversationError):
                converse(selection, "hello", [], effort="high", workspace=self.workspace)

    def test_proposal_codex_is_denied_before_invocation_but_normal_chat_remains_supported(self):
        runner = Mock(side_effect=AssertionError("must not invoke runtime"))
        with self.assertRaisesRegex(ConversationError, "all-tools-off"):
            converse("codex:gpt-5.4", "plan", [], effort="high", workspace=self.workspace,
                     no_tools=True, runner=runner)
        runner.assert_not_called()

    def test_proposal_claude_requires_and_uses_no_tools_runtime_controls(self):
        help_text = "--tools --safe-mode --strict-mcp-config --mcp-config --setting-sources --disable-slash-commands --permission-prompts --max-turns"
        payload = {"result": '{"questions":["Which oracle?"]}', "usage": {"input_tokens": 2, "output_tokens": 3}}
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=(help_text.encode() if "--help" in argv else json.dumps(payload).encode()), stderr=b"")
        with patch("camol.conversation.shutil.which", return_value="/opt/camol-test/claude"):
            reply = converse("claude:fable", "plan", [], effort="high", workspace=self.workspace,
                             no_tools=True, runner=runner)
        self.assertEqual(len(calls), 2)  # read-only help probe, then one model invocation
        self.assertNotEqual(calls[0][1]["cwd"], str(self.workspace))
        argv = calls[1][0]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertEqual(reply.output_tokens, 3)
        calls.clear()
        help_text = "old runtime without required flags"
        with patch("camol.conversation.shutil.which", return_value="/opt/camol-test/claude"):
            with self.assertRaisesRegex(ConversationError, "no planning request sent"):
                converse("claude:fable", "plan", [], effort="high", workspace=self.workspace,
                         no_tools=True, runner=runner)
        self.assertEqual(len(calls), 1)

    def test_proposal_local_tool_call_response_is_rejected_with_usage_without_execution(self):
        payload = json.dumps({"choices": [{"message": {"content": None, "tool_calls": [{"name": "Bash"}]}}],
                              "usage": {"prompt_tokens": 2, "completion_tokens": 3}, "model": "fixture"}).encode()
        with patch("camol.conversation.AbortableLocalHTTP.request", return_value=payload) as opened:
            with self.assertRaisesRegex(ConversationError, "prohibited tool calls") as failure:
                converse("local:fixture", "plan", [], effort="high", workspace=self.workspace, no_tools=True)
        self.assertEqual(json.loads(opened.call_args.args[1])["tool_choice"], "none")
        self.assertNotIn("tools", json.loads(opened.call_args.args[1]))
        self.assertEqual(failure.exception.reply.output_tokens, 3)

    def test_local_wait_has_wall_clock_bound_even_when_endpoint_keeps_processing(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        disconnected = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "999999")
                self.end_headers()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        disconnected.set()
                        return
                    time.sleep(.01)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        service = threading.Thread(target=server.serve_forever, daemon=True)
        service.start()
        try:
            for cancel in (False, True):
                cancelled = threading.Event()
                timer = threading.Timer(.06, cancelled.set) if cancel else None
                if timer:
                    timer.start()
                started = time.monotonic()
                expected = "cancelled" if cancel else "timed out"
                with self.assertRaisesRegex(ConversationError, expected + ".*usage is unknown"):
                    converse("local:fixture", "plan", [], effort="high", workspace=self.workspace,
                             no_tools=True, timeout=1 if cancel else .08, cancel_event=cancelled,
                             local_endpoint="http://127.0.0.1:{}/v1".format(server.server_port))
                if timer:
                    timer.cancel()
                self.assertLess(time.monotonic() - started, .8)
                self.assertFalse(any(thread.name == "camol-local-planner" and thread.is_alive() for thread in threading.enumerate()))
                self.assertTrue(disconnected.wait(.5))
                disconnected.clear()
        finally:
            server.shutdown()
            server.server_close()
            service.join(1)

    def test_inner_socket_timeout_has_same_unknown_usage_outcome_as_outer_deadline(self):
        import socket
        for error in (TimeoutError("PRIVATE_TIMEOUT_CAUSE"), socket.timeout("PRIVATE_SOCKET_CAUSE")):
            with self.subTest(kind=type(error).__name__), patch(
                    "camol.conversation.AbortableLocalHTTP.request", side_effect=error) as request:
                with self.assertRaisesRegex(ConversationError, "timed out.*usage is unknown") as caught:
                    converse("local:fixture", "plan", [], effort="high", workspace=self.workspace, timeout=10)
                self.assertNotIn("PRIVATE_", str(caught.exception))
                self.assertEqual(request.call_count, 1)
                self.assertFalse(any(thread.name == "camol-local-planner" and thread.is_alive() for thread in threading.enumerate()))

    def test_local_conversation_rejects_non_loopback_before_network_access(self):
        with patch("camol.conversation.AbortableLocalHTTP.request", side_effect=AssertionError("network called")):
            with self.assertRaisesRegex(ConversationError, "numeric loopback"):
                converse(
                    "local:qwen", "private plan", [], effort="high", workspace=self.workspace,
                    local_endpoint="https://example.com/v1",
                )

    def test_claude_jsonl_chunks_are_streamed_before_final_reply(self):
        executable = self.workspace / "fake-claude"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'stream_event','event':{'delta':{'text':'first '}}}), flush=True)\n"
            "print(json.dumps({'type':'stream_event','event':{'delta':{'text':'second'}}}), flush=True)\n"
            "print(json.dumps({'type':'result','result':'first second','usage':{'input_tokens':2,'output_tokens':2},'modelUsage':{'claude-fable-5':{}}}), flush=True)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        chunks = []
        with patch("camol.conversation.shutil.which", return_value=str(executable)):
            reply = converse(
                "claude:fable", "hello", [], effort="high", workspace=self.workspace,
                on_chunk=chunks.append,
            )
        self.assertEqual(chunks, ["first ", "second"])
        self.assertEqual(reply.text, "first second")

    def test_cancellation_kills_provider_even_when_it_never_reads_prompt(self):
        executable = self.workspace / "fake-claude"
        executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
        executable.chmod(0o755)
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        started = time.monotonic()
        timer.start()
        try:
            with patch("camol.conversation.shutil.which", return_value=str(executable)):
                with self.assertRaises(ConversationCancelled):
                    converse(
                        "claude:fable", "hello", [{"role": "human", "content": "x" * 4000}] * 20,
                        effort="high", workspace=self.workspace, cancel_event=cancelled,
                    )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
