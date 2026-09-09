import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from camol.cli import main
from camol.schema import canonical_digest
from camol.ssh_protocol import SSHTarget


class RemoteCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.journal = self.root / "journal"
        self.profile = SSHTarget(
            name="test-target", host="host.example", port=22, login="owner",
            known_hosts=str(self.root / "known_hosts"), known_hosts_sha256=canonical_digest("known"),
            identity_file=str(self.root / "key-reference-only"), target_id="project",
            target_digest=canonical_digest("remote-policy-target"), run_id="run", plan_digest=canonical_digest("plan"),
            owner="owner", bridge_identity=dict(camol_version="0.3.0a1", python_executable="/usr/bin/python3",
                python_sha256=canonical_digest("python"), package_sha256=canonical_digest("package"), control_version=3),
            allowed_commands=("status", "drain"),
        )
        self.path = self.root / "target.json"
        self.path.write_text(json.dumps(self.profile.to_dict()))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["remote", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_validate_is_offline_and_rejects_ambiguous_or_option_shaped_target(self):
        code, output, error = self.invoke(["validate", "--target", str(self.path)])
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["contacts_remote"])
        self.assertFalse(self.journal.exists())
        self.path.write_text('{"owner":"intruder",' + json.dumps(self.profile.to_dict())[1:])
        code, _, error = self.invoke(["validate", "--target", str(self.path)])
        self.assertEqual(code, 2)
        self.assertIn("duplicate", error)
        document = dict(self.profile.to_dict(), host="-oProxyCommand=bad")
        self.path.write_text(json.dumps(document))
        code, output, _ = self.invoke(["validate", "--target", str(self.path)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["outcome"], "not_dispatched")

    def test_mutation_requires_explicit_opt_in_before_even_constructing_a_client(self):
        arguments = ["request", "--target", str(self.path), "--state-dir", str(self.journal), "--command", "drain"]
        with patch("camol.ssh_transport.SSHControlClient", side_effect=AssertionError("must not construct client")):
            code, output, _ = self.invoke(arguments + ["--by", "owner"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["outcome"], "not_dispatched")
            code, _, _ = self.invoke(arguments + ["--allow-mutation"])
            self.assertEqual(code, 2)
        self.assertFalse(self.journal.exists())

    def test_receipt_inspection_is_offline_and_noncreating(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("inspection must not start SSH")):
            code, output, error = self.invoke(["receipts", "--target", str(self.path), "--state-dir", str(self.journal)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), [])
        self.assertFalse(self.journal.exists())

    def test_explicit_request_facade_uses_data_params_and_reports_refusal(self):
        params = self.root / "params.json"
        params.write_text('{"limit":2}')
        with patch("camol.ssh_transport.SSHControlClient") as constructor:
            constructor.return_value.request = AsyncMock(return_value={"ok": False, "error": "fixture refusal"})
            code, output, _ = self.invoke(["request", "--target", str(self.path), "--state-dir", str(self.journal),
                                          "--command", "status", "--params", str(params), "--by", "owner"])
            constructor.return_value.request.assert_awaited_once_with("status", params={"limit": 2}, requested_by="owner")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output)["ok"])
        self.assertFalse(self.journal.exists())
