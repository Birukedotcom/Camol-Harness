import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

from camol import Harness
from camol.artifacts import RunArchive
from camol.cli import main
from camol.recovery import RecoveryError, generate_recovery_key, load_recovery_key
from camol.run_recovery import (MANIFEST_MAGIC, _crypto, _open, _seal,
                                export_run_recovery, plan_run_recovery,
                                restore_run_recovery, verify_run_recovery)
from camol.schema import canonical_digest
from camol.supervisor import SupervisorError
from tests import test_api as api_fixture
from tests.test_workspace import git

ROOT = api_fixture.ROOT


@unittest.skipUnless(AESGCM, "optional recovery encryption dependency")
class RunRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = api_fixture.HarnessApiTests()
        cls.fixture.setUp()
        cls.source, cls.state_dir = cls.fixture.workspace, cls.fixture.state_dir
        cls.shared = cls.source.parent
        cls.key = os.urandom(32)
        cls.baseline = cls.shared / "baseline-archive"
        with Harness(cls.source, cls.state_dir) as harness:
            state = harness.prepare(ROOT / "examples/local-n-box-runbook.json")
            harness.approve(by="owner", digest=state["plan_digest"])
            cls.final = harness.run()
            cls.events = harness.events()
            cls.review = harness.recovery_plan()
            cls.result = harness.export_recovery(cls.baseline, by="owner",
                review_digest=cls.review["review_digest"], allow_encrypted_raw=True, key=cls.key)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive, self.output = self.root / "archive", self.root / "restored"

    def clone_archive(self):
        shutil.copytree(self.baseline, self.archive)

    def alter_manifest(self, mutate):
        path = self.archive / "manifest.camol"
        cipher = _crypto(self.key)
        value = json.loads(_open(cipher, path.read_bytes(), MANIFEST_MAGIC))
        mutate(value)
        path.write_bytes(_seal(cipher, json.dumps(value).encode(), MANIFEST_MAGIC))

    def export(self, **changes):
        args = dict(source=self.source, state_dir=self.state_dir, events=self.events,
                    by="owner", review_digest=self.review["review_digest"],
                    allow_encrypted_raw=True, key=self.key, output=self.archive)
        args.update(changes)
        return export_run_recovery(**args)

    def test_completed_n_box_run_restores_every_recorded_candidate_and_commit_without_inputs(self):
        hidden_source, hidden_state = self.shared / "hidden-source", self.shared / "hidden-state"
        self.source.rename(hidden_source)
        self.state_dir.rename(hidden_state)
        try:
            result = restore_run_recovery(archive=self.baseline, key=self.key, output=self.output)
        finally:
            hidden_source.rename(self.source)
            hidden_state.rename(self.state_dir)
        self.assertEqual(result, self.result)
        self.assertEqual(RunArchive.replay(self.output / "ledger"), self.final)
        self.assertEqual(result["restored_captures"], 4)
        self.assertEqual(result["restored_commits"], 4)
        head = self.output / "accepted" / self.final["integration_head"]
        self.assertEqual(git(head, "rev-parse", "HEAD"), self.final["integration_head"])
        self.assertEqual(git(head, "status", "--porcelain"), "")
        self.assertEqual(git(head, "remote"), "")
        self.assertFalse(result["resume_authorized"])
        self.assertFalse(result["cleanup_authorized"])
        self.assertFalse(result["operational_restore"])
        self.assertFalse((self.output / "control").exists())

    def test_review_is_deterministic_without_git_or_crypto_and_does_not_claim_readiness(self):
        with patch("camol.run_recovery._Git.call", side_effect=AssertionError("no Git")), \
                patch("camol.run_recovery._crypto", side_effect=AssertionError("no key")):
            self.assertEqual(plan_run_recovery(events=self.events), self.review)
        self.assertFalse(self.review["content_verified"])
        self.assertEqual(self.review["limits"]["code_parts"], 1024)

    def test_owner_digest_and_raw_opt_in_are_all_required_before_work(self):
        for change in ({"by": "different"}, {"review_digest": "sha256:" + "0" * 64}, {"allow_encrypted_raw": False}):
            with self.subTest(change=change), patch("camol.run_recovery._Git.call", side_effect=AssertionError("no Git")):
                with self.assertRaises(RecoveryError):
                    self.export(**change)
            self.assertFalse(self.archive.exists())

    def test_nonterminal_run_is_not_presented_as_a_completed_backup(self):
        index = next(index for index, event in enumerate(self.events) if event["type"] == "RUN_COMPLETED")
        with self.assertRaisesRegex(RecoveryError, "completed"):
            plan_run_recovery(events=self.events[:index])

    def test_changed_review_during_export_prevents_manifest_publication(self):
        with self.assertRaisesRegex(RecoveryError, "advanced"):
            self.export(recheck=lambda: dict(self.review, review_digest="sha256:" + "0" * 64))
        self.assertFalse((self.archive / "manifest.camol").exists())
        self.assertEqual(plan_run_recovery(events=self.events), self.review)

    def test_late_ordinary_evidence_mutation_prevents_publication(self):
        def mutate_after_verification():
            with (self.archive / "ledger" / "events.jsonl").open("ab") as stream:
                stream.write(b"changed")
            return self.review
        with self.assertRaises(RecoveryError):
            self.export(recheck=mutate_after_verification)
        self.assertFalse((self.archive / "manifest.camol").exists())

    def test_owner_lock_blocks_competing_backup_cli_without_starting_any_workers(self):
        output, errors = io.StringIO(), io.StringIO()
        with Harness(self.source, self.state_dir), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["recovery", "plan-run", "--source", str(self.source),
                         "--state-dir", str(self.state_dir), "--run-id", self.review["run_id"]])
        self.assertEqual(code, 2)
        self.assertIn("another Camol supervisor", errors.getvalue())
        with Harness(self.source, self.state_dir) as harness:
            self.assertEqual(harness.events(), self.events)

    def test_wrong_key_does_not_create_plaintext_output(self):
        with self.assertRaisesRegex(RecoveryError, "authentication"):
            restore_run_recovery(archive=self.baseline, key=os.urandom(32), output=self.output)
        self.assertFalse(self.output.exists())

    def test_authenticated_manifest_cannot_omit_a_recorded_code_part(self):
        self.clone_archive()
        self.alter_manifest(lambda value: value["parts"].pop(next(iter(value["parts"]))))
        with patch("camol.run_recovery._Git.call", side_effect=AssertionError("no Git")):
            with self.assertRaisesRegex(RecoveryError, "inventory"):
                restore_run_recovery(archive=self.archive, key=self.key, output=self.output)
        self.assertFalse((self.output / "recovery-result.json").exists())

    def test_authenticated_manifest_cannot_change_ledger_derived_review(self):
        self.clone_archive()
        self.alter_manifest(lambda value: value["plan"]["accepted_revisions"].clear())
        with self.assertRaisesRegex(RecoveryError, "coverage"):
            restore_run_recovery(archive=self.archive, key=self.key, output=self.output)
        self.assertFalse(self.output.exists())

    def test_code_byte_tampering_and_linked_member_refuse_completion(self):
        self.clone_archive()
        first = next((self.archive / "captures").iterdir()) / "recovery.camol"
        original = first.read_bytes()
        first.write_bytes(original + b"changed")
        with self.assertRaises(RecoveryError):
            restore_run_recovery(archive=self.archive, key=self.key, output=self.output)
        self.assertFalse((self.output / "recovery-result.json").exists())
        first.unlink()
        outside = self.root / "outside.camol"
        outside.write_bytes(original)
        first.symlink_to(outside)
        with self.assertRaises(RecoveryError):
            verify_run_recovery(archive=self.archive, key=self.key)
        self.assertEqual(outside.read_bytes(), original)

    def test_missing_cas_never_creates_an_empty_replacement_store(self):
        root = self.root / "absent-state"
        root.mkdir()
        with self.assertRaisesRegex(RecoveryError, "existing ordinary"):
            self.export(state_dir=root)
        self.assertEqual(list(root.iterdir()), [])
        self.assertFalse(self.archive.exists())

    def test_combined_restored_content_budget_is_not_reset_for_each_workspace(self):
        from camol.run_recovery import _materialize
        budgets = []
        def exhaust_after_first(*args, **kwargs):
            budgets.append(kwargs["resource_budget"])
            result = _materialize(*args, **kwargs)
            kwargs["resource_budget"]["bytes"] = 0
            return result
        with patch("camol.run_recovery._materialize", exhaust_after_first):
            with self.assertRaisesRegex(RecoveryError, "combined recovery"):
                restore_run_recovery(archive=self.baseline, key=self.key, output=self.output)
        self.assertEqual(len(budgets), 2)
        self.assertIs(budgets[0], budgets[1])
        self.assertFalse((self.output / "recovery-result.json").exists())

    def test_cli_exact_review_export_verify_and_restore_preserve_original_ledger(self):
        keys = self.root / "keys"
        generate_recovery_key(output=keys)
        key_file = keys / "key.bin"
        common = ["--source", str(self.source), "--state-dir", str(self.state_dir), "--run-id", self.review["run_id"]]
        out, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["recovery", "plan-run", *common]), 0)
        self.assertEqual(json.loads(out.getvalue()), self.review)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["recovery", "export-run", *common, "--by", "owner", "--review-digest", self.review["review_digest"], "--allow-encrypted-raw", "--key-file", str(key_file), "--output", str(self.archive)]), 0, errors.getvalue())
            self.assertEqual(main(["recovery", "verify-run", "--archive", str(self.archive), "--key-file", str(key_file)]), 0, errors.getvalue())
            self.assertEqual(main(["recovery", "restore-run", "--archive", str(self.archive), "--key-file", str(key_file), "--output", str(self.output)]), 0, errors.getvalue())
        self.assertNotIn(load_recovery_key(path=key_file).hex(), out.getvalue() + errors.getvalue())
        self.assertEqual(RunArchive.replay(self.output / "ledger"), self.final)
        with Harness(self.source, self.state_dir) as harness:
            self.assertEqual(harness.events(), self.events)


