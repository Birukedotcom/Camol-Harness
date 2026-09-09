import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import camol
from camol import target_preparation as service
from camol.admission import AdmissionBundle
from camol.source_binding import require_source_admission
from camol.schema import canonical_digest
from tests import test_handoff_doctor as fixture


class TargetPreparationTests(unittest.TestCase):
    def setUp(self):
        self.doctor = fixture.HandoffDoctorTests()
        self.doctor.setUp()
        self.addCleanup(self.doctor.doCleanups)
        self.source = self.doctor.fixture
        self.state = self.source.root / "prepared-state"
        self.proposal = service.propose(source_proposal=self.doctor.proposal,
            source_export_digest=self.source.export()["receipt"]["digest"], runbook=self.doctor.document,
            agent_id="builder", evaluator_digest=self.doctor.evaluator, state_dir=str(self.state),
            request_id="prepare-box", by="owner", issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=self.doctor.proposal["expires_at"])

    def prepare(self, **changes):
        return asyncio.run(service.prepare(self.proposal, **dict(dict(by="owner",
            review_digest=self.proposal["digest"], archive=self.source.package, key=self.source.key), **changes)))

    def test_actual_child_prepares_isolated_box_but_cannot_admit_to_controller(self):
        before = self.source.harness.events()
        result = self.prepare()
        self.assertEqual(result["status"], "prepared", result)
        bundle = AdmissionBundle.from_dict(result["bundle"])
        workspace = Path(bundle.workspace.path)
        self.assertTrue((workspace / "proof.py").is_file())
        self.assertFalse((workspace / "output.txt").exists())
        self.assertEqual(workspace.relative_to(self.state).parts[0], "worktrees")
        self.assertEqual(self.state.stat().st_mode & 0o077, 0)
        self.assertEqual(self.source.harness.events(), before)
        for field in ("execution_authority", "controller_admission", "target_authenticated", "global_capacity_reserved", "worker_started"):
            self.assertIs(result[field], False)
        with self.assertRaises(ValueError):
            require_source_admission(self.source.harness.state(), bundle)
        self.assertEqual(service.inspect(self.state)["result"], result)
        with patch.object(service, "prepare_async", side_effect=AssertionError("no redispatch")):
            self.assertEqual(self.prepare(), result)

    def test_unapproved_effects_key_export_owner_and_dirty_source_fail_before_intent(self):
        with patch.object(service, "prepare_async", side_effect=AssertionError("no child")):
            for changes in (dict(by="foreign"), dict(review_digest="sha256:" + "0" * 64), dict(key=os.urandom(32))):
                with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                    self.prepare(**changes)
            original = deepcopy(self.proposal)
            self.proposal["effects"]["launch_worker"] = True
            self.proposal["digest"] = canonical_digest({k: v for k, v in self.proposal.items() if k != "digest"})
            with self.assertRaises(ValueError):
                self.prepare()
            self.proposal = original
            (self.source.output / "proof.py").write_text("unapproved")
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(self.state.exists())

    def test_lost_result_never_repeats_filesystem_effects(self):
        with patch.object(service, "_save", side_effect=OSError("publication lost")), self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(service.inspect(self.state)["status"], "PREPARATION_UNKNOWN")
        with patch.object(service, "prepare_async", side_effect=AssertionError("no retry")), self.assertRaisesRegex(ValueError, "PREPARATION_UNKNOWN"):
            self.prepare()

    def test_cancelled_child_retains_terminal_record_without_authority(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        with patch.object(service, "prepare_async", side_effect=cancelled), self.assertRaises(asyncio.CancelledError):
            self.prepare()
        retained = service.inspect(self.state)
        self.assertEqual(retained["status"], "cancelled")
        self.assertIsNone(retained["result"]["bundle"])
        self.assertEqual(self.prepare(), retained["result"])

    def test_nonempty_state_and_symlink_state_are_not_repurposed(self):
        self.state.mkdir(mode=0o700)
        (self.state / "keep").write_text("owner data")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((self.state / "keep").read_text(), "owner data")
        moved = self.state.with_name("owner-state")
        self.state.rename(moved)
        self.state.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.prepare()
        self.assertFalse((moved / "preparation-intent.json").exists())

    def test_export_mismatch_is_rejected_without_creating_state(self):
        self.proposal["source_export_digest"] = "sha256:" + "0" * 64
        self.proposal["digest"] = canonical_digest({k: v for k, v in self.proposal.items() if k != "digest"})
        with self.assertRaisesRegex(ValueError, "reviewed export"):
            self.prepare()
        self.assertFalse(self.state.exists())

    def test_evaluator_mismatch_cannot_produce_green_readiness(self):
        self.proposal["evaluator_digest"] = "sha256:" + "0" * 64
        self.proposal["digest"] = canonical_digest({k: v for k, v in self.proposal.items() if k != "digest"})
        result = self.prepare()
        if result["status"] == "prepared":
            bundle = AdmissionBundle.from_dict(result["bundle"])
            self.assertFalse(bundle.evaluator_ready)
        else:
            self.assertEqual(result["status"], "failed")
        self.assertFalse(result["worker_started"])

    def test_expiry_during_child_is_retained_as_failure(self):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=1)
        with patch.object(service, "_now", side_effect=[now, now, future, future]):
            result = self.prepare()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["bundle"])
        self.assertEqual(service.inspect(self.state)["status"], "failed")

    def test_repo_git_on_path_never_executes_through_real_preparation(self):
        trapdir = self.source.output / ".git" / "bin"
        trapdir.mkdir()
        sentinel = self.source.root / "trap-executed"
        trap = trapdir / "git"
        trap.write_text("#!/bin/sh\n/usr/bin/touch '" + str(sentinel) + "'\nexit 1\n")
        trap.chmod(0o700)
        # Git metadata is outside tracked source identity, but remains a
        # protected source path for read-only probes and helper selection.
        with patch.dict(os.environ, PATH=str(trapdir) + os.pathsep + os.defpath):
            result = self.prepare()
        self.assertFalse(sentinel.exists())
        self.assertFalse(result["worker_started"])
        if result["status"] == "prepared":
            self.assertNotEqual(result["bundle"]["receipt"]["status"], "green")

    def test_real_cli_plan_apply_and_inspect(self):
        proposal_path = self.source.root / "preparation.json"
        source_path = self.source.root / "source-proposal.json"
        source_path.write_text(json.dumps(self.doctor.proposal))
        key_path = self.source.root / "key.bin"
        key_path.write_bytes(self.source.key)
        key_path.chmod(0o600)
        def command(*args):
            result = subprocess.run([sys.executable, "-m", "camol", "target-prepare", *args],
                cwd=self.source.root, env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
            return json.loads(result.stdout)
        proposal = command("plan", "--source-proposal", str(source_path), "--source-export-digest", self.proposal["source_export_digest"],
            "--runbook", str(self.doctor.runbook), "--agent-id", "builder", "--evaluator-digest", self.doctor.evaluator,
            "--state-dir", str(self.state), "--request-id", "prepare-cli", "--by", "owner", "--expires-at", self.proposal["expires_at"])
        self.assertFalse(self.state.exists())
        proposal_path.write_text(json.dumps(proposal))
        result = command("apply", "--proposal", str(proposal_path), "--review-digest", proposal["digest"],
            "--by", "owner", "--archive", str(self.source.package), "--key-file", str(key_path))
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(command("inspect", "--state-dir", str(self.state))["result"], result)
