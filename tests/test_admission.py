import copy
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from camol.admission import AdmissionBundle, AdmissionController
from camol.runbook import load_runbook, runbook_digest
from camol.workspace import WorkspaceManager


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class AdmissionControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        (self.source / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", self.source / "examples/fake_agent.py")
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Camol Test")
        git(self.source, "config", "user.email", "camol@example.invalid")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "fixture")
        self.runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        self.manager = WorkspaceManager(self.source, self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def controller(self, runbook=None):
        return AdmissionController(runbook or self.runbook, self.manager, clock=lambda: NOW)

    def test_complete_bundle_is_ready_round_trips_and_binds_isolated_workspace(self):
        task = self.runbook["tasks"][0]
        agent = self.runbook["agents"][0]
        bundle, handle = self.controller().prepare(
            plan_digest=runbook_digest(self.runbook), task=task, agent=agent, granted_by="owner"
        )
        decision = bundle.decision(
            now=NOW.isoformat(),
            plan_digest=runbook_digest(self.runbook),
            plan_frozen=True,
            dependencies_green=True,
        )
        self.assertTrue(decision.ready, [reason.detail for reason in decision.reasons])
        self.assertEqual(bundle.workspace.path, str(handle.path))
        self.assertEqual(bundle.workspace.filesystem_policy, "isolated_worktree_write")
        self.assertEqual(bundle.receipt.reservation_id, bundle.reservation.reservation_id)
        self.assertEqual(AdmissionBundle.from_dict(bundle.to_dict()).digest(), bundle.digest())
        malformed = bundle.to_dict()
        malformed["control_plane_ready"] = "yes"
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            AdmissionBundle.from_dict(malformed)

    def test_hosted_unverified_adapter_cannot_pass_without_provider_proof(self):
        runbook = copy.deepcopy(self.runbook)
        runbook["run"]["id"] = "hosted-run"
        runbook["agents"][0]["adapter"]["argv"] = ["claude"]
        bundle, _ = self.controller(runbook).prepare(
            plan_digest=runbook_digest(runbook),
            task=runbook["tasks"][0],
            agent=runbook["agents"][0],
            granted_by="owner",
        )
        decision = bundle.decision(
            now=NOW.isoformat(), plan_digest=runbook_digest(runbook), plan_frozen=True, dependencies_green=True
        )
        self.assertFalse(decision.ready)
        provider = next(probe for probe in bundle.receipt.probes if probe.kind == "provider")
        self.assertEqual(provider.status, "unknown")
        self.assertEqual(provider.reason_code, "AUTH_REQUIRED")

    def test_managed_retry_probes_the_exact_current_dirty_digest(self):
        task = self.runbook["tasks"][0]
        agent = self.runbook["agents"][0]
        first, handle = self.controller().prepare(
            plan_digest=runbook_digest(self.runbook), task=task, agent=agent, granted_by="owner"
        )
        tracked = handle.path / "examples" / "fake_agent.py"
        tracked.write_text(tracked.read_text(encoding="utf-8") + "\n# candidate\n", encoding="utf-8")
        (handle.path / "changed.txt").write_text("candidate", encoding="utf-8")
        second, _ = self.controller().prepare(
            plan_digest=runbook_digest(self.runbook), task=task, agent=agent, granted_by="owner"
        )
        self.assertNotEqual(first.workspace.dirty_digest, second.workspace.dirty_digest)
        repository = next(probe for probe in second.receipt.probes if probe.probe_id == "source.repository")
        self.assertEqual(repository.status, "green")


if __name__ == "__main__":
    unittest.main()