@unittest.skipUnless(AESGCM, "optional recovery encryption dependency")
class RevisionRunRecoveryTests(unittest.TestCase):
    def test_real_refinement_successor_and_ancestor_evidence_restore_together(self):
        from tests.test_evaluation import EvaluationLoopTests
        from tests.test_gate_runtime import v5_plan
        from camol.gate_runtime import acceptance_digest

        fixture = EvaluationLoopTests()
        fixture.setUp()
        try:
            with Harness(fixture.source, fixture.state) as harness:
                state = harness.prepare(v5_plan("first"))
                harness.approve(by="human-owner", digest=state["plan_digest"])
                first = harness.run()
                harness.accept(by="human-owner", outcome_digest=acceptance_digest(first))
                proposal = harness.propose_revision(v5_plan("successor"), reason="reviewed successor")
                harness.apply_revision(by="human-owner", proposal_digest=proposal["proposal_digest"])
                final = harness.run()
                final = harness.accept(by="human-owner", outcome_digest=acceptance_digest(final))
                review = harness.recovery_plan()
                self.assertEqual(set(review["streams"]), {"first", "successor"})
                key = os.urandom(32)
                archive = fixture.root / "run-backup"
                result = harness.export_recovery(archive, by="human-owner", review_digest=review["review_digest"], allow_encrypted_raw=True, key=key)
            restored = fixture.root / "restored"
            self.assertEqual(restore_run_recovery(archive=archive, key=key, output=restored), result)
            manifest, events, lineage = RunArchive.verify_snapshot(restored / "ledger")
            self.assertEqual(set(lineage), {"first"})
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(RunArchive.replay(restored / "ledger"), final)
            self.assertGreaterEqual(result["restored_captures"], 4)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
