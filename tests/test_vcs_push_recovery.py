import unittest
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import camol
from camol import git_push
from camol.artifacts import ArtifactStore, RunArchive
from camol.gate_runtime import acceptance_digest
from camol.schema import canonical_digest
from camol.state import project
from camol.vcs import VCSError, snapshot, render_snapshot
from camol.vcs_push import _result, VCSPush, apply
from tests import test_vcs_push as push_fixture
from tests.test_vcs_push import git


class PushResultContractTests(unittest.TestCase):
    def test_non_string_status_is_a_controlled_contract_denial(self):
        for status in ([], {}, None, True, 1):
            with self.subTest(status=status), self.assertRaises(VCSError):
                _result(dict(status=status, dispatch_attempted=False, observed_before=None,
                             observed_after=None, push_exit_code=None, error_code="PUSH_NOT_DISPATCHED"), {})

    def test_non_string_receipt_identity_is_a_controlled_contract_denial(self):
        state = dict(run_id="run", status="completed", approved_by="owner", agents={}, tasks={})
        for request_id in ([], {}, None, True, 1):
            value = dict(request_id=request_id, proposal_digest="invalid", finished_at="invalid",
                         elapsed_ms=0, result={}, digest="invalid")
            with self.subTest(request_id=request_id), self.assertRaises(VCSError):
                apply(copy.deepcopy(state), dict(type="VCS_PUSH_FINISHED", run_id="run", actor_id="owner", payload=value))


class PushRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(push_fixture.VCSPushTests.setUpClass.__func__)
    setUp = push_fixture.VCSPushTests.setUp
    proposal = push_fixture.VCSPushTests.proposal
    publish = push_fixture.VCSPushTests.publish

    def uncertain(self, pending=False):
        if pending:
            original = self.store.append
            def append(event, **kwargs):
                if event["type"] == "VCS_PUSH_FINISHED":
                    raise OSError("lost final append")
                return original(event, **kwargs)
            with patch.object(self.store, "append", side_effect=append), self.assertRaises(OSError):
                self.publish()
        else:
            original = git_push.bounded_preflight_run
            reads = []
            def call(argv, **kwargs):
                if "ls-remote" in argv:
                    reads.append(argv)
                    if len(reads) == 2:
                        raise OSError("lost post-push readback")
                return original(argv, **kwargs)
            with patch.object(git_push, "bounded_preflight_run", side_effect=call):
                self.assertEqual(self.publish()["receipt"]["result"]["status"], "effect_unknown")
        return self.orchestrator.state(self.run_id)["vcs_pushes"]["push-1"]

    def review(self):
        now = datetime.now(timezone.utc)
        return self.service.propose_acknowledgment(request_id="push-1", reason="Reviewed destination and outstanding external effects; permit a new exact review.",
            issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())

    def acknowledge(self, value):
        return self.service.acknowledge(value, by="owner", review_digest=value["digest"])

    def test_unknown_is_retained_while_separate_new_review_can_observe_present_ref(self):
        prior = self.uncertain()
        before = self.orchestrator.state(self.run_id)
        review = self.review()
        with patch.object(git_push, "publish", side_effect=AssertionError("ack must not execute Git")):
            receipt = self.acknowledge(review)
            self.assertEqual(self.acknowledge(review), receipt)
            with self.assertRaises(VCSError):
                next_review = self.proposal(request_id="next", expected_old=self.integration["revision"])
                self.service.publish(next_review, by="owner", review_digest=next_review["digest"], allow_write=False)
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(after["vcs_pushes"]["push-1"]["receipt"], prior["receipt"])
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(acceptance_digest(after), acceptance_digest(before))
        self.assertEqual(after["total_tokens"], before["total_tokens"])
        self.assertIn("Owner acknowledged uncertainty", render_snapshot(snapshot(after)))
        self.assertEqual(snapshot(after)["schema_version"], 4)
        self.assertFalse(snapshot(after)["coverage"]["remote_push_observed"])
        result = self.publish(self.proposal(request_id="next", expected_old=self.integration["revision"]))
        self.assertEqual(result["receipt"]["result"]["status"], "already_present")
        self.assertFalse(result["receipt"]["result"]["dispatch_attempted"])
        final = self.orchestrator.state(self.run_id)
        self.assertEqual(project(self.store.read(self.run_id)), final)
        archive = self.root / "recovery-export"
        RunArchive.export(self.run_id, self.store.read(self.run_id), ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), final)

    def test_pending_retains_missing_receipt_and_ack_retry_survives_expiry(self):
        self.uncertain(pending=True)
        review = self.review()
        receipt = self.acknowledge(review)
        with patch.object(self.orchestrator, "clock", return_value=datetime.now(timezone.utc) + timedelta(days=1)):
            self.assertEqual(VCSPush(self.orchestrator, self.run_id).acknowledge(review, by="owner", review_digest=review["digest"]), receipt)
        record = self.orchestrator.state(self.run_id)["vcs_pushes"]["push-1"]
        self.assertIsNone(record["receipt"])
        self.assertFalse(record["acknowledgment"]["proposal"]["prior_outcome_resolved"])

    def test_wrong_owner_stale_target_scope_expiry_and_false_confirmation_are_rejected(self):
        self.uncertain()
        review = self.review()
        baseline = self.store.read(self.run_id)
        with self.assertRaises(VCSError):
            self.service.acknowledge(review, by="worker", review_digest=review["digest"])
        for key, value in (("record_digest", "sha256:" + "0" * 64), ("prior_outcome_resolved", True),
                           ("execution_authority", True), ("automatic_retry", True), ("extra", True)):
            changed = dict(review, **{key: value})
            changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
            with self.subTest(key=key), self.assertRaises(VCSError):
                self.acknowledge(changed)
        with patch.object(self.orchestrator, "clock", return_value=datetime.now(timezone.utc) + timedelta(days=1)), self.assertRaises(VCSError):
            self.acknowledge(review)
        self.assertEqual(self.store.read(self.run_id), baseline)

    def test_changed_pending_result_invalidates_old_acknowledgment_review(self):
        self.uncertain(pending=True)
        review = self.review()
        record = self.orchestrator.state(self.run_id)["vcs_pushes"]["push-1"]
        finished = self.orchestrator._now()
        outcome = dict(status="confirmed", dispatch_attempted=True, observed_before=None,
            observed_after=self.integration["revision"], push_exit_code=0, error_code=None)
        receipt = dict(request_id="push-1", proposal_digest=record["proposal"]["digest"], finished_at=finished, elapsed_ms=1, result=outcome)
        receipt["digest"] = canonical_digest(receipt)
        self.service._append("VCS_PUSH_FINISHED", receipt, "owner", finished, self.orchestrator.state(self.run_id))
        with self.assertRaises(VCSError):
            self.acknowledge(review)

    def test_acknowledgment_before_dispatch_prevents_original_late_launch(self):
        real_publish = git_push.publish
        def publish(proposal, **kwargs):
            self.acknowledge(self.review())
            return real_publish(proposal, **kwargs)
        with patch.object(git_push, "publish", side_effect=publish):
            record = self.publish()
        self.assertEqual(record["receipt"]["result"]["status"], "not_dispatched")
        self.assertTrue(record["acknowledgment"])
        self.assertEqual(git(self.remote, "for-each-ref"), "")

    def test_child_cli_acknowledgment_is_metadata_only(self):
        prior = self.uncertain()
        common = ["--state-dir", str(self.root), "--run-id", self.run_id]
        def cli(*arguments):
            result = subprocess.run([sys.executable, "-m", "camol", "vcs", *arguments, *common],
                cwd=self.fixture.workspace, env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
            return json.loads(result.stdout)
        review = cli("propose-push-ack", "--request-id", "push-1", "--reason", "Reviewed uncertain publication effects", "--expires-at", self.arguments["expires_at"])
        path = self.root / "ack.json"
        path.write_text(json.dumps(review))
        result = cli("acknowledge-push", "--workspace", str(self.fixture.workspace), "--proposal", str(path), "--by", "owner", "--review-digest", review["digest"])
        record = cli("push-status", "--request-id", "push-1")
        self.assertEqual(record["acknowledgment"], result)
        self.assertEqual(record["receipt"], prior["receipt"])

    def test_replay_cannot_replace_owner_acknowledgment_with_worker_authority(self):
        self.uncertain()
        self.acknowledge(self.review())
        original = self.store.read(self.run_id)
        changed = copy.deepcopy(original)
        changed[-1]["actor_id"] = next(iter(self.final["agents"]))
        with self.assertRaises(ValueError):
            project(changed)
        self.assertEqual(project(original), self.orchestrator.state(self.run_id))
