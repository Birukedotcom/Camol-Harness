import asyncio
import copy
import io
import json
import threading
import unittest
from unittest.mock import patch

from camol import Harness
from camol.artifacts import ArtifactStore, RunArchive
from camol.cli import main
from camol.events import new_event
from camol.gate_runtime import acceptance_digest
from camol.github_vcs import normalize
from camol.orchestrator import Orchestrator
from camol.schema import canonical_digest
from camol.state import project
from camol.vcs import VCSError
from tests.test_github_vcs import TARGET, SECRET, responses
from tests import test_vcs as lineage


class VCSObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lineage.VCSLineageTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        lineage.VCSLineageTests.tearDownClass.__func__(cls)

    def setUp(self):
        lineage.VCSLineageTests.setUp(self)
        self.integration = self.final["integrations"][0]
        self.candidate = self.integration["candidate_id"]
        self.calls = []

    def fetcher(self, target, revision, **kwargs):
        self.calls.append((target, revision))
        return normalize(responses(target, revision), target, revision)

    def observe(self, **changes):
        args = dict(candidate_id=self.candidate, integration_id=self.integration["integration_id"],
                    target=TARGET, by="owner", allow_network=True, request_id="read-1", fetcher=self.fetcher)
        args.update(changes)
        return self.orchestrator.observe_vcs(self.run_id, **args)

    def test_intent_finish_replay_export_and_exact_retry_preserve_gate_and_usage(self):
        before = self.orchestrator.state(self.run_id)
        old_relation = self.orchestrator.propose_vcs_relation(self.run_id, source=self.ids[0], target=self.ids[1], relation="absorbs", reason="Before observation")
        self.orchestrator.apply_vcs_relation(self.run_id, old_relation, by="owner", review_digest=old_relation["digest"])
        self.assertEqual(self.orchestrator.vcs_snapshot(self.run_id)["schema_version"], 1)
        record = self.observe(token="credential-not-retained")
        self.assertEqual(record["receipt"]["status"], "observed")
        self.assertEqual(record["request"]["transport"], "owner_embedding_callback")
        self.assertEqual(self.observe(token="another-not-retained"), record)
        self.assertEqual(len(self.calls), 1)
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(acceptance_digest(after), acceptance_digest(before))
        self.assertEqual(after["total_tokens"], before["total_tokens"])
        self.assertEqual(after["tasks"], before["tasks"])
        serialized = json.dumps(self.store.read(self.run_id))
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("credential-not-retained", serialized)
        graph = self.orchestrator.vcs_snapshot(self.run_id)
        self.assertEqual(graph["schema_version"], 2)
        node = next(item for item in graph["nodes"] if item["candidate_id"] == self.candidate)
        self.assertTrue(node["remote_ref_readback"]["matches_integration"])
        self.assertIsNone(node["remote_push_receipt"], "readback is not a push operation receipt")
        self.assertEqual(project(self.store.read(self.run_id)), after)
        archive = self.root / "export"
        RunArchive.export(self.run_id, self.store.read(self.run_id), ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), after)

    def test_unknown_new_read_never_falls_back_to_old_green_or_reissues_pending_request(self):
        self.observe()
        original = self.orchestrator._emit
        def refuse_finish(run_id, kind, *args, **kwargs):
            if kind == "VCS_OBSERVATION_FINISHED":
                raise OSError("fixture logging failure")
            return original(run_id, kind, *args, **kwargs)
        with patch.object(self.orchestrator, "_emit", side_effect=refuse_finish), self.assertRaises(VCSError):
            self.observe(request_id="read-pending")
        self.assertEqual(len(self.calls), 2)
        pending = self.observe(request_id="read-pending")
        self.assertIsNone(pending["receipt"])
        self.assertEqual(len(self.calls), 2)
        node = next(item for item in self.orchestrator.vcs_snapshot(self.run_id)["nodes"] if item["candidate_id"] == self.candidate)
        self.assertIsNone(node["remote_ref_readback"])
        self.assertEqual(node["remote_observations"][-1]["status"], "pending")
        self.assertEqual(Orchestrator(self.store).state(self.run_id), self.orchestrator.state(self.run_id))

    def test_scope_approval_and_intent_persistence_precede_any_network_callback(self):
        baseline = self.store.read(self.run_id)
        for changes in ({"allow_network": False}, {"allow_network": 1}, {"by": "worker"},
                        {"candidate_id": "foreign"}, {"integration_id": "foreign"},
                        {"request_id": ""}, {"timeout": 61}):
            with self.assertRaises(ValueError):
                self.observe(**changes)
        with patch.object(self.orchestrator, "_emit", side_effect=OSError("no intent")), self.assertRaises(OSError):
            self.observe()
        self.assertFalse(self.calls)
        self.assertEqual(self.store.read(self.run_id), baseline)
        self.observe()
        with self.assertRaises(VCSError):
            self.observe(target=dict(TARGET, pull_request=13))
        self.assertEqual(len(self.calls), 1)

    def test_invalid_callback_result_is_retained_as_unavailable_and_falsey_callable_is_used(self):
        class Falsey:
            def __bool__(self): return False
            def __call__(self, *args, **kwargs): return {"forged": "success"}
        with patch("camol.github_vcs.fetch", side_effect=AssertionError("do not substitute real network")):
            record = self.observe(fetcher=Falsey())
        self.assertEqual(record["receipt"]["status"], "unavailable")
        self.assertIsNone(record["receipt"]["result"])
        self.assertNotIn("forged", json.dumps(self.store.read(self.run_id)))

    def test_later_environment_does_not_rewrite_relationship_or_observation_history(self):
        proposal = self.orchestrator.propose_vcs_relation(self.run_id, source=self.ids[0], target=self.ids[1],
            relation="absorbs", reason="stable-owner-reason")
        self.orchestrator.apply_vcs_relation(self.run_id, proposal, by="owner", review_digest=proposal["digest"])
        self.observe()
        events = self.store.read(self.run_id)
        baseline = project(events)
        with patch.dict("os.environ", UNRELATED_SERVICE_TOKEN="stable-owner-reason"):
            self.assertEqual(project(events), baseline)

    def test_cancellation_is_recorded_and_propagates_even_if_final_logging_fails(self):
        def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.observe(fetcher=cancelled)
        record = self.orchestrator.state(self.run_id)["vcs_observations"]["read-1"]
        self.assertEqual(record["receipt"]["status"], "cancelled")
        original = self.orchestrator._emit
        def refuse_finish(run_id, kind, *args, **kwargs):
            if kind == "VCS_OBSERVATION_FINISHED": raise OSError("fixture")
            return original(run_id, kind, *args, **kwargs)
        with patch.object(self.orchestrator, "_emit", side_effect=refuse_finish), self.assertRaises(asyncio.CancelledError) as caught:
            self.observe(request_id="read-2", fetcher=cancelled)
        self.assertTrue(caught.exception.observation_recording_failed)

    def test_replay_rejects_worker_receipts_boolean_smuggling_and_changed_readback(self):
        self.observe()
        events = self.store.read(self.run_id)
        for index in (-1, -2):
            altered = copy.deepcopy(events)
            altered[index]["actor_id"] = next(iter(self.final["agents"]))
            with self.assertRaises(ValueError):
                project(altered)
        altered = copy.deepcopy(events)
        altered[-2]["payload"]["execution_authority"] = 0
        with self.assertRaises(ValueError):
            project(altered)
        altered = copy.deepcopy(events)
        data = altered[-1]["payload"]["result"]
        data["branch_readback"]["matches_integration"] = 1
        with self.assertRaises(ValueError):
            project(altered)
        altered = copy.deepcopy(events)
        altered[-1]["payload"]["request_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            project(altered)

    def test_cli_requires_network_opt_in_and_reads_retained_receipts_offline(self):
        target_file = self.root / "target.json"
        target_file.write_text(json.dumps(TARGET))
        args = ["vcs", "observe", "--state-dir", str(self.root), "--run-id", self.run_id,
                "--workspace", str(self.fixture.workspace), "--candidate", self.candidate,
                "--integration", self.integration["integration_id"], "--target", str(target_file),
                "--by", "owner", "--request-id", "cli-read"]
        with patch("camol.cli.Harness", side_effect=AssertionError("no owner acquisition")), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(args), 2)
        with patch("camol.github_vcs.fetch", side_effect=self.fetcher), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main([*args, "--allow-network"]), 0)
        output = io.StringIO()
        with patch("camol.github_vcs.fetch", side_effect=AssertionError("offline read")), patch("sys.stdout", output):
            self.assertEqual(main(["vcs", "observation", "--state-dir", str(self.root), "--run-id", self.run_id, "--request-id", "cli-read"]), 0)
        self.assertEqual(json.loads(output.getvalue())["receipt"]["status"], "observed")
        with Harness(self.fixture.workspace, self.root) as harness:
            self.assertEqual(harness.vcs_snapshot()["schema_version"], 2)
