import contextlib
import io
import json
import unittest

from camol.cli import build_parser, main
from tests import test_retention


class RetentionCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_retention.RetentionInventoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.policy_path = self.fixture.root / "inspection-policy.json"
        self.policy_path.write_text(json.dumps(self.fixture.policy.to_dict()))
        self.argv = ["retention", "inspect", "--state-dir", str(self.fixture.state),
                     "--db", str(self.fixture.database), "--run-id", self.fixture.run_id,
                     "--policy", str(self.policy_path)]

    def invoke(self, argv):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(argv)
        return code, output.getvalue(), error.getvalue()

    def test_inspection_is_nonmutating_content_free_and_digest_bound(self):
        self.fixture.artifact(b"private content never rendered")
        before = self.fixture.fingerprint()
        code, output, error = self.invoke(self.argv)
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["inventory_digest"], self.fixture.inspect().digest())
        self.assertFalse(report["inventory"]["deletion_authorized"])
        self.assertNotIn("private content", output)
        self.assertEqual(before, self.fixture.fingerprint())

    def test_missing_state_and_mismatched_run_refuse_without_creation(self):
        for flag, value in (("--state-dir", str(self.fixture.root / "absent")),
                            ("--run-id", "another-run")):
            argv = list(self.argv)
            argv[argv.index(flag) + 1] = value
            code, output, error = self.invoke(argv)
            self.assertEqual((code, output), (2, ""))
            self.assertTrue(error.startswith("camol: "))
        self.assertFalse((self.fixture.root / "absent").exists())

    def test_policy_duplicate_keys_and_purge_action_are_not_accepted(self):
        self.policy_path.write_text('{"schema":"secret-value","schema":"duplicate"}')
        code, output, error = self.invoke(self.argv)
        self.assertEqual((code, output), (2, ""))
        self.assertIn("duplicate", error)
        self.assertNotIn("secret-value", error)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            build_parser().parse_args(["retention", "purge"])
        self.assertEqual(stopped.exception.code, 2)
