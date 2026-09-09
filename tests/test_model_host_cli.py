import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camol.cli import main
from camol.model_host import LlamaCppModelHost, ModelHostPlan, ModelHostUnload
from camol.schema import canonical_digest


class ModelHostCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "host"
        self.plan = ModelHostPlan(
            plan_id="fixture-model", owner="owner", model_store_root=str(self.root / "models"),
            download_plan_digest=canonical_digest("download"), logical_path="fixture.gguf",
            artifact_digest=canonical_digest("model"), artifact_size_bytes=32,
            executable=str(self.root / "not-installed-llama-server"), executable_digest=canonical_digest("binary"),
            port=18177, lifetime_seconds=60,
        )
        self.path = self.root / "host-plan.json"
        self.path.write_text(json.dumps(self.plan.to_dict()))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["model-host", *arguments, "--root", str(self.state)])
        return code, output.getvalue(), errors.getvalue()

    def test_validation_and_read_only_inspection_never_create_host_state(self):
        code, output, error = self.invoke(["validate", "--plan", str(self.path)])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["plan_digest"], self.plan.digest())
        self.assertFalse(result["starts_process"])
        self.assertFalse(result["proves_inference"])
        self.assertFalse(self.state.exists())
        for action in ("inventory", "status", "events"):
            code, _, _ = self.invoke([action, "--plan-digest", self.plan.digest()])
            self.assertEqual(code, 2)
            self.assertFalse(self.state.exists())

    def test_ambiguous_contract_or_missing_load_authority_has_no_side_effect(self):
        self.path.write_text('{"owner":"intruder",' + json.dumps(self.plan.to_dict())[1:])
        code, _, error = self.invoke(["prepare", "--plan", str(self.path)])
        self.assertEqual(code, 2)
        self.assertIn("duplicate", error)
        self.assertFalse(self.state.exists())
        code, _, error = self.invoke(["load", "--plan-digest", self.plan.digest(), "--by", "owner"])
        self.assertEqual(code, 2)
        self.assertIn("operation-id", error)
        self.assertFalse(self.state.exists())

    def test_prepare_approve_and_explicit_execution_arguments_are_separate(self):
        code, _, error = self.invoke(["prepare", "--plan", str(self.path)])
        self.assertEqual(code, 0, error)
        with LlamaCppModelHost(self.state, read_only=True) as host:
            self.assertIsNone(host.status(self.plan.digest())["approved_by"])
        code, _, error = self.invoke(["approve", "--plan-digest", self.plan.digest(), "--by", "intruder"])
        self.assertEqual(code, 2)
        code, _, error = self.invoke(["approve", "--plan-digest", self.plan.digest(), "--by", "owner"])
        self.assertEqual(code, 0, error)
        with patch.object(LlamaCppModelHost, "load", return_value={"status": "loaded", "loaded": "observed"}) as load:
            code, _, error = self.invoke(["load", "--plan-digest", self.plan.digest(), "--by", "owner", "--operation-id", "one-load"])
        self.assertEqual(code, 0, error)
        load.assert_called_once_with(self.plan.digest(), "owner", operation_id="one-load")
        request = ModelHostUnload("one-stop", self.plan.digest(), "one-load", "owner")
        path = self.root / "stop.json"
        path.write_text(json.dumps(request.to_dict()))
        with patch.object(LlamaCppModelHost, "unload", return_value={"status": "unloaded", "loaded": "no"}) as unload:
            code, _, error = self.invoke(["unload", "--request", str(path), "--by", "owner", "--digest", request.digest()])
        self.assertEqual(code, 0, error)
        unload.assert_called_once_with(request, "owner", approve_digest=request.digest())

    def test_execution_exit_status_requires_confirmed_effect(self):
        self.invoke(["prepare", "--plan", str(self.path)])
        self.invoke(["approve", "--plan-digest", self.plan.digest(), "--by", "owner"])
        request = ModelHostUnload("stop-uncertain", self.plan.digest(), "load-uncertain", "owner")
        path = self.root / "stop-uncertain.json"
        path.write_text(json.dumps(request.to_dict()))
        cases = (
            ("load", ["load", "--plan-digest", self.plan.digest(), "--by", "owner", "--operation-id", "load-uncertain"]),
            ("unload", ["unload", "--request", str(path), "--by", "owner", "--digest", request.digest()]),
        )
        for method, args in cases:
            for status in ("failed", "unknown", "cancelled", "expired", "loading", "stopping"):
                receipt = {"status": status, "loaded": "unverified"}
                with self.subTest(method=method, status=status), patch.object(LlamaCppModelHost, method, return_value=receipt):
                    code, output, _ = self.invoke(args)
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(output), receipt)
        with patch.object(LlamaCppModelHost, "load", return_value={"status": "loaded", "loaded": "unverified"}):
            code, _, _ = self.invoke(cases[0][1])
            self.assertEqual(code, 2)
