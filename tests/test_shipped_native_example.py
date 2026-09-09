"""Execute the shipped native smoke oracle without a provider or account."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.runbook import load_runbook


ROOT = Path(__file__).resolve().parents[1]


class NativeExampleOracleTests(unittest.TestCase):
    def setUp(self):
        self.runbook = load_runbook(ROOT / "examples/claude-fable-runbook.json")
        self.temporary = tempfile.TemporaryDirectory(prefix="camol-native-oracle-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        command = self.runbook["tasks"][0]["verification"][0]["argv"]
        self.assertEqual(command[:2], ["python3", "-c"])
        self.command = [sys.executable] + command[1:]

    def verify(self, contents):
        if contents is not None:
            (self.workspace / "CAMOL_RESULT.md").write_text(contents, encoding="utf-8")
        return subprocess.run(self.command, cwd=self.workspace, capture_output=True, text=True, timeout=5)

    def test_the_requested_single_line_with_real_newline_passes(self):
        result = self.verify("camol provider adapter proof\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_literal_escape_characters_are_not_the_requested_line(self):
        result = self.verify("camol provider adapter proof\\n")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_incorrect_or_extra_content_fails(self):
        for contents in (None, "wrong\n", "camol provider adapter proof\nextra\n"):
            with self.subTest(contents=contents):
                self.assertNotEqual(self.verify(contents).returncode, 0)

    def test_oracle_and_work_goal_remain_in_the_reviewed_runbook(self):
        task = self.runbook["tasks"][0]
        self.assertIn("camol provider adapter proof", task["goal"])
        self.assertEqual(self.runbook["agents"][0]["adapter"]["kind"], "claude_cli")
        self.assertEqual(self.runbook["run"]["max_concurrency"], 1)
        self.assertEqual(self.runbook["run"]["token_policy"]["max_turns_per_task"], 1)
        # A verifier regression does not justify silently increasing provider use.
        self.assertEqual(self.runbook["run"]["token_policy"]["max_total_tokens"], 32000)
