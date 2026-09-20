import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from camol.conversation import ConversationCancelled, ConversationError, ConversationReply
from camol.routed_chat import RoutedChatController
from camol.routed_prompt import build_routed_prompt
from tests.test_classifier_preview import result


class RoutedChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.predict = Mock(return_value=result("qwen"))
        self.generate = Mock(return_value=ConversationReply("Proposed plan: preserve A/D and arrows.", "claude", "sonnet", "claude-test", 42, 12))
        self.kwargs = dict(warm=lambda: None, workspace=Path(self.temp.name) / "empty",
                           state_root=Path(self.temp.name) / "state", converse_fn=self.generate)
        self.controller = RoutedChatController(self.predict, [], **self.kwargs)

    def test_original_request_reaches_no_tools_generator_and_durable_logs(self):
        request = "Plan a 3D ping-pong game. Keep A/D + arrows; don't review a PR."
        # Deliberately wrong route: the original request must retain priority.
        response = self.controller.handle(request)
        self.assertEqual(response.generation["status"], "completed")
        args, kwargs = self.generate.call_args
        payload = json.loads(args[1].split("\n\n", 1)[1])
        self.assertEqual(payload["user_request"], request)
        self.assertEqual(payload["advisory_route"], "review_changes")
        self.assertIn("follow the user", args[1])
        self.assertTrue(kwargs["no_tools"])
        self.assertEqual(kwargs["workspace"], self.kwargs["workspace"].resolve())
        calls = self.controller.store.planning_calls()
        self.assertEqual(calls[-1]["status"], "completed")
        self.assertEqual(calls[-1]["output_tokens"], 12)
        event = self.controller.store.proposal_events()[-1]
        self.assertEqual(event["prompt"], args[1])
        self.assertEqual(event["call_id"], calls[-1]["call_id"])
        self.assertEqual(self.controller.store.planning_calls_path.stat().st_mode & 0o777, 0o600)
        restored = RoutedChatController(self.predict, [], **self.kwargs)
        self.assertEqual(restored.handle("/prompt").messages[0], args[1])
        self.assertIn(request, str(restored.handle("/history").messages))
        loaded = restored.handle("/load")
        self.assertEqual(loaded.comparison[0]["model"], "qwen")
        self.assertEqual(loaded.generation["status"], "completed")

    def test_abstention_still_answers_original_request(self):
        self.predict.return_value = result("qwen", None)
        response = self.controller.handle("Explain what a paddle is.")
        self.assertEqual(response.generation["status"], "completed")
        self.assertIn("Do not force a guessed procedure", self.generate.call_args.args[1])

    def test_state_history_clear_and_model_survive_as_expected(self):
        self.controller.handle("/state 3D ping-pong")
        self.controller.handle("Plan the controls.")
        self.controller.handle("Now explain scoring.")
        args = self.generate.call_args.args
        self.assertEqual(len(args[2]), 2)
        self.assertIn("3D ping-pong", args[1])
        self.controller.handle("/model local:test-model")
        restored = RoutedChatController(self.predict, [], **self.kwargs)
        self.assertEqual(restored.model, "local:test-model")
        restored.handle("/clear")
        restored.handle("Start a new task")
        self.assertEqual(self.generate.call_args.args[2], [])
        self.assertEqual(len(restored.store.planning_calls()), 3)

    def test_bad_classifier_output_never_calls_generator_and_logs_failure(self):
        for observation in (result("qwen", "execute_anything"),
                            dict(result("qwen"), scores=[{"route": "explain", "score": float("nan")}])):
            self.predict.return_value = observation
            response = self.controller.handle("Explain this")
            self.assertIn("error", response.messages[0])
            self.assertFalse(self.controller.store.proposal_events()[-1]["generator_called"])
        self.generate.assert_not_called()

    def test_generator_failure_and_cancel_are_visible_and_logged(self):
        for error, status in ((ConversationError("not authenticated"), "failed"),
                              (ConversationCancelled("cancelled"), "cancelled")):
            self.generate.side_effect = error
            response = self.controller.handle("Explain this")
            self.assertIn(str(error), response.messages[0])
            self.assertEqual(self.controller.store.planning_calls()[-1]["status"], status)
            self.assertEqual(self.controller.session["messages"][-1]["kind"], "error")

    def test_commands_secrets_and_unsupported_provider_never_dispatch(self):
        for command in ("/run", "/approve abc", "/login claude", "/model codex:gpt-5", "API_TOKEN=abcdefghijklmnopqrstuvwxyz123456"):
            self.controller.handle(command)
        self.predict.assert_not_called()
        self.generate.assert_not_called()
        self.assertEqual(self.controller.model, "claude:sonnet")
        self.assertTrue(self.controller.handle("/state API_TOKEN=abcdefghijklmnopqrstuvwxyz123456").error)
        self.assertEqual(self.controller.state, "")

    def test_oversized_prompt_rejected_without_truncation(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            build_routed_prompt("x" * 12000, "", result("qwen"))


if __name__ == "__main__":
    unittest.main()
