from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import camol
from camol.doctor import DoctorOptions, run_doctor
from camol.evaluation import evaluator_identity
from camol.handoff_doctor import inspect_handoff
from camol.schema import canonical_digest
from tests.test_doctor import DoctorFixture, snapshot
from tests import test_source_handoff as source_fixture


class ScopedDoctorTests(DoctorFixture):
    def test_exact_scope_preserves_full_plan_and_does_not_fallback_to_other_worker(self):
        whole, _ = self.doctor()
        scoped, text = self.doctor(task_id="inventory", agent_id="builder")
        self.assertEqual(scoped.exit_code, 0, text)
        self.assertEqual(scoped.payload["schema_version"], 2)
        self.assertEqual(scoped.payload["plan_digest"], whole.payload["plan_digest"])
        self.assertEqual(scoped.payload["evaluator_digest"], whole.payload["evaluator_digest"])
        self.assertEqual(scoped.payload["scope"], dict(task_ids=["inventory"], agent_ids=["builder"], verdict_basis="selected_subjects_only"))
        self.assertEqual([item["task_id"] for item in scoped.payload["tasks"]], ["inventory"])
        self.assertEqual([item["agent_id"] for item in scoped.payload["tasks"][0]["candidates"]], ["builder"])
        denied, _ = self.doctor(task_id="frame", agent_id="builder")
        self.assertEqual(denied.exit_code, 2)
        self.assertFalse(denied.payload["tasks"][0]["candidates"])
        self.assertEqual(denied.payload["tasks"][0]["waiting"][0]["code"], "CAPACITY_EXHAUSTED")

    def test_invalid_scope_is_rejected_before_probes(self):
        from camol.probes import default_registry
        registry = default_registry()
        with patch.object(registry, "shared_probes", side_effect=AssertionError("no probe")):
            for values in (dict(task_id="unknown"), dict(agent_id="unknown"), dict(task_id=[]), dict(agent_id=True)):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    self.doctor(registry=registry, **values)

    def test_nonselected_worker_is_not_probed_and_scoped_cli_is_explicit(self):
        from camol.cli import main
        from camol.probes import default_registry
        registry = default_registry()
        with patch.object(registry, "agent_probes", wraps=registry.agent_probes) as called:
            self.doctor(registry=registry, task_id="inventory", agent_id="builder")
        self.assertEqual([call.args[0]["id"] for call in called.call_args_list], ["builder"])
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = main(["doctor", str(self.runbook), "--workspace", str(self.repo), "--state-dir", str(self.state),
                         "--task-id", "inventory", "--agent-id", "builder", "--json"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["scope"]["verdict_basis"], "selected_subjects_only")


class HandoffDoctorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceHandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.proposal
        self.fixture.proposal = self.fixture.harness.propose_source_handoff(
            generation=old["target"]["generation"], adoption_digest=old["adoption_digest"],
            destination_workspace=old["destination_workspace"], request_id=old["request_id"], by=old["owner"],
            issued_at=old["issued_at"], expires_at=old["expires_at"], task_id="change")
        self.proposal = self.fixture.proposal
        self.fixture.export()
        self.fixture.receive()
        self.runbook = self.fixture.root / "runbook.json"
        self.document = self.fixture.harness.state()["runbook"]
        self.runbook.write_text(json.dumps(self.document))
        self.target_state = self.fixture.root / "target-state"
        self.target_state.mkdir()
        self.evaluator = canonical_digest(evaluator_identity(self.document, self.fixture.source))
        self.arguments = dict(proposal=self.proposal, review_digest=self.proposal["digest"], by="owner",
            archive=self.fixture.package, key=self.fixture.key, runbook=self.runbook,
            workspace=self.fixture.output, state_dir=self.target_state, target_id="target-copy",
            generation="target-generation", agent_id="builder", evaluator_digest=self.evaluator)

    def inspect(self, **changes):
        return inspect_handoff(**dict(self.arguments, **changes))

    def test_exact_received_source_and_worker_probe_without_writes_or_launch_authority(self):
        before = snapshot(self.fixture.root)
        events = self.fixture.harness.events()
        result = self.inspect()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["verdict"], "selected_probes_coherent")
        self.assertEqual(result["doctor"]["scope"]["task_ids"], ["change"])
        self.assertTrue(result["source_matches"])
        self.assertTrue(result["evaluator_matches"])
        for flag in ("execution_authority", "lease_authorized", "target_authenticated", "controller_state_checked"):
            self.assertFalse(result[flag])
        self.assertEqual(result["model_calls"], 0)
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertEqual(snapshot(self.fixture.root), before)
        self.assertEqual(self.fixture.harness.events(), events)
        self.assertFalse((self.fixture.output / "output.txt").exists(), "doctor cannot run the worker or task commands")
        self.assertFalse(list(self.target_state.iterdir()))
        self.assertEqual(result["digest"], canonical_digest({k: v for k, v in result.items() if k != "digest"}))

    def test_missing_target_state_and_changed_expected_evaluator_never_report_ready(self):
        missing = self.fixture.root / "missing-state"
        result = self.inspect(state_dir=missing)
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(missing.exists())
        result = self.inspect(evaluator_digest="sha256:" + "0" * 64)
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["evaluator_matches"])
        self.assertEqual(result["problems"][0]["code"], "EVALUATOR_CONFLICT")

    def test_wrong_owner_key_worker_plan_or_source_denies_before_doctor(self):
        with patch("camol.handoff_doctor.run_doctor", side_effect=AssertionError("no probe")):
            for changes in (dict(by="builder"), dict(key=os.urandom(32)), dict(agent_id="foreign"),
                            dict(target_id="foreign"), dict(generation="foreign"), dict(workspace=self.fixture.source),
                            dict(state_dir=self.fixture.output / "nested")):
                with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                    self.inspect(**changes)
            changed = dict(self.document, rules=[])
            self.runbook.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.inspect()
            self.runbook.write_text(json.dumps(self.document))
            (self.fixture.output / "proof.py").write_text("unapproved\n")
            with self.assertRaises(ValueError):
                self.inspect()

    def test_expiry_and_source_mutation_during_probes_cannot_publish_green(self):
        with patch("camol.handoff_doctor._now", side_effect=[datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=1)]):
            with self.assertRaises(ValueError):
                self.inspect()
        original = run_doctor
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.fixture.output / "proof.py").write_text("changed during probes\n")
            return result
        with patch("camol.handoff_doctor.run_doctor", side_effect=changed), self.assertRaises(ValueError):
            self.inspect()

    def test_validated_document_is_not_reread_after_approval_check(self):
        original = run_doctor
        def changed(*args, **kwargs):
            self.runbook.write_text("not a runbook any more")
            return original(*args, **kwargs)
        with patch("camol.handoff_doctor.run_doctor", side_effect=changed):
            result = self.inspect()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["plan_digest"], self.proposal["source_binding"]["plan_digest"])

    def test_real_child_cli_selected_readiness_and_probe_scope(self):
        proposal_file = self.fixture.root / "proposal.json"
        proposal_file.write_text(json.dumps(self.proposal))
        key_file = self.fixture.root / "key.bin"
        key_file.write_bytes(self.fixture.key)
        key_file.chmod(0o600)
        args = ["source-handoff", "doctor", "--proposal", str(proposal_file), "--review-digest", self.proposal["digest"],
            "--by", "owner", "--archive", str(self.fixture.package), "--key-file", str(key_file),
            "--runbook", str(self.runbook), "--workspace", str(self.fixture.output), "--state-dir", str(self.target_state),
            "--target-id", "target-copy", "--generation", "target-generation", "--agent-id", "builder", "--evaluator-digest", self.evaluator]
        result = subprocess.run([sys.executable, "-m", "camol", *args], cwd=self.fixture.root,
            env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
        value = json.loads(result.stdout)
        self.assertEqual(value["selection_digest"], self.proposal["selection"]["digest"])
        self.assertFalse(value["target_authenticated"])
        self.assertNotIn(self.fixture.key.hex(), result.stdout.decode())
