import unittest

from camol.hillclimb import compare_vectors


class HillClimbTests(unittest.TestCase):
    def test_large_improvement_cannot_hide_a_guardrail_regression(self):
        verdict = compare_vectors(
            baseline={"verified_steps_per_1k_tokens": 1.0, "failure_rate": 0.1},
            candidate={"verified_steps_per_1k_tokens": 4.0, "failure_rate": 0.3},
            dimensions=[
                {
                    "name": "verified_steps_per_1k_tokens",
                    "direction": "maximize",
                    "minimum_improvement": 0.2,
                },
                {
                    "name": "failure_rate",
                    "direction": "minimize",
                    "regression_tolerance": 0.01,
                },
            ],
        )
        self.assertFalse(verdict["promotable"])
        self.assertEqual([item["name"] for item in verdict["regressions"]], ["failure_rate"])


if __name__ == "__main__":
    unittest.main()
