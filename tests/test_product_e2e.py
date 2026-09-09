import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from camol.app import InteractiveController
from camol.planning import compile_runbook
from camol.schema import canonical_digest
from camol.supervisor import SupervisorPaths, send_control
from camol.store import ReadOnlyEventStore
from camol.state import project


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

    def stop_fixture(self, state_dir):
        paths = SupervisorPaths.under(state_dir)
        raced = None
        if paths.socket.exists():
            try:
                asyncio.run(send_control(state_dir, "stop", requested_by="test-owner"))
            except (ConnectionRefusedError, ConnectionResetError, FileNotFoundError) as error:
                raced = error
        if paths.lock.exists():
            deadline = time.monotonic() + 5
            stopped = False
            with paths.lock.open("r") as lock:
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        stopped = not paths.socket.exists() and not paths.pid.exists()
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                        if stopped:
                            break
                    time.sleep(.02)
            self.assertTrue(stopped, "fixture supervisor has not released its exact leader lock/control paths")
        if raced is not None:
            # Disappearance between exists() and connect is only tolerated after
            # both a persisted terminal outcome and actual leader exit are proved.
            self.assertTrue(paths.lock.is_file(), "a stop-race requires the exact supervisor lock as exit evidence")
            with closing(ReadOnlyEventStore(paths.database)) as store:
                state = project(store.read(store.latest_run_id()))
            self.assertIn(state["status"], {"completed", "blocked"}, str(raced))

    def tearDown(self):
        self.stop_fixture(Path(self.controller.session["state_dir"]))
        self.temporary.cleanup()

    def test_boot_to_grill_approval_detached_run_box_completion_and_reattach(self):
        self.controller.handle("/grill produce an independently verified fixture")
        for answer in (
            "A content-addressed artifact and green independent verification",
            "Do not deploy or use hosted providers",
            "No credentials and no source-checkout writes",
            "implement | create the fixture artifact",
            "python3 -c \"from pathlib import Path; assert Path('camol-boxes/builder/artifacts/implement.txt').is_file()\"",
            "boxes=1 turns=3 tokens=12000 cost_cents=100 turn_timeout_seconds=600",
        ):
            final = self.controller.handle(answer)
        self.assertIn("Nothing has started", final.messages[0])

        # An ordinary user can import an explicit process runbook. This goes
        # through the same visible frozen-plan path as the terminal command.
        proposal = self.controller.session["plan"]["proposal"]
        runbook = compile_runbook(
            proposal,
            run_id=self.controller.session["run_id"],
            adapter={
                "kind": "process",
                "argv": ["python3", "{workspace}/examples/fake_agent.py", "{packet}", "{result}"],
                "timeout_seconds": 60,
            },
        )
        runbook_path = self.workspace.parent / "process.runbook.json"
        runbook_path.write_text(json.dumps(runbook), encoding="utf-8")
        imported = self.controller.handle("/import " + str(runbook_path))
        self.assertIn("IMPORTED PLAN", imported.messages[0])
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
        self.assertIn("provider_observed_tokens", self.controller.handle("/usage run").messages[0])
        self.assertEqual(json.loads(self.controller.handle("/debug list").messages[0]), {})

    def test_second_completed_project_run_uses_a_new_ledger_and_boxes(self):
        self.test_boot_to_grill_approval_detached_run_box_completion_and_reattach()
        controller = self.controller
        first_state_dir = Path(controller.session["state_dir"])
        try:
            response = controller.handle("/grill make the second fixture")
            self.assertIn("GRILL 1", response.messages[0])
            self.assertNotEqual(Path(controller.session["state_dir"]), first_state_dir)
            for answer in (
                "Second fixture has independent green verification", "Do not deploy", "Preserve source",
                "second | produce the second fixture", "python3 -c 'pass'", "boxes=1 turns=3 tokens=12000",
            ):
                response = controller.handle(answer)
            runbook = compile_runbook(
                controller.session["plan"]["proposal"], run_id=controller.session["run_id"],
                adapter={"kind": "process", "argv": ["python3", "{workspace}/examples/fake_agent.py", "{packet}", "{result}"], "timeout_seconds": 60},
            )
            runbook_path = self.workspace.parent / "second.runbook.json"
            runbook_path.write_text(json.dumps(runbook), encoding="utf-8")
            imported = controller.handle("/import " + str(runbook_path))
            self.assertIn("IMPORTED PLAN", imported.messages[0])
            controller.handle("/approve yes")
            started = controller.handle("/run")
            self.assertIn("detached", started.messages[-1])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = controller._control("status")
                if status["run"]["status"] in {"completed", "blocked"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["run"]["status"], "completed", status)
            context = controller.handle("/box builder context")
            self.assertIn("context-packet", context.messages[0])
            self.assertIn("second fixture", context.messages[0])
        finally:
            self.stop_fixture(first_state_dir)


    def test_imported_codex_policy_to_real_daemon_build_and_human_acceptance(self):
        from tests.test_codex_adapter import FAKE_CODEX, codex_profile
        from tests.test_gate_runtime import v5_plan
        bin_dir = self.workspace.parent / "fixture-bin"
        bin_dir.mkdir()
        executable = bin_dir / "fake-codex"
        executable.write_text(FAKE_CODEX)
        executable.chmod(0o755)
        runbook = v5_plan("interactive-codex")
        runbook["agents"][0]["adapter"] = {"kind": "codex_cli", "profile": "codex.json",
                                          "profile_snapshot": codex_profile(), "timeout_seconds": 30}
        path = self.workspace.parent / "codex.runbook.json"
        path.write_text(json.dumps(runbook))
        imported = self.controller.handle("/import " + str(path))
        self.assertIn("IMPORTED PLAN", imported.messages[0])
        self.controller.handle("/approve yes")
        policy = self.controller._launch_manifest(self.controller.session["plan"])
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            started = self.controller.handle("/run --accept-launch " + canonical_digest(policy) + " --accept-spend")
            self.assertIn("detached", started.messages[-1])
            deadline = time.monotonic() + 30
            status = None
            while time.monotonic() < deadline:
                status = self.controller._control("status")
                if status["run"]["status"] in {"awaiting_acceptance", "blocked", "completed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["run"]["status"], "awaiting_acceptance", status)
            pending = self.controller._control("acceptance")
            accepted = self.controller.handle("/accept " + pending["acceptance"]["outcome_digest"])
            self.assertIn("Final outcome accepted", accepted.messages[0])
            self.assertEqual(self.controller._control("status")["run"]["status"], "completed")
        self.assertIn("unknown_cost_invocations", self.controller.handle("/usage run").messages[0])


if __name__ == "__main__":
    unittest.main()
