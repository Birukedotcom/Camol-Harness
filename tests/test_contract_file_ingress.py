"""Exercise strict JSON at the public paths, not only the decoder helper."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from camol.cli import main
from camol.providers import ProviderError, load_model_profile
from camol.runbook import load_runbook, validate_runbook
from camol.schema import SchemaError


ROOT = Path(__file__).resolve().parents[1]


class ContractFileIngressTests(unittest.TestCase):
    def test_duplicate_runbook_authority_is_rejected_before_state_creation(self):
        document = json.loads((ROOT / "examples/local-n-box-runbook.json").read_text())
        expected = validate_runbook(document)
        normal = json.dumps(document)
        duplicates = (
            '{"run":{"id":"different-authority"},' + normal[1:],
            normal.replace(json.dumps(document["run"]), '{"id":"different-authority",' + json.dumps(document["run"])[1:], 1),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path, database = Path(temporary) / "plan.json", Path(temporary) / "never-created.sqlite3"
            for raw in duplicates:
                self.assertEqual(validate_runbook(json.loads(raw)), expected)  # old last-value behavior
                path.write_text(raw)
                with self.assertRaisesRegex(SchemaError, "duplicate"):
                    load_runbook(path)
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    self.assertEqual(main(["init", str(path), "--db", str(database)]), 2)
                self.assertIn("duplicate", errors.getvalue())
                self.assertFalse(database.exists())

    def test_profile_loader_rejects_duplicate_fields_not_just_unknown_fields(self):
        profile = load_model_profile(ROOT, "@camol/claude-fable-5-1")
        normal = json.dumps(profile.to_dict())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text('{"max_run_usd_cents":0,' + normal[1:])
            self.assertEqual(json.loads(path.read_text()), profile.to_dict())
            with self.assertRaises(ProviderError):
                load_model_profile(Path(temporary), path.name)

    def test_download_plan_ambiguity_fails_before_model_store_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "download.json"
            path.write_text('{"owner":"a","owner":"b"}')
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = main(["models", "prepare", "--plan", str(path), "--root", str(root / "store")])
            self.assertEqual(code, 2)
            self.assertIn("duplicate", errors.getvalue())
            self.assertFalse((root / "store").exists())
