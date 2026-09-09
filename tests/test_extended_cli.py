import contextlib
import io
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camol import Harness
from camol.cli import main
from camol.artifacts import RunArchive
from camol.gate_runtime import acceptance_digest
from camol.schema import canonical_digest
from tests import test_api as api_fixture
from tests import test_evaluation as evaluation_fixture
from tests.test_gate_runtime import v5_plan
from tests.test_campaign import manifest
from camol.models import DownloadPlan, DownloadFile
from camol.runbook import load_runbook
from camol.watchers import WatchSpec
from camol.watch_runtime import JournalSource, journal_header, journal_fixture_digest


class ExtendedCliTests(unittest.TestCase):
    setUp = api_fixture.HarnessApiTests.setUp
    tearDown = api_fixture.HarnessApiTests.tearDown

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def test_read_only_missing_database_commands_do_not_create_state(self):
        missing = Path(self.temp.name) / "absent" / "run.db"
        for command in ("status", "events", "usage", "debug", "watchers", "profile", "logs"):
            code, _, error = self.invoke([command, "--db", str(missing)])
            self.assertEqual(code, 2)
            self.assertTrue(error)
            self.assertFalse(missing.parent.exists())

    def test_foreground_run_cannot_bypass_embedded_execution_owner(self):
        with Harness(self.workspace, self.state_dir):
            code, _, error = self.invoke(["run", str(api_fixture.ROOT / "examples/local-n-box-runbook.json"),
                                          "--workspace", str(self.workspace), "--state-dir", str(self.state_dir), "--approve-by", "owner"])
        self.assertEqual(code, 2)
        self.assertIn("another Camol supervisor", error)

    def test_repository_crawl_export_and_readonly_snapshot_queries(self):
        database = Path(self.temp.name) / "graph.sqlite3"
        code, text, error = self.invoke(["repo", "crawl", "--workspace", str(self.workspace), "--db", str(database), "--format", "json"])
        self.assertEqual(code, 0, error)
        snapshot = json.loads(text)
        self.assertGreater(snapshot["node_count"], 0)
        code, text, error = self.invoke(["repo", "export", "--db", str(database), "--format", "dot"])
        self.assertEqual(code, 0, error)
        self.assertIn("digraph camol", text)
        code, text, error = self.invoke(["repo", "diff", snapshot["snapshot_id"], snapshot["snapshot_id"], "--db", str(database)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["nodes"]["changed"], [])

    def test_campaign_manifest_digest_is_shown_before_execution(self):
        path = Path(self.temp.name) / "campaign.json"
        path.write_text(json.dumps(manifest()), encoding="utf-8")
        code, text, error = self.invoke(["campaign", "validate", "--manifest", str(path)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["expected_trials"], 6)
        self.assertTrue(json.loads(text)["manifest_digest"].startswith("sha256:"))

    def test_capacity_publish_requires_exact_owner_declared_approval_before_creating_state(self):
        database = Path(self.temp.name) / "uncreated" / "capacity.sqlite3"
        code, _, _ = self.invoke(["capacity", "publish", "--db", str(database), "--namespace", "test"])
        self.assertEqual(code, 2)
        self.assertFalse(database.parent.exists())
        now = datetime.now(timezone.utc)
        supply = dict(schema="camol.capacity_supply", schema_version=1, namespace="test", pool_id="runtime",
                      kind="runtime", limits={"concurrency": 2}, outside_usage={"concurrency": 0}, capabilities=["code"],
                      attributes={}, rate_limit=None, status="ready", provenance="owner_declared", source="owner/test-owner",
                      observed_at=(now - timedelta(seconds=1)).isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())
        path = Path(self.temp.name) / "supply.json"
        path.write_text(json.dumps(supply), encoding="utf-8")
        arguments = ["capacity", "publish", "--db", str(database), "--namespace", "test", "--supply", str(path), "--by", "test-owner"]
        code, _, _ = self.invoke(arguments + ["--digest", canonical_digest("wrong")])
        self.assertEqual(code, 2)
        self.assertFalse(database.parent.exists())
        code, text, error = self.invoke(arguments + ["--digest", canonical_digest(supply)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["supply_digest"], canonical_digest(supply))
        code, text, error = self.invoke(["capacity", "inventory", "--db", str(database), "--namespace", "test"])
        self.assertEqual(code, 0, error)
        self.assertIn("owner_declared", text)

    def test_control_approval_forwards_exact_identity_and_digest(self):
        async def reply(*args, **kwargs):
            self.assertEqual(args, (self.state_dir, "gate-approve"))
            self.assertEqual(kwargs["requested_by"], "owner")
            self.assertEqual(kwargs["params"], dict(approved_by="owner", task_id="build", assessment_digest="sha256:" + "a" * 64))
            return {"result": {"accepted": True}}
        with patch("camol.cli.send_control_v2", reply):
            code, text, error = self.invoke(["ctl", "gate-approve", "--state-dir", str(self.state_dir), "--by", "owner",
                                            "--task-id", "build", "--digest", "sha256:" + "a" * 64])
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(text)["accepted"])

    def test_model_prepare_and_approve_are_not_download_or_inference(self):
        root = Path(self.temp.name) / "models"
        code, _, _ = self.invoke(["models", "list", "--root", str(root)])
        self.assertEqual(code, 2)
        self.assertFalse(root.exists())
        plan = DownloadPlan("tiny-plan", "tiny-model", "pinned-revision", "owner",
                            (DownloadFile("model.gguf", "https://example.invalid/model.gguf", canonical_digest("model"), 1024),),
                            ("https://example.invalid",), 1024, 2048)
        path = Path(self.temp.name) / "model-plan.json"
        path.write_text(json.dumps(plan.to_dict()))
        with patch("camol.models.HTTPSDownloadTransport.open", side_effect=AssertionError("unapproved network call")):
            code, text, error = self.invoke(["models", "validate", "--plan", str(path)])
            self.assertEqual(code, 0, error)
            self.assertFalse(json.loads(text)["starts_download"])
            self.assertFalse(root.exists())
            code, text, error = self.invoke(["models", "prepare", "--plan", str(path), "--root", str(root)])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(text)["status"], "planned")
            code, _, _ = self.invoke(["models", "approve", "--digest", plan.digest(), "--by", "wrong-owner", "--root", str(root)])
            self.assertEqual(code, 2)
            code, text, error = self.invoke(["models", "approve", "--digest", plan.digest(), "--by", "owner", "--root", str(root)])
            self.assertEqual(code, 0, error)
            result = json.loads(text)
            self.assertEqual(result["status"], "approved")
            self.assertEqual(result["loaded"], "unverified")
            self.assertEqual(result["inference_ready"], "unverified")
            self.assertEqual(result["accounted_bytes"], 0)

    def test_offline_swebench_cli_never_prints_oracle_or_starts_grader(self):
        from tests.test_swebench import fixture_files
        raw, prepared, lock = fixture_files(Path(self.temp.name))
        lock_path = Path(self.temp.name) / "suite-lock.json"
        lock_path.write_text(json.dumps(lock))
        with patch("camol.swebench.OfficialSWEBenchGrader.grade", side_effect=AssertionError("must not grade")):
            code, text, error = self.invoke(["swebench", "inspect", "--dataset", str(raw), "--prepared", str(prepared), "--lock", str(lock_path)])
        self.assertEqual(code, 0, error)
        result = json.loads(text)
        self.assertFalse(result["grader_executed"])
        self.assertTrue(result["oracle_bodies_excluded"])
        self.assertNotIn("gold fix", text)
        self.assertNotIn("protected regression", text)
        self.assertEqual(len(result["worker_inputs"]), 1)

    def test_watch_cli_freezes_schedules_reads_and_inspects_real_local_journal(self):
        with Harness(self.workspace, self.state_dir) as harness:
            initial = harness.prepare(load_runbook(api_fixture.ROOT / "examples/local-n-box-runbook.json"))
            harness.approve(by="owner", digest=initial["plan_digest"])
        now = datetime.now(timezone.utc)
        spec = WatchSpec("build-events", "local-build", "journal:local-build", ("started", "ended"), ("build",),
                         "ended", (("build", "one"),), "build-v1", "jsonl-v1", canonical_digest("placeholder"))
        spec = replace(spec, fixture_digest=journal_fixture_digest(spec))
        journal = Path(self.temp.name) / "source.jsonl"
        observation = dict(event_id="one", revision=1, event_class="ended", correlation={"build": "one"},
                           occurred_at=now.isoformat(), content_digest=canonical_digest("artifact"))
        journal.write_text(json.dumps(journal_header(spec)) + "\n" + json.dumps(observation) + "\n")
        journal.chmod(0o600)
        schedule = dict(schema="camol.watch_schedule", schema_version=1, watcher_id=spec.watcher_id,
                        binding=JournalSource().binding(spec.source_id, journal), interval_seconds=1,
                        max_polls=2, expires_at=(now + timedelta(minutes=5)).isoformat())
        spec_path, schedule_path = Path(self.temp.name) / "watch.json", Path(self.temp.name) / "schedule.json"
        spec_path.write_text(json.dumps(spec.to_dict()))
        schedule_path.write_text(json.dumps(schedule))
        shared = ["--workspace", str(self.workspace), "--state-dir", str(self.state_dir)]
        for arguments in (["watch", "create", "--spec", str(spec_path), "--by", "owner", "--digest", canonical_digest(spec.to_dict())],
                          ["watch", "schedule", "--schedule", str(schedule_path), "--by", "owner", "--digest", canonical_digest(schedule)]):
            code, _, error = self.invoke(arguments + shared)
            self.assertEqual(code, 0, error)
        code, text, error = self.invoke(["watch", "poll"] + shared)
        self.assertEqual(code, 0, error)
        result = json.loads(text)[spec.watcher_id]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scheduled_polls"], 1)
        code, text, error = self.invoke(["watch", "inspect", "--state-dir", str(self.state_dir)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)[spec.watcher_id]["cursor"], result["cursor"])


class RevisionApiTests(unittest.TestCase):
    setUp = evaluation_fixture.EvaluationLoopTests.setUp
    tearDown = evaluation_fixture.EvaluationLoopTests.tearDown

    def test_public_api_revises_rebuilds_accepts_and_exports_both_ledgers(self):
        with Harness(self.source, self.state) as run:
            prepared = run.prepare(v5_plan("embedded-first"))
            run.approve(by="human-owner", digest=prepared["plan_digest"])
            first = run.run()
            self.assertEqual(first["status"], "awaiting_acceptance")
            run.accept(by="human-owner", outcome_digest=acceptance_digest(first))
            proposal = run.propose_revision(v5_plan("embedded-second"), reason="reviewed successor")
            with patch.object(run.runner, "close", wraps=run.runner.close) as close_prior:
                second = run.apply_revision(by="human-owner", proposal_digest=proposal["proposal_digest"])
                close_prior.assert_called_once_with()
            self.assertEqual(second["revision"]["base_revision"], first["integration_head"])
            self.assertEqual(second["tasks"]["change"]["completed_step_ids"], [])
            final = run.run()
            self.assertEqual(final["status"], "awaiting_acceptance")
            final = run.accept(by="human-owner", outcome_digest=acceptance_digest(final))
            exported = self.root / "revision-archive"
            run.export(exported)
            self.assertEqual(RunArchive.replay(exported), final)
        with Harness(self.source, self.state) as reopened:
            self.assertEqual(reopened.run_id, "embedded-second")
            self.assertEqual(reopened.run()["status"], "completed")


if __name__ == "__main__":
    unittest.main()
