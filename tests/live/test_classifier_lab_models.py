"""Opt-in, cached-model checks; these never download weights.

CAMOL_CLASSIFIER_LIVE=1 .camol/classifier-lab/runtime/bin/python -m unittest \
    tests.live.test_classifier_lab_models -v
"""

import os
import unittest

from tests.test_classifier_lab import lab


@unittest.skipUnless(os.environ.get("CAMOL_CLASSIFIER_LIVE") == "1", "requires classifier lab setup and explicit opt-in")
class ClassifierModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        cls.models = [lab.Classifier(name) for name in lab.MODELS]

    def test_content_and_reversed_label_order(self):
        report = lab.doctor(self.models)
        self.assertTrue(report["passed"], report["checks"])

    def test_no_silent_truncation(self):
        routes = [{"id": name, "description": name} for name in ("sport", "travel")]
        for model in self.models:
            with self.subTest(model=model.name), self.assertRaisesRegex(ValueError, "nothing was truncated"):
                model.predict("long input " * 600, routes)

    def test_qwen_attention_preserves_future_context_and_masks_padding(self):
        import torch
        from transformers.cache_utils import DynamicCache

        encoder = self.models[1].model.model.encoder_model
        hidden = torch.zeros(1, 3, encoder.config.hidden_size)
        padding = torch.tensor([[1, 1, 0]])
        mask = encoder._update_causal_mask(padding, hidden, torch.arange(3), None, False)
        self.assertEqual(mask[0, 0, 0, 1].item(), 0.0)
        self.assertLess(mask[0, 0, 0, 2].item(), -1e20)
        cache = DynamicCache()
        cache.update(torch.zeros(1, 1, 1, 8), torch.zeros(1, 1, 1, 8), 0)
        with self.assertRaisesRegex(ValueError, "cached decoding"):
            encoder._update_causal_mask(padding, hidden, torch.arange(3), cache, False)


if __name__ == "__main__":
    unittest.main()
