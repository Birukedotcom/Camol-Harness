import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from camol.app import InteractiveController
from camol.planning import compile_runbook
from camol.schema import canonical_digest
from camol.supervisor import SupervisorPaths, send_control


ROOT = Path(__file__).resolve().parents[1]


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "repo"
        self.state_root = root / "state"
        (self.workspace / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", self.workspace / "examples/fake_agent.py")
        subprocess.run(["git", "-C", str(self.workspace), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-q", "-m", "fixture"], check=True)
        self.controller = InteractiveController(self.workspace, state_root=self.state_root)

    def tearDown(self):
        state_dir = Path(self.controller.session["state_dir"])
        if SupervisorPaths.under(state_dir).socket.exists():
            try:
                asyncio.run(send_control(state_dir, "stop", requested_by="test-owner"))
                deadline = time.time() + 5
                while SupervisorPaths.under(state_dir).socket.exists() and time.time() < deadline:
                    time.sleep(0.02)
            except Exception:
                pass
        self.temporary.cleanup()

    def test_boot_to_grill_approval_detached_run_box_completion_and_reattach(self):
        self.controller.handle("/grill produce an independently verified fixture")
        for answer in (
            "A content-addressed artifact and green independent verification",
            "Do not deploy or use hosted providers",
            "No credentials and no source-checkout writes",
            "implement | create the fixture artifact",
            "python3 -c \"from pathlib import Path; assert Path('artifacts/implement.txt').is_file()\"",
            "boxes=1 turns=3 tokens=12000 cost_cents=100 turn_timeout_seconds=600",
        ):
            final = self.controller.handle(answer)
        self.assertIn("Nothing has started", final.messages[0])

        # Replace the planning-only manual adapter with the repository's explicit
        # deterministic fixture adapter, preserving it beneath a new visible digest.
        plan = dict(self.controller.session["plan"])
        proposal = plan["proposal"]
        plan["runbook"] = compile_runbook(
            proposal,
            run_id=plan["run_id"],
            adapter={
                "kind": "process",
                "argv": ["python3", "{workspace}/examples/fake_agent.py", "{packet}", "{result}"],
                "timeout_seconds": 60,
            },
        )
        plan["execution_status"] = "ready"
        plan["execution_limitation"] = "TEST FIXTURE: deterministic local process evidence; no hosted-model evidence."
        digest = canonical_digest(plan)
        self.controller.session = self.controller.store.update(
            self.controller.session,
            plan=plan,
            plan_digest=digest,
            approved_digest=None,
            status="plan_ready",
        )
        self.assertFalse(Path(self.controller.session["state_dir"]).exists())
        denied = self.controller.handle("/run")
        self.assertIn("requires the exact current plan", denied.messages[0])
        self.assertFalse(Path(self.controller.session["state_dir"]).exists())

        self.controller.handle("/approve yes")
        started = self.controller.handle("/run")
        if len(started.messages) <= 1:
            log = SupervisorPaths.under(Path(self.controller.session["state_dir"])).log
            self.fail("{}\n{}".format(started.messages, log.read_text() if log.exists() else "no log"))
        self.assertIn("detached", started.messages[1])
        state_dir = Path(self.controller.session["state_dir"])
        self.assertTrue(SupervisorPaths.under(state_dir).socket.exists())

        deadline = time.time() + 20
        status = None
        while time.time() < deadline:
            status = self.controller._control("status")
            if status["run"]["status"] in {"completed", "blocked"}:
                break
            time.sleep(0.05)
        self.assertIsNotNone(status)
        diagnostics = self.controller._control("events", {"after_seq": 0, "limit": 100, "wait_ms": 0})
        self.assertEqual(
            status["run"]["status"], "completed",
            json.dumps({"status": status, "events": diagnostics}, indent=2),
        )
        self.assertEqual(status["mode"], "terminal")

        box = self.controller.handle("/box builder")
        self.assertIn("TASK_SUCCEEDED", box.messages[0])
        events = self.controller.handle("/events")
        self.assertIn("RUN_COMPLETED", events.messages[0])
        self.controller.handle("/quit")
        self.assertTrue(SupervisorPaths.under(state_dir).socket.exists())

        reattached = InteractiveController(self.workspace, state_root=self.state_root)
        self.assertEqual(reattached.session["session_id"], self.controller.session["session_id"])
        status_response = reattached.handle("/status")
        self.assertIn("supervisor=terminal", status_response.messages[0])


if __name__ == "__main__":
    unittest.main()
