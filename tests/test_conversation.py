import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.conversation import ConversationError, _environment, _prompt, converse, parse_selection, provider_argv


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

    def test_local_conversation_rejects_non_loopback_before_network_access(self):
        with patch("camol.conversation.open_without_proxy", side_effect=AssertionError("network called")):
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


if __name__ == "__main__":
    unittest.main()
