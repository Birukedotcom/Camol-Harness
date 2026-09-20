import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import camol
from camol import Harness
from camol.artifacts import ArtifactStore, RunArchive
from camol.debug_execution import source_identity
from camol.schema import canonical_digest
from camol.source_handoff import authorize, export_source, propose, receive_source, validate_proposal
from camol.state import project
from camol.store import SQLiteEventStore
from tests import test_api as fixture


class TaskSourceHandoffTests(unittest.TestCase):
    contract_path = Path("camol-boxes/strategist/contract.txt")
    inventory_path = Path("camol-boxes/builder/inventory.txt")
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture.HarnessApiTests()
        cls.fixture.setUp()
        cls.addClassCleanup(cls.fixture.tearDown)
        cls.source = cls.fixture.workspace.resolve()
        with Harness(cls.source, cls.fixture.state_dir) as harness:
            plan = json.loads((fixture.ROOT / "examples/local-n-box-runbook.json").read_text())
            plan["run"]["max_concurrency"] = 1
            tasks = {item["id"]: item for item in plan["tasks"]}
            tasks["inventory"]["depends_on"] = ["frame"]
            tasks["challenge"]["depends_on"] = ["inventory"]
            spare = copy.deepcopy(tasks["inventory"])
            spare.update(id="spare", depends_on=["frame"])
            plan["tasks"].append(spare)
            state = harness.prepare(plan)
            harness.orchestrator.bind_source(state["run_id"], source_identity(cls.source))
            harness.approve(by="owner", digest=state["plan_digest"])
            descriptor = dict(schema="camol.execution_target", schema_version=1, target_id="receiver",
                generation="generation", control_plane_id="control", label="copy fixture", ownership="adopted",
                provider=dict(kind="local", account="owner", project="fixture", location="local", resource_id="copy", resource_name="copy"),
                transport=dict(kind="local", profile_digest="sha256:" + "a" * 64))
            now = datetime.now(timezone.utc)
            cls.expiry = (now + timedelta(minutes=30)).isoformat()
            adopted = harness.targets.propose(descriptor, by="owner", expires_at=cls.expiry)
            harness.targets.adopt(adopted, by="owner", approval_digest=adopted["digest"])
            cls.adoption = adopted["digest"]
            cls.initial = harness.state()
            first = harness.run(should_pause=lambda: harness.state()["tasks"]["frame"]["status"] == "succeeded")
            assert first["tasks"]["frame"]["status"] == "succeeded", first
            assert first["tasks"]["inventory"]["attempts"] == 0, first
            cls.first_events = harness.events()
            cls.first = harness.state()
            second = harness.run(should_pause=lambda: harness.state()["tasks"]["inventory"]["status"] == "succeeded")
            assert second["tasks"]["inventory"]["status"] == "succeeded", second
            assert second["tasks"]["spare"]["attempts"] == 0, second
            cls.second = harness.state()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state_dir = self.root / "state"
        store = SQLiteEventStore(self.state_dir / "camol.sqlite3")
        try:
            store.append_many(copy.deepcopy(self.first_events))
        finally:
            store.close()
        self.harness = Harness(self.source, self.state_dir).__enter__()
        self.addCleanup(self.harness.close)
        self.key = os.urandom(32)
        self.output = self.root / "receiver"
        self.package = self.root / "package"
        self.arguments = dict(generation="generation", adoption_digest=self.adoption,
            destination_workspace=str(self.output), request_id="task-copy", by="owner",
            issued_at=datetime.now(timezone.utc).isoformat(), expires_at=self.expiry, task_id="inventory")

    def proposal(self, **changes):
        return self.harness.propose_source_handoff(**dict(self.arguments, **changes))

    def export(self, proposal):
        return self.harness.export_source_handoff(proposal, by="owner", review_digest=proposal["digest"], key=self.key, output=self.package)

    def receive(self, proposal):
        return receive_source(archive=self.package, key=self.key, proposal=proposal, review_digest=proposal["digest"],
            by="owner", target_id="receiver", generation="generation", output=self.output)

    def test_real_dependent_task_copy_includes_accepted_prior_changes_and_replays(self):
        before = self.harness.state()
        proposal = self.proposal()
        self.assertEqual(proposal["schema_version"], 2)
        self.assertEqual(proposal["selection"]["base_kind"], "accepted_integration")
        self.assertEqual(proposal["selection"]["source"]["revision"], self.first["integration_head"])
        self.assertNotEqual(self.first["integration_head"], self.initial["source_binding"]["source"]["revision"])
        self.export(proposal)
        received = self.receive(proposal)
        self.assertEqual(received["source"], dict(proposal["selection"]["source"], workspace=str(self.output)))
        self.assertTrue((self.output / self.contract_path).is_file())
        self.assertFalse((self.source / self.contract_path).exists())
        self.assertFalse((self.output / self.inventory_path).exists(), "handoff must not include later work")
        subprocess.run([sys.executable, "-c", "from pathlib import Path; assert 'objective' in Path('camol-boxes/strategist/contract.txt').read_text()"],
            cwd=self.output, check=True, timeout=5)
        self.assertEqual(self.harness.state()["tasks"], before["tasks"])
        self.assertEqual(self.harness.state()["total_tokens"], before["total_tokens"])
        self.assertFalse(received["execution_authority"])
        archive = self.root / "ledger-export"
        RunArchive.export(before["run_id"], self.harness.events(), ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), self.harness.state())

    def test_missing_dependencies_active_task_and_source_substitution_deny(self):
        for task_id in ("challenge", "integrate", "frame", "unknown"):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.proposal(task_id=task_id)
        with self.assertRaises(ValueError):
            self.proposal(source_workspace=str(self.source))
        proposal = self.proposal()
        for field, replacement in (("source", self.initial["source_binding"]["source"]),
                                   ("task_digest", "sha256:" + "0" * 64),
                                   ("origin_digest", "sha256:" + "0" * 64)):
            changed = copy.deepcopy(proposal)
            changed["selection"][field] = replacement
            self.rehash(changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.export(changed)
        self.assertFalse(self.package.exists())

    @staticmethod
    def rehash(proposal):
        selection = proposal["selection"]
        selection["digest"] = canonical_digest({k: v for k, v in selection.items() if k != "digest"})
        proposal["digest"] = canonical_digest({k: v for k, v in proposal.items() if k != "digest"})

    def test_stale_head_during_capture_and_lost_append_retry_do_not_publish(self):
        proposal = self.proposal(task_id="spare")
        self.assertIn(self.second["tasks"]["spare"]["status"], {"pending", "waiting"})
        self.assertNotEqual(self.first["integration_head"], self.second["integration_head"])
        from camol import source_handoff
        original = source_handoff._restore
        current = [self.first]
        def restore(*args):
            result = original(*args)
            current[0] = self.second
            return result
        with patch.object(source_handoff, "_restore", side_effect=restore), self.assertRaises(ValueError):
            export_source(proposal=proposal, by="owner", review_digest=proposal["digest"], get_state=lambda: current[0],
                          key=self.key, output=self.package)
        self.assertFalse(self.package.exists())
        with patch.object(self.harness.store, "append", side_effect=OSError("lost event")), self.assertRaises(OSError):
            self.export(proposal)
        original_bytes = (self.package / "source.camol").read_bytes()
        with self.assertRaises(ValueError):
            export_source(proposal=proposal, by="owner", review_digest=proposal["digest"], get_state=lambda: self.second,
                          key=self.key, output=self.package)
        self.assertEqual((self.package / "source.camol").read_bytes(), original_bytes)

    def test_initial_task_uses_baseline_and_v1_contract_remains_exact(self):
        initial = propose(self.initial, **dict(self.arguments, task_id="frame"))
        self.assertEqual(initial["selection"]["base_kind"], "approved_baseline")
        self.assertEqual(initial["selection"]["source"], self.initial["source_binding"]["source"])
        v1 = self.proposal(task_id=None)
        self.assertEqual(v1["schema_version"], 1)
        self.assertNotIn("selection", v1)
        self.export(v1)
        self.receive(v1)
        self.assertFalse((self.output / self.contract_path).exists())
        with self.assertRaises(ValueError):
            self.proposal(task_id=None, source_workspace=str(self.source))

    def test_malformed_selection_and_schema_downgrade_are_controlled_denials(self):
        proposal = self.proposal()
        for value in (None, [], "integration", True):
            changed = dict(proposal, selection=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_proposal(changed)
        for value in ([], {}, True, "unknown"):
            changed = copy.deepcopy(proposal)
            changed["selection"]["base_kind"] = value
            self.rehash(changed)
            with self.subTest(kind=value), self.assertRaises(ValueError):
                validate_proposal(changed)
        with self.assertRaises(ValueError):
            validate_proposal(dict(proposal, schema_version=1))

    def test_planning_cannot_block_active_embedded_execution_or_ignore_bad_paths(self):
        from camol.orchestrator import StateTransitionError
        with patch.object(self.harness, "_running", True), patch("camol.task_source.source_identity", side_effect=AssertionError("no read")):
            with self.assertRaises(StateTransitionError):
                self.proposal()
        for path in ("", "relative", 1, [], {}):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.proposal(source_workspace=path)

    def test_inherited_revision_requires_exact_existing_code_without_old_gates(self):
        from tests.test_gate_runtime import v5_plan
        revision = self.harness.orchestrator.propose_revision(self.first["run_id"], v5_plan("successor"), "Review inherited work")
        inherited = self.harness.orchestrator.apply_revision(self.first["run_id"], revision["proposal_digest"], "owner")
        self.harness.run_id = inherited["run_id"]
        descriptor = self.first["execution_targets"]["generation"]["proposal"]["descriptor"]
        adoption = self.harness.targets.propose(descriptor, by="owner", expires_at=self.expiry)
        self.harness.targets.adopt(adoption, by="owner", approval_digest=adoption["digest"])
        path = self.first["integrations"][-1]["workspace"]["path"]
        with self.assertRaises(ValueError):
            self.proposal(task_id="change", adoption_digest=adoption["digest"])
        proposal = self.proposal(task_id="change", source_workspace=path, adoption_digest=adoption["digest"])
        self.assertEqual(proposal["selection"]["base_kind"], "inherited_revision")
        self.export(proposal)
        self.receive(proposal)
        self.assertTrue((self.output / self.contract_path).exists())
        self.assertEqual(self.harness.state()["tasks"]["change"]["attempts"], 0)
        self.assertEqual(self.harness.state()["gate_assessments"], {})
        self.assertEqual(project(self.harness.events()), self.harness.state())

    def test_real_cli_plans_task_head_and_receiver_uses_same_versioned_contract(self):
        self.harness.close()
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "camol", "source-handoff", *args], cwd=self.root,
                env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
            return json.loads(result.stdout)
        common = ["--state-dir", str(self.state_dir), "--run-id", self.first["run_id"]]
        proposal = cli("plan", *common, "--generation", "generation", "--adoption-digest", self.adoption,
            "--destination-workspace", str(self.output), "--request-id", "task-copy", "--by", "owner",
            "--expires-at", self.expiry, "--task-id", "inventory")
        self.assertEqual(proposal["selection"]["source"]["revision"], self.first["integration_head"])
        proposal_file = self.root / "proposal.json"
        proposal_file.write_text(json.dumps(proposal))
        key_file = self.root / "key"
        key_file.write_bytes(self.key)
        key_file.chmod(0o600)
        approval = ["--proposal", str(proposal_file), "--by", "owner", "--review-digest", proposal["digest"], "--key-file", str(key_file)]
        cli("export", *common, *approval, "--workspace", str(self.source), "--output", str(self.package))
        received = cli("receive", *approval, "--archive", str(self.package), "--output", str(self.output),
            "--target-id", "receiver", "--generation", "generation")
        self.assertEqual(received["source"]["revision"], self.first["integration_head"])
