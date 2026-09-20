import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import camol
from camol import Harness
from camol.artifacts import ArtifactStore, RunArchive
from camol.debug_execution import source_identity
from camol.recovery import RecoveryError
from camol.schema import canonical_digest
from camol.source_handoff import receive_source, export_source
from camol.state import project
from tests import test_evaluation as fixture
from tests.test_gate_runtime import v5_plan


class SourceHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EvaluationLoopTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root.resolve()
        self.source = self.fixture.source.resolve()
        (self.source / "proof.py").write_text("print('handoff-ready')\n")
        fixture.git(self.source, "add", "proof.py")
        fixture.git(self.source, "commit", "-qm", "source handoff proof")
        self.harness = Harness(self.source, self.fixture.state).__enter__()
        self.addCleanup(self.harness.close)
        state = self.harness.prepare(v5_plan("source-handoff"))
        self.harness.orchestrator.bind_source(state["run_id"], source_identity(self.source))
        self.harness.approve(by="owner", digest=state["plan_digest"])
        now = datetime.now(timezone.utc)
        self.expiry = (now + timedelta(minutes=5)).isoformat()
        descriptor = dict(schema="camol.execution_target", schema_version=1, target_id="target-copy",
            generation="target-generation", control_plane_id="control", label="copy fixture", ownership="adopted",
            provider=dict(kind="local", account="owner", project="fixture", location="local", resource_id="receiver", resource_name="receiver"),
            transport=dict(kind="local", profile_digest="sha256:" + "a" * 64))
        review = self.harness.targets.propose(descriptor, by="owner", expires_at=self.expiry)
        self.harness.targets.adopt(review, by="owner", approval_digest=review["digest"])
        self.output = self.root / "receiver"
        self.package = self.root / "package"
        self.proposal = self.harness.propose_source_handoff(generation="target-generation", adoption_digest=review["digest"],
            destination_workspace=str(self.output), request_id="source-copy", by="owner", issued_at=now.isoformat(), expires_at=self.expiry)
        self.key = os.urandom(32)

    def export(self):
        return self.harness.export_source_handoff(self.proposal, by="owner", review_digest=self.proposal["digest"], key=self.key, output=self.package)

    def receive(self, **changes):
        arguments = dict(archive=self.package, key=self.key, proposal=self.proposal, review_digest=self.proposal["digest"],
                         target_id="target-copy", generation="target-generation", output=self.output, by="owner")
        arguments.update(changes)
        return receive_source(**arguments)

    def test_independent_copy_matches_commit_tree_bytes_and_runs_without_source(self):
        before = self.harness.state()
        record = self.export()
        self.assertEqual(self.export(), record)
        self.assertNotIn(b"handoff-ready", (self.package / "source.camol").read_bytes())
        (self.source).rename(self.root / "source-offline")
        receipt = self.receive()
        expected = dict(self.proposal["source_binding"]["source"], workspace=str(self.output))
        self.assertEqual(receipt["source"], expected)
        self.assertEqual(source_identity(self.output), expected)
        result = subprocess.run([sys.executable, str(self.output / "proof.py")], cwd=self.output,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True)
        self.assertEqual(result.stdout, b"handoff-ready\n")
        self.assertFalse(receipt["execution_authority"])
        self.assertFalse(receipt["readiness_proven"])
        after = self.harness.state()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["total_tokens"], before["total_tokens"])
        self.assertEqual(project(self.harness.events()), after)
        archive = self.root / "ledger-export"
        RunArchive.export(after["run_id"], self.harness.events(), ArtifactStore(self.fixture.state), archive)
        self.assertEqual(RunArchive.replay(archive), after)

    def test_changed_source_unknown_fields_and_wrong_owner_never_publish(self):
        changed = dict(self.proposal, execution_authority=True)
        changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
        with self.assertRaises(ValueError):
            self.harness.export_source_handoff(changed, by="owner", review_digest=changed["digest"], key=self.key, output=self.package)
        with self.assertRaises(ValueError):
            self.harness.export_source_handoff(self.proposal, by="builder", review_digest=self.proposal["digest"], key=self.key, output=self.package)
        (self.source / "proof.py").write_text("print('unapproved')\n")
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.package.exists())
        self.assertFalse(self.harness.state().get("source_handoffs"))

    def test_wrong_key_changed_receipt_target_path_and_expiry_deny_receive(self):
        self.export()
        for changes in (dict(key=os.urandom(32)), dict(target_id="foreign"), dict(generation="foreign"),
                        dict(output=self.root / "other"), dict(by="builder"),
                        dict(clock=lambda: datetime.now(timezone.utc) + timedelta(days=1))):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self.receive(**changes)
        self.assertFalse(self.output.exists())
        receipt_path = self.package / "receipt.json"
        value = json.loads(receipt_path.read_text())
        value["readiness_proven"] = True
        value["digest"] = canonical_digest({k: v for k, v in value.items() if k != "digest"})
        receipt_path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.receive()
        self.assertFalse(self.output.exists())

    def test_retirement_during_capture_prevents_publication(self):
        from camol import source_handoff
        original = source_handoff._restore
        def restore(*args):
            result = original(*args)
            self.harness.targets.retire("target-generation", by="owner", adoption_digest=self.proposal["adoption_digest"], reason="target retired during capture")
            return result
        with patch.object(source_handoff, "_restore", side_effect=restore), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.package.exists())

    def test_lost_event_append_recovers_exact_package_without_rewriting_ciphertext(self):
        original = self.harness.store.append
        def append(event, **kwargs):
            if event["type"] == "SOURCE_HANDOFF_EXPORTED":
                raise OSError("lost export append")
            return original(event, **kwargs)
        with patch.object(self.harness.store, "append", side_effect=append), self.assertRaises(OSError):
            self.export()
        first = (self.package / "source.camol").read_bytes()
        from camol.recovery import _crypto
        cipher = _crypto(self.key)
        class ExistingCipher:
            decrypt = cipher.decrypt
            def encrypt(self, *args):
                raise AssertionError("no new encryption")
        with patch("camol.source_handoff._crypto", return_value=ExistingCipher()):
            recovered = self.export()
        self.assertEqual((self.package / "source.camol").read_bytes(), first)
        self.assertIn("receipt", recovered)

    def test_nonempty_output_and_source_hook_are_not_used(self):
        marker = self.root / "hook-ran"
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o700)
        fixture.git(self.source, "config", "core.hooksPath", str(hooks))
        self.export()
        self.output.mkdir()
        (self.output / "keep").write_text("keep")
        with self.assertRaises(ValueError):
            self.receive()
        self.assertEqual((self.output / "keep").read_text(), "keep")
        self.assertFalse(marker.exists())

    def test_worker_authored_receipt_and_readiness_claim_fail_replay(self):
        self.export()
        events = self.harness.events()
        for field in ("actor", "readiness"):
            changed = copy.deepcopy(events)
            if field == "actor":
                changed[-1]["actor_id"] = "builder"
            else:
                receipt = changed[-1]["payload"]["receipt"]
                receipt["readiness_proven"] = True
                receipt["digest"] = canonical_digest({k: v for k, v in receipt.items() if k != "digest"})
            with self.assertRaises(ValueError):
                project(changed)

    def test_real_child_cli_export_receive_and_inspection(self):
        proposal_file = self.root / "proposal.json"
        proposal_file.write_text(json.dumps(self.proposal))
        key_file = self.root / "key.bin"
        key_file.write_bytes(self.key)
        key_file.chmod(0o600)
        self.harness.close()
        def cli(*arguments):
            result = subprocess.run([sys.executable, "-m", "camol", "source-handoff", *arguments], cwd=self.root,
                env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
            return json.loads(result.stdout)
        common = ["--state-dir", str(self.fixture.state), "--run-id", "source-handoff"]
        approval = ["--proposal", str(proposal_file), "--by", "owner", "--review-digest", self.proposal["digest"], "--key-file", str(key_file)]
        record = cli("export", *common, *approval, "--workspace", str(self.source), "--output", str(self.package))
        receipt = cli("receive", *approval, "--archive", str(self.package), "--target-id", "target-copy", "--generation", "target-generation", "--output", str(self.output))
        self.assertEqual(receipt["export_digest"], record["receipt"]["digest"])
        self.assertEqual(cli("inspect", *common, "--request-id", "source-copy"), record)
