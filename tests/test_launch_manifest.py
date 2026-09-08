import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from camol.app import InteractiveController
from camol.launch_manifest import build_manifest, profiles_for_runbook, LaunchError
from camol.preflight_journal import PreflightJournal
from camol.providers import create_claude_capability, capability_path
from camol.schema import canonical_digest
from camol.supervisor import SupervisorError
from tests.test_codex_adapter import codex_profile
from tests.test_gate_runtime import v5_plan
from tests.test_providers import profile_payload, Completed, Help


class LaunchManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "README").write_text("baseline\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "fake-claude").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "fake-claude").chmod(0o755)
        self.environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.controller = InteractiveController(self.workspace, state_root=self.root / "camol")
        self.controller.spawn_fn = Mock(return_value={"pid": 123, "started": True})
        self.requests = []
        self.behavior = None
        def runtime(argv, **kwargs):
            if "--help" in argv:
                return Help()
            self.requests.append(argv)
            if self.behavior:
                return self.behavior(argv, **kwargs)
            return Completed()
        self.controller.preflight_fn = lambda profile, **kwargs: create_claude_capability(profile, runner=runtime, **kwargs)

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True).stdout.decode().strip()

    def import_profiles(self, profiles):
        runbook = v5_plan("mixed-launch")
        base = runbook["agents"][0]
        runbook["agents"] = []
        for index, profile in enumerate(profiles):
            agent = copy.deepcopy(base)
            agent.update(id="worker-" + str(index), box="box-" + str(index))
            if profile is not None:
                agent["adapter"] = dict(kind=profile["adapter_kind"], profile="profile.json", profile_snapshot=profile, timeout_seconds=30)
            runbook["agents"].append(agent)
        runbook["run"]["max_concurrency"] = len(profiles)
        path = self.root / "runbook.json"
        path.write_text(json.dumps(runbook))
        result = self.controller.handle("/import " + str(path))
        return result

    def approve_and_review(self, preflight_cents=10):
        self.assertIn("Approved", self.controller.handle("/approve yes").messages[0])
        response = self.controller.handle("/run --preflight-cents " + str(preflight_cents))
        self.assertIn("LAUNCH REVIEW", response.messages[0])
        manifest = json.loads(response.messages[0].split("\n", 1)[1])
        return manifest, response.messages[-1].splitlines()[-1]

    def profiles(self):
        return [profile_payload(profile_id="claude-a"), profile_payload(profile_id="claude-b", effort="low")]

    def test_mixed_review_is_deterministic_passive_and_never_sums_worker_envelopes(self):
        profiles = self.profiles()
        result = self.import_profiles([None, profiles[0], profiles[0], profiles[1], codex_profile(), codex_profile(True)])
        self.assertIn("IMPORTED PLAN", result.messages[0])
        with patch.object(self.controller.connections, "probe_claude", side_effect=AssertionError("review probes auth")), patch("camol.codex_policy.local_model_inventory", side_effect=AssertionError("review probes catalog")):
            manifest, command = self.approve_and_review()
            repeated = self.controller.handle("/run")
        self.assertEqual(json.loads(repeated.messages[0].split("\n", 1)[1]), manifest)
        self.assertEqual(len(manifest["workers"]), 6)
        self.assertEqual(len(manifest["preflights"]), 2)
        self.assertEqual(manifest["common_hosted_worker_ceiling_usd_cents"], 30)
        self.assertEqual(manifest["max_requested_preflight_total_usd_cents"], 20)
        self.assertEqual(manifest["target"]["readiness"], "unverified")
        self.assertIn("--accept-spend", command)
        self.assertEqual(self.requests, [])
        self.controller.spawn_fn.assert_not_called()
        self.assertFalse((Path(self.controller.session["state_dir"]) / "provider-preflights").exists())

    def test_profile_id_collision_missing_snapshots_and_conflicting_global_budgets_are_denied(self):
        for profiles in ([profile_payload(), profile_payload(effort="low")],
                         [profile_payload(), profile_payload(profile_id="other", max_run_usd_cents=31)]):
            result = self.import_profiles(profiles)
            self.assertIn("denied", result.messages[0])
            self.assertIsNone(self.controller.session["plan"])
        runbook = v5_plan()
        runbook["agents"][0]["adapter"] = {"kind": "claude_cli", "profile": "changing.json"}
        with self.assertRaisesRegex(LaunchError, "embedded"):
            profiles_for_runbook(runbook)
        self.assertEqual(self.requests, [])

    def test_all_claude_profiles_preflight_once_and_retry_after_spawn_failure_reuses_receipts(self):
        profiles = self.profiles()
        self.import_profiles([profiles[0], profiles[0], profiles[1]])
        manifest, command = self.approve_and_review(7)
        self.controller.spawn_fn.side_effect = [SupervisorError("fixture spawn failed before start"), {"pid": 123, "started": True}]
        first = self.controller.handle(command)
        self.assertIn("fixture spawn failed", first.messages[0])
        self.assertEqual(len(self.requests), 2)
        second = self.controller.handle(command)
        self.assertIn("additional preflight charge=0", second.messages[0])
        self.assertEqual(len(self.requests), 2)
        self.assertIn("detached", second.messages[-1])
        self.assertEqual(self.controller.spawn_fn.call_args.kwargs["expected_source"], manifest["source"])
        with PreflightJournal(Path(self.controller.session["state_dir"]), read_only=True) as journal:
            rows = journal.inventory()
        self.assertEqual({row["intent"]["operation_id"] for row in rows}, {row["operation_id"] for row in manifest["preflights"]})
        self.assertEqual([row["intent"]["max_usd_cents"] for row in rows], [7, 7])

    def test_old_flags_wrong_digest_and_spend_alone_never_invoke_provider(self):
        self.import_profiles(self.profiles())
        manifest, command = self.approve_and_review()
        for raw in ("/run --accept-spend --worker-cents 30", "/run --accept-spend",
                    "/run --accept-provider-policy " + canonical_digest(manifest) + " --accept-spend",
                    command.replace(canonical_digest(manifest), "sha256:" + "0" * 64),
                    command.replace(" --accept-spend", ""), command + " --preflight-cents 10"):
            self.assertIn("denied", self.controller.handle(raw).messages[0])
        for value in ("True", "0", "101", "999999999999", "1.5"):
            self.assertIn("denied", self.controller.handle("/run --preflight-cents " + value).messages[0])
        altered = self.controller.handle(command.replace("--preflight-cents 10", "--preflight-cents 9"))
        self.assertIn("exact current", altered.messages[0])
        self.assertEqual(self.requests, [])
        self.controller.spawn_fn.assert_not_called()

    def test_unknown_second_preflight_holds_repeat_and_does_not_start_any_worker(self):
        self.import_profiles(self.profiles())
        _, command = self.approve_and_review()
        def uncertain(argv, **kwargs):
            if len(self.requests) == 2:
                raise subprocess.TimeoutExpired(argv, 1)
            return Completed()
        self.behavior = uncertain
        self.assertIn("denied", self.controller.handle(command).messages[0])
        self.assertEqual(len(self.requests), 2)
        self.assertIn("unresolved preflight", self.controller.handle(command).messages[0])
        self.assertEqual(len(self.requests), 2)
        self.controller.spawn_fn.assert_not_called()

    def test_known_failure_and_stale_operation_are_not_automatically_retried(self):
        self.import_profiles(self.profiles()[:1])
        _, command = self.approve_and_review()
        class Failed(Completed):
            stdout = json.dumps({"type": "result", "result": "failed", "is_error": True,
                "total_cost_usd": .002, "usage": {"input_tokens": 4, "output_tokens": 1}}).encode()
        self.behavior = lambda *args, **kwargs: Failed()
        self.assertIn("denied", self.controller.handle(command).messages[0])
        self.assertIn("already spent", self.controller.handle(command).messages[0])
        self.assertEqual(len(self.requests), 1)
        self.controller.spawn_fn.assert_not_called()

        # A distinct fixture session demonstrates expiry without sleeping or
        # changing the already-approved manifest/operation ID on retry.
        self.controller = InteractiveController(self.workspace, state_root=self.root / "stale-camol")
        self.controller.spawn_fn = Mock(return_value={"pid": 123, "started": True})
        self.import_profiles(self.profiles()[:1])
        _, command = self.approve_and_review()
        requests = []
        def runtime(argv, **kwargs):
            if "--help" in argv:
                return Help()
            requests.append(argv)
            return Completed()
        expired_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        first = True
        def preflight(profile, **kwargs):
            nonlocal first
            when = expired_at if first else None
            first = False
            return create_claude_capability(profile, runner=runtime, now=when, **kwargs)
        self.controller.preflight_fn = preflight
        self.assertIn("stale", self.controller.handle(command).messages[0])
        self.assertIn("already spent", self.controller.handle(command).messages[0])
        self.assertEqual(len(requests), 1)
        self.controller.spawn_fn.assert_not_called()

    def test_source_drift_during_preflight_retains_accounting_and_prevents_spawn(self):
        self.import_profiles(self.profiles()[:1])
        _, command = self.approve_and_review()
        def drift(argv, **kwargs):
            (self.workspace / "README").write_text("new source\n")
            self.git("add", ".")
            self.git("commit", "-qm", "changed")
            return Completed()
        self.behavior = drift
        self.assertIn("source", self.controller.handle(command).messages[0])
        self.assertEqual(len(self.requests), 1)
        self.controller.spawn_fn.assert_not_called()
        with PreflightJournal(Path(self.controller.session["state_dir"]), read_only=True) as journal:
            self.assertEqual(journal.inventory()[0]["outcome"]["status"], "succeeded")

    def test_cancellation_between_profiles_stops_the_batch_without_erasing_cost(self):
        self.import_profiles(self.profiles())
        _, command = self.approve_and_review()
        def cancel(argv, **kwargs):
            self.controller.cancel_active()
            return Completed()
        self.behavior = cancel
        self.assertIn("cancelled", self.controller.handle(command).messages[0])
        self.assertEqual(len(self.requests), 1)
        self.controller.spawn_fn.assert_not_called()

    def test_earlier_capability_expiry_is_rechecked_after_later_profile(self):
        self.import_profiles(self.profiles())
        _, command = self.approve_and_review()
        original = self.controller.preflight_fn
        receipts = []
        def expire(profile, **kwargs):
            receipt = original(profile, **kwargs)
            receipts.append((profile, receipt))
            if len(receipts) == 2:
                first_profile, first = receipts[0]
                payload = first.to_dict()
                payload.update(observed_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
                               expires_at=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat())
                capability_path(kwargs["state_dir"], first_profile.profile_id).write_text(json.dumps(payload))
            return receipt
        self.controller.preflight_fn = expire
        self.assertIn("exact fresh persisted", self.controller.handle(command).messages[0])
        self.controller.spawn_fn.assert_not_called()
        self.assertEqual(len(self.requests), 2)

    def test_cancel_during_final_source_observation_prevents_dispatch(self):
        self.import_profiles([codex_profile()])
        _, command = self.approve_and_review()
        original = self.controller._launch_manifest
        observations = []
        def cancel_after_source(*args, **kwargs):
            value = original(*args, **kwargs)
            observations.append(value)
            if len(observations) == 2:
                self.controller.close_client()
            return value
        self.controller._launch_manifest = cancel_after_source
        response = self.controller.handle(command)
        self.assertIn("cancelled before supervisor dispatch", response.messages[0])
        self.controller.spawn_fn.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_cancel_while_writing_frozen_runbook_prevents_dispatch(self):
        self.import_profiles([codex_profile()])
        _, command = self.approve_and_review()
        original = self.controller.store.write_runbook
        def cancel_after_write(*args, **kwargs):
            path = original(*args, **kwargs)
            self.controller.close_client()
            return path
        self.controller.store.write_runbook = cancel_after_write
        self.assertIn("cancelled before supervisor dispatch", self.controller.handle(command).messages[0])
        self.controller.spawn_fn.assert_not_called()

    def test_process_compatibility_path_observes_detach_before_dispatch(self):
        from camol.workspace import WorkspaceManager
        self.import_profiles([None])
        self.controller.handle("/approve yes")
        original = WorkspaceManager.assert_source_ready
        def cancel_after_source(manager):
            original(manager)
            self.controller.close_client()
        with patch.object(WorkspaceManager, "assert_source_ready", cancel_after_source):
            self.assertIn("cancelled before supervisor dispatch", self.controller.handle("/run").messages[0])
        self.controller.spawn_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
