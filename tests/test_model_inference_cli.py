import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camol.cli import main
from camol.model_inference import ModelInference, ModelInferencePlan, prompt_file_identity
from camol.schema import canonical_digest
from tests import test_model_inference as fixtures


class ModelInferenceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host_root = self.root / "absent-host"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("PRIVATE_CLI_PROMPT_0b21\n")
        now = datetime.now(timezone.utc)
        self.plan = ModelInferencePlan(
            "cli-one", canonical_digest("host"), "load-one", "owner", "camol-" + "a" * 48,
            **prompt_file_identity(self.prompt), max_output_tokens=4, timeout_seconds=3,
            issued_at=(now - timedelta(seconds=1)).isoformat(),
            expires_at=(now + timedelta(seconds=60)).isoformat(),
        )
        self.document = self.root / "request.json"
        self.document.write_text(json.dumps(self.plan.to_dict()))

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["model-inference", *arguments, "--root", str(self.host_root)])
        return code, out.getvalue(), err.getvalue()

    def test_passive_contract_and_prompt_fingerprints_never_create_state_or_reveal_text(self):
        for action, args in (
            ("fingerprint", ["--prompt-file", str(self.prompt)]),
            ("validate", ["--plan", str(self.document), "--prompt-file", str(self.prompt)]),
        ):
            code, output, error = self.invoke([action, *args])
            self.assertEqual(code, 0, error)
            self.assertFalse(json.loads(output)["sends_prompt"])
            self.assertNotIn("PRIVATE_CLI_PROMPT_0b21", output + error)
            self.assertNotIn(str(self.prompt), output)
            self.assertFalse(self.host_root.exists())
        for action in ("status", "events", "inventory"):
            args = [] if action == "inventory" else ["--plan-digest", self.plan.digest()]
            code, _, _ = self.invoke([action, *args])
            self.assertEqual(code, 2)
            self.assertFalse(self.host_root.exists())

    def test_missing_or_ambiguous_authority_fails_before_opening_store(self):
        cases = [
            ["infer", "--plan-digest", self.plan.digest(), "--by", "owner"],
            ["infer", "--plan-digest", self.plan.digest(), "--prompt-file", str(self.prompt)],
            ["approve", "--by", "owner"],
            ["prepare"], ["fingerprint"],
            ["status", "--plan-digest", self.plan.digest(), "--show-response"],
            ["infer", "--plan", str(self.document), "--prompt-file", str(self.prompt)],
        ]
        with patch.object(ModelInference, "__init__", side_effect=AssertionError("must not open store")):
            for args in cases:
                with self.subTest(args=args):
                    code, _, _ = self.invoke(args)
                    self.assertEqual(code, 2)
        self.document.write_text('{"owner":"intruder",' + json.dumps(self.plan.to_dict())[1:])
        code, _, error = self.invoke(["prepare", "--plan", str(self.document)])
        self.assertEqual(code, 2)
        self.assertIn("duplicate", error)
        self.assertFalse(self.host_root.exists())


class ModelInferenceCliLifecycleTests(unittest.TestCase):
    setUp = fixtures.ModelInferenceTests.setUp
    tearDown = fixtures.ModelInferenceTests.tearDown
    plan = fixtures.ModelInferenceTests.plan
    approved = fixtures.ModelInferenceTests.approved
    start = fixtures.ModelInferenceTests.start
    request = fixtures.ModelInferenceTests.request
    posts = fixtures.ModelInferenceTests.posts

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["model-inference", *arguments, "--root", str(self.host_root)])
        return code, out.getvalue(), err.getvalue()

    def prepare_cli(self, plan):
        document = self.base / "inference-plan.json"
        document.write_text(json.dumps(plan.to_dict()))
        code, _, err = self.invoke(["prepare", "--plan", str(document), "--prompt-file", str(self.prompt)])
        self.assertEqual(code, 0, err)
        code, _, err = self.invoke(["approve", "--plan-digest", plan.digest(), "--by", "owner"])
        self.assertEqual(code, 0, err)

    def test_real_cli_dispatch_is_one_shot_and_response_display_is_explicit(self):
        plan = self.start()
        self.prepare_cli(plan)
        arguments = ["infer", "--plan-digest", plan.digest(), "--by", "owner", "--prompt-file", str(self.prompt)]
        code, output, error = self.invoke(arguments)
        self.assertEqual(code, 0, error)
        self.assertIsNone(json.loads(output)["response_text"])
        self.assertNotIn("PRIVATE_RESPONSE_82a1", output)
        self.assertEqual(len(self.posts()), 1)
        code, output, _ = self.invoke([*arguments, "--show-response"])
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["response_text"])
        self.assertEqual(len(self.posts()), 1)
        another = self.request("cli-two")
        self.prepare_cli(another)
        code, output, err = self.invoke(["infer", "--plan-digest", another.digest(), "--by", "owner",
                                       "--prompt-file", str(self.prompt), "--show-response"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["response_text"], "PRIVATE_RESPONSE_82a1")
        self.assertEqual(len(self.posts()), 2)
        for action in ("status", "events", "inventory"):
            args = [] if action == "inventory" else ["--plan-digest", another.digest()]
            code, output, err = self.invoke([action, *args])
            self.assertEqual(code, 0, err)
            self.assertNotIn("PRIVATE_RESPONSE_82a1", output)
            self.assertNotIn("PRIVATE_PROMPT_59f3", output)

    def test_uncertain_execution_is_nonzero_and_is_not_retried(self):
        plan = self.start("httpfail")
        self.prepare_cli(plan)
        args = ["infer", "--plan-digest", plan.digest(), "--by", "owner", "--prompt-file", str(self.prompt)]
        for _ in range(2):
            code, output, err = self.invoke(args)
            self.assertEqual(code, 2, err)
            self.assertEqual(json.loads(output)["status"], "unknown")
        self.assertEqual(len(self.posts()), 1)

    def test_nonterminal_and_failed_receipts_never_report_success(self):
        self.start()
        args = ["infer", "--plan-digest", canonical_digest("request"), "--by", "owner", "--prompt-file", str(self.prompt)]
        for status in ("prepared", "approved", "in_flight", "failed", "cancelled", "expired", "unknown"):
            with self.subTest(status=status), patch.object(ModelInference, "infer", return_value={"status": status, "response_text": None}):
                code, _, _ = self.invoke(args)
                self.assertEqual(code, 2)
