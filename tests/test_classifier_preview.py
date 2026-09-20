import threading
import unittest
from unittest.mock import Mock

from camol.classifier_preview import ClassifierPreviewController, PREVIEW_COMMANDS


def result(name="small", route="review_changes"):
    return {"model": name, "proposed_route": route, "top_route": "review_changes",
            "abstained": route is None, "latency_ms": 25.0, "tokens": 100,
            "scores": [{"route": "review_changes", "score": .9}, {"route": "explain", "score": .1}]}


class ClassifierPreviewTests(unittest.TestCase):
    def setUp(self):
        self.predict = Mock(return_value=[result(), result("qwen")])
        self.controller = ClassifierPreviewController(self.predict, [])

    def test_execution_and_login_commands_never_reach_a_predictor(self):
        for command in ("/run", "/approve abc", "/login claude", "/grill deploy", "/import plan.json", "/target provision", "/switch"):
            with self.subTest(command=command):
                response = self.controller.handle(command)
                self.assertIn("unavailable", response.messages[0])
                self.assertIsNone(response.login_argv)
                self.assertFalse(response.login_choices)
        self.predict.assert_not_called()

    def test_state_is_shared_by_models_and_clear_resets_it(self):
        self.controller.handle("/state Review pull request 5")
        response = self.controller.handle("Do it.")
        self.predict.assert_called_once_with("Do it.", "Review pull request 5")
        self.assertEqual(len(response.comparison), 2)
        cleared = self.controller.handle("/clear")
        self.assertTrue(cleared.clear_transcript)
        self.assertTrue(cleared.reset_results)
        self.assertEqual(self.controller.state, "")

    def test_sensitive_input_is_never_sent_to_predictor(self):
        response = self.controller.handle("API_TOKEN=abcdefghijklmnopqrstuvwxyz123456")
        self.predict.assert_not_called()
        self.assertIsNone(response.comparison)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(response))

    def test_cancel_discards_in_flight_comparison(self):
        entered, release = threading.Event(), threading.Event()
        returned = []
        def predict(text, state):
            entered.set()
            release.wait(3)
            return [result(), result("qwen")]
        self.controller.predict = predict
        thread = threading.Thread(target=lambda: returned.append(self.controller.handle("Review this")))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertIn("running", self.controller.handle("/state changed").messages[0])
            self.assertEqual(self.controller.state, "")
            self.controller.handle("/cancel")
        finally:
            release.set()
            thread.join(3)
        self.assertIsNone(returned[0].comparison)
        self.assertTrue(returned[0].reset_results)

    def test_close_prevents_another_request(self):
        self.controller.close_client()
        self.controller.handle("Review the diff")
        self.predict.assert_not_called()

    def test_predictor_failure_clears_stale_results(self):
        self.predict.side_effect = ValueError("prompt too long")
        response = self.controller.handle("hello")
        self.assertIn("prompt too long", response.messages[0])
        self.assertTrue(response.reset_results)

    def test_preview_palette_has_no_execution_commands(self):
        commands = {command.command for command in PREVIEW_COMMANDS}
        self.assertTrue({"/state", "/clear", "/routes", "/example"} <= commands)
        self.assertFalse({"/run", "/approve", "/login", "/grill"} & commands)

    def test_load_failure_is_reported_without_claiming_readiness(self):
        self.controller.warm = Mock(side_effect=OSError("weights missing"))
        response = self.controller.handle("/load")
        self.assertTrue(self.controller.load_failed)
        self.assertFalse(self.controller.ready)
        self.assertIn("weights missing", response.messages[0])


if __name__ == "__main__":
    unittest.main()
