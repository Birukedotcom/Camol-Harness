import time
import unittest
from unittest.mock import Mock

from textual.widgets import RichLog, Static

from camol.classifier_preview import ClassifierPreviewController
from camol.classifier_tui import ClassifierPreviewApp
from camol.tui import CamolApp, PromptArea, SlashCommandScreen
from tests.test_classifier_preview import result


class ClassifierTuiTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, pilot, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("classifier view did not settle")
            await pilot.pause(.01)
        await pilot.pause(.05)

    async def test_native_composer_and_both_model_cards(self):
        predict = Mock(return_value=[result(), result("qwen", None)])
        controller = ClassifierPreviewController(predict, [])
        app = ClassifierPreviewApp(controller, show_boot=False)
        self.assertIsInstance(app, CamolApp)
        async with app.run_test(size=(120, 36)) as pilot:
            await self.wait_for(pilot, lambda: controller.ready)
            prompt = app.query_one("#prompt", PromptArea)
            transcript = "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)
            self.assertEqual(transcript.count("CAMOL / CLASSIFIER PREVIEW"), 1)
            self.assertEqual(prompt.styles.background.hex, "#050B07")
            self.assertEqual(app.screen.styles.background.hex, "#030604")
            prompt.load_text("Review this pull request")
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: "review_changes" in str(app.query_one("#result-small", Static).render()))
            self.assertIn("ABSTAIN", str(app.query_one("#result-qwen", Static).render()))
            predict.assert_called_once_with("Review this pull request", "")
            prompt.load_text("/run")
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: "unavailable" in "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines))
            self.assertEqual(predict.call_count, 1)
            app.action_clear_preview()
            await self.wait_for(pilot, lambda: "Waiting" in str(app.query_one("#result-small", Static).render()))

    async def test_narrow_layout_and_preview_palette(self):
        controller = ClassifierPreviewController(Mock(return_value=[]), [])
        app = ClassifierPreviewApp(controller, show_boot=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.wait_for(pilot, lambda: controller.ready)
            panel = app.query_one("#classifier-panel")
            transcript = app.query_one("#transcript")
            self.assertGreaterEqual(panel.region.y, transcript.region.bottom)
            self.assertLessEqual(panel.region.bottom, app.query_one("#work-area").region.bottom)
            self.assertLessEqual(app.query_one("#prompt").region.bottom, app.size.height)
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/")
            await pilot.pause(.2)
            self.assertIsInstance(app.screen, SlashCommandScreen)
            self.assertTrue(all(c.command != "/run" for c in app.screen.commands))
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
