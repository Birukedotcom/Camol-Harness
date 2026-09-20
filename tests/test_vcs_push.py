import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import camol
from camol import git_push
from camol.artifacts import ArtifactStore, RunArchive
from camol.gate_runtime import acceptance_digest
from camol.schema import canonical_digest
from camol.state import project
from camol.vcs import VCSError, snapshot, render_snapshot
from camol.vcs_push import VCSPush, propose
from tests import test_vcs as lineage


def git(path, *args):
    return subprocess.check_output(["/usr/bin/git", "-C", str(path), *args], stderr=subprocess.PIPE).decode().strip()


class VCSPushTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lineage.VCSLineageTests.setUpClass.__func__(cls)

    def setUp(self):
        lineage.VCSLineageTests.setUp(self)
        self.remote = (self.root / "remote.git").resolve()
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--quiet")
        self.service = VCSPush(self.orchestrator, self.run_id)
        self.integration = self.final["integrations"][-1]
        self.target = dict(kind="local_bare", repository=str(self.remote), branch="build/result")
        now = datetime.now(timezone.utc)
        self.arguments = dict(candidate_id=self.integration["candidate_id"], integration_id=self.integration["integration_id"],
            target=self.target, expected_old=None, request_id="push-1", issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=10)).isoformat(), timeout_seconds=30)

    def proposal(self, **changes):
        return self.service.propose(**dict(self.arguments, **changes))

    def publish(self, proposal=None, **changes):
        proposal = self.proposal() if proposal is None else proposal
        return self.service.publish(proposal, **dict(by="owner", review_digest=proposal["digest"], allow_write=True, **changes))

    def test_real_push_exact_retry_replay_and_export_without_changing_gates(self):
        before = self.orchestrator.state(self.run_id)
        proposal = self.proposal()
        record = self.publish(proposal)
        self.assertEqual(record["receipt"]["result"]["status"], "confirmed", record)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/build/result"), self.integration["revision"])
        with patch("camol.git_push.publish", side_effect=AssertionError("never reissue")):
            self.assertEqual(self.publish(proposal), record)
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(acceptance_digest(after), acceptance_digest(before))
        self.assertEqual(after["total_tokens"], before["total_tokens"])
        self.assertEqual(project(self.store.read(self.run_id)), after)
        graph = snapshot(after)
        self.assertEqual(graph["schema_version"], 3)
        self.assertTrue(graph["coverage"]["remote_push_observed"])
        self.assertIn(": confirmed; request push-1", render_snapshot(graph))
        archive = self.root / "export"
        RunArchive.export(self.run_id, self.store.read(self.run_id), ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), after)

    def test_approval_expiry_and_foreign_or_extended_plan_precede_git(self):
        proposal = self.proposal()
        variants = []
        for field, value in (("owner", "worker"), ("allow_non_fast_forward", True), ("automatic_retries", 1), ("extra", True)):
            changed = dict(proposal, **{field: value})
            changed["digest"] = canonical_digest({key: val for key, val in changed.items() if key != "digest"})
            variants.append(changed)
        with patch("camol.git_push.publish", side_effect=AssertionError("must not call Git")):
            for changed in variants:
                with self.assertRaises(ValueError):
                    self.publish(changed)
            with self.assertRaises(VCSError):
                self.service.publish(proposal, by="owner", review_digest=proposal["digest"])
            with self.assertRaises(VCSError):
                self.service.publish(proposal, by="worker", review_digest=proposal["digest"], allow_write=True)
            with self.assertRaises(VCSError):
                self.publish(proposal, token="unused-local-secret")
            with patch.object(self.orchestrator, "clock", return_value=datetime.now(timezone.utc) + timedelta(hours=2)):
                with self.assertRaises(VCSError):
                    self.publish(proposal)
        self.assertFalse(self.orchestrator.state(self.run_id).get("vcs_pushes"))

    def test_known_expected_ref_update_and_already_present_are_distinct(self):
        first = self.final["integrations"][0]
        initial = self.proposal(candidate_id=first["candidate_id"], integration_id=first["integration_id"], request_id="first")
        self.assertEqual(self.publish(initial)["receipt"]["result"]["status"], "confirmed")
        follow = self.proposal(request_id="follow", expected_old=first["revision"])
        self.assertEqual(self.publish(follow)["receipt"]["result"]["status"], "confirmed")
        present = self.proposal(request_id="present", expected_old=self.integration["revision"])
        result = self.publish(present)["receipt"]["result"]
        self.assertEqual(result["status"], "already_present")
        self.assertFalse(result["dispatch_attempted"])

    def test_changed_destination_and_non_fast_forward_never_overwrite(self):
        self.publish()
        wrong = self.proposal(request_id="absent-again")
        result = self.publish(wrong)["receipt"]["result"]
        self.assertEqual(result["status"], "not_dispatched")
        first = self.final["integrations"][0]
        backwards = self.proposal(candidate_id=first["candidate_id"], integration_id=first["integration_id"],
                                  request_id="backward", expected_old=self.integration["revision"])
        self.assertEqual(self.publish(backwards)["receipt"]["result"]["status"], "not_dispatched")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/build/result"), self.integration["revision"])

    def test_missing_outcome_append_preserves_pending_and_never_reissues(self):
        proposal = self.proposal()
        original = self.store.append
        def append(event, **kwargs):
            if event["type"] == "VCS_PUSH_FINISHED":
                raise OSError("lost outcome publication")
            return original(event, **kwargs)
        with patch.object(self.store, "append", side_effect=append), self.assertRaises(OSError):
            self.publish(proposal)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/build/result"), self.integration["revision"])
        with patch("camol.git_push.publish", side_effect=AssertionError("never repeat")):
            self.assertIsNone(self.publish(proposal)["receipt"])
            with self.assertRaises(VCSError):
                self.publish(self.proposal(request_id="bypass"))
        self.assertIn("pending_effect_unknown", render_snapshot(snapshot(self.orchestrator.state(self.run_id))))

    def test_lost_readback_after_real_push_is_unknown_and_blocks_new_request(self):
        original = git_push.bounded_preflight_run
        reads = []
        def call(argv, **kwargs):
            if "ls-remote" in argv:
                reads.append(argv)
                if len(reads) == 2:
                    raise OSError("readback lost")
            return original(argv, **kwargs)
        with patch.object(git_push, "bounded_preflight_run", side_effect=call):
            record = self.publish()
        self.assertEqual(record["receipt"]["result"]["status"], "effect_unknown")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/build/result"), self.integration["revision"])
        with self.assertRaises(VCSError):
            self.publish(self.proposal(request_id="another"))

    def test_cancellation_before_launch_and_linked_target_make_no_push(self):
        cancellation = threading.Event()
        cancellation.set()
        result = self.publish(cancel_event=cancellation)["receipt"]["result"]
        self.assertEqual(result["status"], "not_dispatched")
        link = self.root / "linked.git"
        link.symlink_to(self.remote, target_is_directory=True)
        with self.assertRaises(VCSError):
            self.proposal(request_id="linked", target=dict(self.target, repository=str(link)))
        self.assertFalse(git(self.remote, "show-ref", "--heads") if (self.remote / "refs/heads/build").exists() else "")

    def test_expanded_result_and_worker_event_rejected_on_replay(self):
        self.publish()
        events = self.store.read(self.run_id)
        for change in ("actor", "receipt"):
            edited = copy.deepcopy(events)
            if change == "actor":
                edited[-1]["actor_id"] = next(iter(self.final["agents"]))
            else:
                edited[-1]["payload"]["result"]["extra"] = True
                edited[-1]["payload"]["digest"] = canonical_digest({k: v for k, v in edited[-1]["payload"].items() if k != "digest"})
            with self.assertRaises(ValueError):
                project(edited)

    def test_actual_child_cli_propose_push_and_status(self):
        target = self.root / "target.json"
        target.write_text(json.dumps(self.target))
        common = ["--state-dir", str(self.root), "--run-id", self.run_id]
        def cli(*arguments):
            result = subprocess.run([sys.executable, "-m", "camol", "vcs", *arguments, *common],
                cwd=self.fixture.workspace, env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
            return json.loads(result.stdout)
        proposal = cli("propose-push", "--candidate", self.integration["candidate_id"], "--integration", self.integration["integration_id"],
            "--target", str(target), "--create-branch", "--request-id", "child", "--expires-at", self.arguments["expires_at"])
        path = self.root / "proposal.json"
        path.write_text(json.dumps(proposal))
        record = cli("push", "--workspace", str(self.fixture.workspace), "--proposal", str(path), "--by", "owner",
                     "--review-digest", proposal["digest"], "--allow-write")
        self.assertEqual(record["receipt"]["result"]["status"], "confirmed")
        self.assertEqual(cli("push-status", "--request-id", "child"), record)

    def test_repository_replaced_after_review_is_not_written(self):
        proposal = self.proposal()
        self.remote.rename(self.root / "original.git")
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--quiet")
        self.assertEqual(self.publish(proposal)["receipt"]["result"]["status"], "not_dispatched")
        self.assertEqual(git(self.remote, "for-each-ref"), "")

    def test_ref_race_between_read_and_push_is_not_overwritten(self):
        original = git_push.bounded_preflight_run
        first = self.final["integrations"][0]
        source = self.integration["workspace"]["path"]
        def call(argv, **kwargs):
            if "push" in argv:
                git(self.remote, "fetch", source, first["revision"] + ":refs/heads/build/result")
            return original(argv, **kwargs)
        with patch.object(git_push, "bounded_preflight_run", side_effect=call):
            record = self.publish()
        self.assertEqual(record["receipt"]["result"]["status"], "effect_unknown")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/build/result"), first["revision"])

    def test_github_case_alias_and_credential_channel_without_network(self):
        secret = "explicit-test-credential-not-a-real-token"
        destination = dict(kind="github_https", repository="Owner/Repo", branch="result")
        proposal = self.proposal(target=destination)
        self.assertEqual(proposal["target"]["repository"], "owner/repo")
        calls = []
        def call(argv, **kwargs):
            calls.append(argv)
            self.assertNotIn(secret, repr(argv))
            self.assertTrue(kwargs["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
            if "push" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            output = b"" if len(calls) == 1 else (self.integration["revision"] + "\trefs/heads/result\n").encode()
            return subprocess.CompletedProcess(argv, 0, output, b"")
        with patch.object(git_push, "bounded_preflight_run", side_effect=call):
            record = self.publish(proposal, allow_network=True, token=secret)
        self.assertEqual(record["receipt"]["result"]["status"], "confirmed")
        self.assertEqual(len(calls), 3)
        self.assertNotIn(secret, json.dumps(self.store.read(self.run_id)))

    def test_source_pre_push_hook_and_remote_rewrite_are_not_used(self):
        source = Path(self.integration["workspace"]["path"])
        sentinel = self.root / "source-hook-ran"
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\ntouch '" + str(sentinel) + "'\nexit 1\n")
        hook.chmod(0o700)
        # The class's fixture is shared: restore every setting in finally.
        settings = {"core.hooksPath": str(hooks), "url.ext::false.insteadOf": str(self.remote)}
        prior = {}
        for key, value in settings.items():
            try:
                prior[key] = git(source, "config", "--get", key)
            except subprocess.CalledProcessError:
                prior[key] = None
            git(source, "config", key, value)
        try:
            self.assertEqual(self.publish()["receipt"]["result"]["status"], "confirmed")
            self.assertFalse(sentinel.exists())
        finally:
            for key, value in prior.items():
                if value is None:
                    git(source, "config", "--unset", key)
                else:
                    git(source, "config", key, value)
