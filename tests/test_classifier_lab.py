import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("classifier_lab", ROOT / "scripts/classifier_lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


class ClassifierLabTests(unittest.TestCase):
    def setUp(self):
        self.routes = [{"id": "review", "description": "review code"},
                       {"id": "build", "description": "write code"}]

    def fixture(self, value, callback):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(value)
            return callback(path)

    def test_close_scores_abstain_even_with_clear_top(self):
        result = lab.decision([1.0, 0.9], self.routes, 0.5, 0.15)
        self.assertEqual(result["top_route"], "review")
        self.assertIsNone(result["proposed_route"])
        self.assertEqual(result["reasons"], ["small_margin"])

    def test_threshold_abstains(self):
        result = lab.decision([1.0, 0.0], self.routes, 0.9, 0.0)
        self.assertEqual(result["reasons"], ["low_score"])

    def test_large_logits_are_numerically_stable(self):
        result = lab.decision([10000.0, 9990.0], self.routes, 0.55, 0.15)
        self.assertEqual(result["proposed_route"], "review")
        self.assertAlmostEqual(sum(row["score"] for row in result["scores"]), 1)

    def test_bad_logits_are_errors(self):
        for values in ([math.nan, 1], [1, math.inf], [1], [1, 2, 3]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                lab.decision(values, self.routes, 0.55, 0.15)

    def test_reserved_delimiters_rejected_in_prompt_and_state(self):
        for marker in lab.RESERVED:
            with self.assertRaises(ValueError):
                lab.input_text("hello " + marker)
            with self.assertRaises(ValueError):
                lab.input_text("hello", marker)

    def test_empty_prompt_rejected(self):
        for value in ("", "  ", None, 3):
            with self.assertRaises(ValueError):
                lab.input_text(value)

    def test_state_is_explicitly_separated(self):
        text = lab.input_text("Do it.", "Review PR 5")
        self.assertEqual(text, "Current task state:\nReview PR 5\n\nLatest user request:\nDo it.")

    def test_models_receive_their_training_label_order(self):
        self.assertEqual(lab.prepare_text("TEXT", ["SPORT", "Travel"], True),
                         "<<LABEL>>sport<<LABEL>>travel<<SEP>>TEXT")
        self.assertEqual(lab.prepare_text("TEXT", ["SPORT", "Travel"], False),
                         "TEXT<<LABEL>>sport<<LABEL>>travel<<SEP>>")

    def test_duplicate_descriptions_rejected_case_insensitively(self):
        routes = self.routes + [{"id": "another", "description": "REVIEW CODE"}]
        with self.assertRaises(ValueError):
            self.fixture(json.dumps({"routes": routes}), lab.load_labels)

    def test_missing_or_duplicate_ids_rejected(self):
        for routes in ([{}, {}], self.routes + [self.routes[0]]):
            with self.assertRaises(ValueError):
                self.fixture(json.dumps({"routes": routes}), lab.load_labels)

    def test_bad_case_labels_and_duplicate_cases_rejected(self):
        case = {"id": "one", "text": "review this", "expected": "review"}
        for text in (json.dumps(dict(case, expected="unknown")),
                     json.dumps(case) + "\n" + json.dumps(case)):
            with self.assertRaises(ValueError):
                self.fixture(text, lambda path: lab.load_cases(path, self.routes))

    def test_summary_counts_wrong_accepted_and_abstentions_separately(self):
        rows = [
            dict(case_id="a", top_route="review", proposed_route="review", expected="review", abstained=False, latency_ms=10),
            dict(case_id="b", top_route="review", proposed_route="review", expected="build", abstained=False, latency_ms=20),
            dict(case_id="c", top_route="build", proposed_route=None, expected="build", abstained=True, latency_ms=30),
        ]
        report = lab.summarize(rows)
        self.assertEqual(report["wrong_accepted"], 1)
        self.assertEqual(report["accepted_accuracy"], 0.5)
        self.assertAlmostEqual(report["top1_accuracy"], 2 / 3)
        self.assertAlmostEqual(report["coverage"], 2 / 3)
        self.assertEqual(report["confusion"]["build"], {"review": 1, "build": 1})

    def test_no_accepted_routes_has_no_accuracy_not_perfect_accuracy(self):
        row = dict(case_id="a", top_route="review", proposed_route=None, expected="build", abstained=True, latency_ms=10)
        report = lab.summarize([row])
        self.assertIsNone(report["accepted_accuracy"])
        self.assertEqual(report["coverage"], 0)

    def test_repeats_are_counted_as_same_case(self):
        row = dict(case_id="a", top_route="review", proposed_route="review", expected="review", abstained=False, latency_ms=10)
        report = lab.summarize([row, dict(row, top_route="build")])
        self.assertEqual(report["unique_cases"], 1)
        self.assertEqual(report["repeat_disagreements"], 1)

    def test_default_corpus_is_valid(self):
        labels = lab.load_labels(lab.DATA / "routes.json")
        cases = lab.load_cases(lab.DATA / "cases.jsonl", labels["routes"])
        self.assertEqual(len(cases), 36)
        self.assertEqual({r["id"] for r in labels["routes"]}, {c["expected"] for c in cases})

    def test_terminal_controls_are_escaped(self):
        self.assertNotIn("\x1b", lab.safe_display("\x1b[2J"))
        self.assertNotIn("\n", lab.safe_display("hello\nworld"))


if __name__ == "__main__":
    unittest.main()
