import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from camol.admission import AdmissionController
from camol.providers import create_claude_capability, load_model_profile
from camol.runbook import (
    migrate_runbook_v1_to_v2,
    migrate_runbook_v2_to_v3,
    runbook_digest,
)
from camol.workspace import WorkspaceManager
from tests.test_providers import Completed, profile_payload


ROOT = Path(__file__).resolve().parents[1]


class HostedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.state = root / "state"
        self.bin = root / "bin"
        (self.source / "profiles/models").mkdir(parents=True)
        self.bin.mkdir()
        self.profile_path = self.source / "profiles/models/test-fable.yaml"
        self.profile_path.write_text(json.dumps(profile_payload()), encoding="utf-8")
        self.runtime = self.bin / "fake-claude"
        self.runtime.write_text(
            """#!/usr/bin/env python3
import json, sys
if '--version' in sys.argv:
    print('fake-claude 1.0')
elif sys.argv[1:] == ['auth', 'status', '--json']:
    print(json.dumps({'loggedIn': True}))
else:
    raise SystemExit(2)
""",
            encoding="utf-8",
        )
        self.runtime.chmod(0o755)
        subprocess.run(["git", "-C", str(self.source), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-q", "-m", "fixture"], check=True)
        raw = json.loads((ROOT / "examples/three-agent-runbook.json").read_text())
        v2 = migrate_runbook_v1_to_v2(
            raw,
            readiness_policy={"receipt_ttl_seconds": 60},
            trust_tiers={agent["id"]: "developer_trusted" for agent in raw["agents"]},
        )
        self.runbook = migrate_runbook_v2_to_v3(v2)
        self.runbook["agents"][0]["adapter"] = {
            "kind": "claude_cli",
            "profile": "profiles/models/test-fable.yaml",
            "timeout_seconds": 30,
        }
        self.now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def test_hosted_candidate_is_red_then_green_only_after_bound_preflight(self):
        path = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        with patch.dict(os.environ, {"PATH": path}):
            manager = WorkspaceManager(self.source, self.state)
            controller = AdmissionController(
                self.runbook, manager, target_id="local:test", clock=lambda: self.now,
            )
            task = self.runbook["tasks"][0]
            agent = self.runbook["agents"][0]
            red, _ = controller.prepare(
                plan_digest=runbook_digest(self.runbook), task=task, agent=agent, granted_by="human",
            )
            self.assertEqual(red.receipt.status, "red")
            self.assertFalse(red.decision(
                now=self.now.isoformat(), plan_digest=runbook_digest(self.runbook),
                plan_frozen=True, dependencies_green=True,
            ).ready)
            profile = load_model_profile(self.source, "profiles/models/test-fable.yaml")
            create_claude_capability(
                profile, target_id="local:test", state_dir=self.state, cwd=self.source,
                accept_spend=True, now=self.now, runner=lambda *args, **kwargs: Completed(),
            )
            green, _ = controller.prepare(
                plan_digest=runbook_digest(self.runbook), task=task, agent=agent, granted_by="human",
            )
        self.assertEqual(green.receipt.status, "green")
        self.assertEqual(green.receipt.requested_model, "fable")
        self.assertEqual(green.receipt.credential_scopes, ("existing-login",))
        self.assertEqual(green.reservation.max_usd_cents, 10)
        self.assertIn("network", green.authority_policy.required_capabilities)
        self.assertTrue(green.decision(
            now=self.now.isoformat(), plan_digest=runbook_digest(self.runbook),
            plan_frozen=True, dependencies_green=True,
        ).ready)


if __name__ == "__main__":
    unittest.main()
