import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.benchmark import BenchmarkError, BenchmarkTrial, compare_trials
from camol.schema import canonical_digest


ROOT = Path(__file__).resolve().parents[1]
DIGEST = canonical_digest({"matched": True})


def trial(arm, **changes):
    payload = {
        "schema": "camol.benchmark_trial", "schema_version": 1,
        "trial_id": "trial-" + arm, "arm": arm, "task_id": "task-1",
        "task_digest": DIGEST, "source_revision": "abc123",
        "evaluator_digest": DIGEST, "model": "claude-fable-5",
        "model_version": "resolved-version", "effort": "high",
        "tool_policy_digest": DIGEST, "budget_digest": DIGEST,
        "outcome": "accepted", "accepted_behavior": 1,
        "invariant_violations": 0, "regressions": 0,
        "tokens": 1000, "cost_usd_micros": 20000, "elapsed_ms": 5000,
        "retries": 0, "human_interventions": 1, "tool_calls": 4,
        "recovery_success": True, "evidence_complete": True,
        "recorded_at": "2026-09-03T00:00:00Z",
    }
    payload.update(changes)
    return BenchmarkTrial.from_dict(payload)


class BenchmarkTests(unittest.TestCase):
    def test_comparison_is_matched_vector_valued_and_never_claims_one_pair_is_significant(self):
        direct = trial("claude_direct")
        camol = trial(
            "camol_one", tokens=900, elapsed_ms=6000, tool_calls=6,
            human_interventions=0,
        )
        report = compare_trials(direct, camol)
        self.assertEqual(report["delta_camol_minus_direct"]["tokens"], -100)
        self.assertEqual(report["delta_camol_minus_direct"]["elapsed_ms"], 1000)
        self.assertEqual(report["guardrails"]["camol_invariant_violations"], 0)
        self.assertFalse(report["statistical_claim"])

    def test_any_changed_control_variable_refuses_comparison(self):
        direct = trial("claude_direct")
        for field, value in (
            ("source_revision", "different"), ("effort", "low"),
            ("budget_digest", canonical_digest({"other": True})),
        ):
            payload = trial("camol_one").to_dict()
            payload[field] = value
            with self.assertRaisesRegex(BenchmarkError, "not matched"):
                compare_trials(direct, BenchmarkTrial.from_dict(payload))

    def test_cli_emits_the_same_transparent_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct = root / "direct.json"
            camol = root / "camol.json"
            direct.write_text(json.dumps(trial("claude_direct").to_dict()), encoding="utf-8")
            camol.write_text(json.dumps(trial("camol_one").to_dict()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable, "-m", "camol", "bench-compare",
                    "--direct", str(direct), "--camol", str(camol),
                ],
                cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["statistical_claim"])


if __name__ == "__main__":
    unittest.main()
