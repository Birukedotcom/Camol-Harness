import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol import Harness
from camol.artifacts import ArtifactStore, RunArchive
from camol.cli import main
from camol.events import new_event
from camol.gate_runtime import acceptance_digest
from camol.orchestrator import Orchestrator
from camol.schema import canonical_digest
from camol.state import project, apply_event
from camol.store import SQLiteEventStore, ConcurrentAppendError
from camol.vcs import VCSError, RELATIONS, propose, impact, snapshot, render_snapshot
from tests import test_api as fixture


class VCSLineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture.HarnessApiTests()
        cls.fixture.setUp()
        with Harness(cls.fixture.workspace, cls.fixture.state_dir) as harness:
            state = harness.prepare(fixture.ROOT / "examples/local-n-box-runbook.json")
            harness.approve(by="owner", digest=state["plan_digest"])
            cls.final = harness.run()
            assert cls.final["status"] == "completed", cls.final
            cls.events = harness.events()
        cls.ids = sorted(cls.final["candidates"])
        assert len(cls.ids) >= 3

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SQLiteEventStore(self.root / "camol.sqlite3")
        self.addCleanup(self.store.close)
        self.store.append_many(copy.deepcopy(self.events))
        self.orchestrator = Orchestrator(self.store)
        self.run_id = self.final["run_id"]

    def proposal(self, **changes):
        values = dict(source=self.ids[0], target=self.ids[1], relation="absorbs", reason="Fold reviewed code")
        values.update(changes)
        return self.orchestrator.propose_vcs_relation(self.run_id, **values)

    def accept(self, proposal):
        return self.orchestrator.apply_vcs_relation(self.run_id, proposal, by="owner", review_digest=proposal["digest"])

    def test_real_candidates_expose_exact_code_and_integration_identity_without_remote_claims(self):
        report = self.orchestrator.vcs_snapshot(self.run_id)
        self.assertEqual(report["digest"], snapshot(project(self.events))["digest"])
        for node in report["nodes"]:
            original = self.final["candidates"][node["candidate_id"]]
            self.assertEqual(node["candidate_digest"], canonical_digest(original))
            self.assertEqual(node["base_revision"], original["salvage"]["base_revision"])
            self.assertTrue(node["integrations"])
            self.assertIsNone(node["remote_push_receipt"])
            self.assertIsNone(node["pull_request"])
            self.assertEqual(node["remote_review_state"], "not_observed")
        report["nodes"][0]["integrations"].clear()
        self.assertTrue(self.orchestrator.vcs_snapshot(self.run_id)["nodes"][0]["integrations"])

    def test_human_review_add_remove_retry_restart_export_and_gate_invariance(self):
        before = self.orchestrator.state(self.run_id)
        proposal = self.proposal()
        self.assertEqual(before, self.orchestrator.state(self.run_id), "proposal cannot append")
        self.accept(proposal)
        after = self.orchestrator.state(self.run_id)
        self.assertEqual(acceptance_digest(before), acceptance_digest(after))
        self.assertEqual(before["tasks"], after["tasks"])
        self.assertEqual(before["total_tokens"], after["total_tokens"])
        self.assertEqual(len(after["vcs_relations"]), 1)
        self.assertEqual(self.accept(proposal), proposal)
        self.assertEqual(after, self.orchestrator.state(self.run_id))
        removal = self.proposal(action="remove", reason="Retain independent implementations")
        self.accept(removal)
        removed = self.orchestrator.state(self.run_id)
        self.accept(proposal)
        self.assertEqual(removed, self.orchestrator.state(self.run_id), "retry cannot resurrect a removed edge")
        self.assertEqual(Orchestrator(self.store).state(self.run_id), removed)
        archive = self.root / "export"
        RunArchive.export(self.run_id, self.store.read(self.run_id),
                          ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), removed)

    def test_all_relationships_are_explicit_and_conflicts_are_symmetric(self):
        for relation in RELATIONS:
            self.accept(self.proposal(relation=relation))
        report = self.orchestrator.vcs_snapshot(self.run_id)
        self.assertEqual({row["relation"] for row in report["relations"]}, set(RELATIONS))
        with self.assertRaises(VCSError):
            self.proposal(source=self.ids[1], target=self.ids[0], relation="conflicts_with")
        with self.assertRaises(VCSError):
            self.proposal(source=self.ids[1], target=self.ids[0], relation="depends_on")

    def test_owner_tampering_stale_graph_unknown_fields_and_foreign_candidates_refuse_without_append(self):
        proposal = self.proposal()
        baseline = self.store.read(self.run_id)[-1]["seq"]
        for owner in ("other", next(iter(self.final["agents"]))):
            with self.assertRaises(VCSError):
                self.orchestrator.apply_vcs_relation(self.run_id, proposal, by=owner, review_digest=proposal["digest"])
        for change in ({"schema_version": True}, {"extra": "unexpected"}, {"plan_digest": "sha256:" + "0" * 64},
                       {"target": "foreign"}, {"action": []}, {"graph_digest": "sha256:" + "0" * 64}):
            altered = dict(proposal, **change)
            altered["digest"] = canonical_digest({key: value for key, value in altered.items() if key != "digest"})
            with self.assertRaises(ValueError):
                self.accept(altered)
        self.assertEqual(self.store.read(self.run_id)[-1]["seq"], baseline)
        self.accept(self.proposal(relation="backports"))
        baseline = self.store.read(self.run_id)[-1]["seq"]
        with self.assertRaises(VCSError):
            self.accept(proposal)
        self.assertEqual(self.store.read(self.run_id)[-1]["seq"], baseline)

    def test_append_race_rejects_a_stale_write_without_corrupting_replay(self):
        first, second = self.proposal(), self.proposal(relation="backports")
        other = Orchestrator(self.store)
        original = self.orchestrator._emit
        def race(*args, **kwargs):
            other.apply_vcs_relation(self.run_id, second, by="owner", review_digest=second["digest"])
            return original(*args, **kwargs)
        with patch.object(self.orchestrator, "_emit", side_effect=race), self.assertRaises(ConcurrentAppendError):
            self.accept(first)
        self.assertEqual(len(project(self.store.read(self.run_id))["vcs_changes"]), 1)

    def test_replay_rejects_worker_authorship_forgery_and_mid_execution_annotations(self):
        proposal = self.proposal()
        for actor in ("other", next(iter(self.final["agents"]))):
            with self.assertRaises(VCSError):
                project(self.events + [new_event(self.run_id, "VCS_RELATION_CHANGED", actor, proposal)])
        state = copy.deepcopy(self.final)
        next(iter(state["tasks"].values()))["lease_id"] = "active-lease"
        with self.assertRaises(VCSError):
            propose(state, source=self.ids[0], target=self.ids[1], relation="absorbs", reason="No active lease changes")
        with self.assertRaises(VCSError):
            apply_event(state, new_event(self.run_id, "VCS_RELATION_CHANGED", "owner", proposal))

    def test_impact_combines_candidate_consumers_and_task_dependencies_without_rewriting_evidence(self):
        self.accept(self.proposal(source=self.ids[1], target=self.ids[0], relation="depends_on"))
        self.accept(self.proposal(source=self.ids[2], target=self.ids[1], relation="absorbs"))
        self.accept(self.proposal(source=self.ids[0], target=self.ids[2], relation="conflicts_with"))
        state = self.orchestrator.state(self.run_id)
        original = copy.deepcopy(state)
        report = impact(state, candidates=[self.ids[0]], change="fold")
        self.assertTrue(set(self.ids[:3]) <= set(report["candidate_ids"]))
        self.assertEqual(report["requested_candidate_ids"], [self.ids[0]])
        self.assertEqual(len(report["review_relationships"]), 1)
        self.assertTrue(all(item["phases"] == ["candidate", "integration"] for item in report["tasks"]))
        for change in ("environment", "plan"):
            report = impact(state, candidates=[self.ids[0]], change=change)
            self.assertEqual({item["task_id"] for item in report["tasks"]}, set(state["tasks"]))
        self.assertEqual(state, original)

    def test_invalid_inputs_limits_and_secret_reason(self):
        for changes in ({"source": self.ids[1]}, {"relation": "merges"}, {"reason": "\x1b[31m"}, {"reason": " "}, {"reason": "x" * 2001}):
            with self.assertRaises(ValueError):
                self.proposal(**changes)
        for candidates in ([], ["foreign"], [self.ids[0], self.ids[0]], [True], [["bad"]]):
            with self.assertRaises(VCSError):
                impact(self.final, candidates=candidates, change="merge")
        with patch("camol.vcs.MAX_NODES", 1), self.assertRaises(VCSError):
            snapshot(self.final)
        with patch("camol.vcs.MAX_CHANGES", 0), self.assertRaises(VCSError):
            self.proposal()
        secret = "sk-ant-" + "a" * 90
        proposal = self.proposal(reason="Credential " + secret)
        self.accept(proposal)
        self.assertNotIn(secret, json.dumps(self.store.read(self.run_id)))

    def test_large_combined_graph_uses_iterative_closure_and_paged_literal_display(self):
        # Algorithm-only synthetic projection; the other tests use a real build
        # and replay. This deliberately exceeds Python's usual recursion limit.
        state = copy.deepcopy(self.final)
        template = state["candidates"][self.ids[0]]
        task = state["tasks"][template["task_id"]]
        state["candidates"], state["tasks"], state["vcs_relations"] = {}, {}, {}
        for index in range(1200):
            key, task_id = "stress-{}".format(index), "task-{}".format(index)
            state["candidates"][key] = dict(template, candidate_id=key, task_id=task_id)
            state["tasks"][task_id] = dict(task, id=task_id, depends_on=[])
            if index:
                edge = dict(source=key, target="stress-{}".format(index - 1), relation="absorbs")
                identity = canonical_digest(edge)
                state["vcs_relations"][identity] = dict(edge, relation_id=identity)
        report = impact(state, candidates=["stress-0"], change="rebase")
        self.assertEqual(len(report["candidate_ids"]), 1200)
        self.assertEqual(len(report["tasks"]), 1200)
        display = render_snapshot(snapshot(state), offset=1199, limit=1)
        self.assertIn("1200 candidates / 1199 relationships", display)
        self.assertLess(len(display), 2000)
        with self.assertRaises(VCSError):
            render_snapshot(snapshot(state), offset=True)

    def test_orchestrator_slash_view_is_readonly_paged_and_present_in_palette(self):
        from camol.app import InteractiveController, SLASH_COMMANDS
        controller = InteractiveController(self.fixture.workspace, state_root=self.root / "ui")
        controller.session = controller.store.update(controller.session, state_dir=str(self.root), run_id=self.run_id)
        with patch.object(controller.connections, "refresh", side_effect=AssertionError("no probe")), \
                patch.object(controller, "spawn_fn", side_effect=AssertionError("no process")):
            response = controller.handle("/vcs")
            self.assertIn("CAMOL VCS", response.messages[0])
            self.assertTrue(response.focus_orchestrator)
            self.assertIn("0 relationships", controller.handle("/vcs 50").messages[0])
            self.assertIn("denied", controller.handle("/vcs -1").messages[0])
            self.assertIn("denied", controller.handle("/vcs extra args").messages[0])
        controller.session = controller.store.update(controller.session, state_dir=str(self.root / "absent"))
        self.assertIn("has not run", controller.handle("/vcs").messages[0])
        self.assertIn("/vcs", {item.command for item in SLASH_COMMANDS})

    def test_cli_and_embedding_share_the_reviewed_ledger_and_offline_reads_are_noncreating(self):
        common = ["--state-dir", str(self.root), "--run-id", self.run_id]
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["vcs", "propose", *common, "--source", self.ids[0], "--target", self.ids[1],
                                   "--relation", "absorbs", "--reason", "Owner review"]), 0)
        proposal = json.loads(output.getvalue())
        proposal_path = self.root / "review.json"
        proposal_path.write_text(json.dumps(proposal))
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["vcs", "apply", *common, "--workspace", str(self.fixture.workspace),
                "--proposal", str(proposal_path), "--by", "owner", "--review-digest", proposal["digest"]]), 0)
        with Harness(self.fixture.workspace, self.root) as harness:
            self.assertEqual(len(harness.vcs_snapshot()["relations"]), 1)
            self.assertTrue(harness.vcs_impact(candidates=[self.ids[0]], change="rebase")["tasks"])
            self.assertEqual(harness.apply_vcs_relation(proposal, by="owner", review_digest=proposal["digest"]), proposal)
        baseline = self.store.read(self.run_id)[-1]["seq"]
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["vcs", "inspect", *common]), 0)
            self.assertEqual(main(["vcs", "impact", *common, "--candidate", self.ids[0], "--change", "merge"]), 0)
        self.assertEqual(self.store.read(self.run_id)[-1]["seq"], baseline)
        missing = self.root / "absent"
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["vcs", "inspect", "--state-dir", str(missing), "--run-id", self.run_id]), 2)
        self.assertFalse(missing.exists())
