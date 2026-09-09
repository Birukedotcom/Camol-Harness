"""Control-plane progress must not depend on a synchronous box readiness probe."""

import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from camol import Harness
from camol.supervisor import Supervisor, send_control
from camol.admission import AdmissionError
from camol.admission_process import prepare
from camol.runbook import load_runbook, runbook_digest
from tests import test_api as fixture


class PreparationContractTests(unittest.TestCase):
    def test_changed_plan_or_subject_is_rejected_before_filesystem_work(self):
        runbook = load_runbook(fixture.ROOT / "examples/local-n-box-runbook.json")
        request = dict(schema="camol.admission_preparation", schema_version=1, source="/unused-source",
            state_dir="/unused-state", runbook=runbook, target_id="local", observed_at="2026-09-08T00:00:00Z",
            arguments=dict(plan_digest=runbook_digest(runbook), task=copy.deepcopy(runbook["tasks"][0]), agent=copy.deepcopy(runbook["agents"][0]),
                           granted_by="owner", base_revision=None, expected_evaluator_digest=None))
        changes = []
        for field, key, value in (("task", "capabilities", ["broader-authority"]),
                                  ("agent", "adapter", {"kind": "process", "argv": ["foreign-command"]}),
                                  ("task", "id", "unknown-task")):
            changed = copy.deepcopy(request)
            changed["arguments"][field][key] = value
            changes.append(changed)
        changed = copy.deepcopy(request)
        changed["arguments"]["plan_digest"] = "sha256:" + "0" * 64
        changes.append(changed)
        changed = copy.deepcopy(request)
        changed["schema_version"] = True
        changes.append(changed)
        with patch("camol.admission_process.WorkspaceManager", side_effect=AssertionError("preparation touched filesystem")):
            for changed in changes:
                with self.subTest(changed=changed["arguments"]["plan_digest"]), self.assertRaises(AdmissionError):
                    prepare(changed)


class AdmissionLivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.HarnessApiTests.setUp(self)

    def tearDown(self):
        fixture.HarnessApiTests.tearDown(self)

    def _gated_interpreter(self):
        root = Path(self.temp.name)
        entered, release, timed_out = (root / name for name in ("entered", "release", "timed-out"))
        binary_dir = root / "runtime"
        binary_dir.mkdir()
        shim = binary_dir / "python3"
        # An owner-installed interpreter wrapper outside workspace/control state.
        # The normal guarded version probe may execute it, never project scripts.
        shim.write_text("#!" + sys.executable + "\n" +
            "import os,sys,time\nfrom pathlib import Path\n" +
            "entered=Path(" + repr(str(entered)) + ")\n" +
            "release=Path(" + repr(str(release)) + ")\n" +
            "timed_out=Path(" + repr(str(timed_out)) + ")\n" +
            "if sys.argv[1:] == ['--version'] and not entered.exists():\n" +
            " entered.write_text('probe started')\n deadline=time.monotonic()+5\n" +
            " while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n" +
            " if not release.exists(): timed_out.write_text('control could not run')\n" +
            "os.execv(" + repr(sys.executable) + ",[" + repr(sys.executable) + "]+sys.argv[1:])\n")
        shim.chmod(0o700)
        return binary_dir, entered, release, timed_out

    async def test_pause_is_serviced_during_real_probe_and_prevents_new_lease(self):
        binary_dir, entered, release, timed_out = self._gated_interpreter()
        paused = False
        observations = []
        with patch.dict(os.environ, {"PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", "")}):
            with Harness(self.workspace, self.state_dir) as harness:
                initial = harness.prepare(fixture.ROOT / "examples/local-n-box-runbook.json")
                harness.approve(by="owner", digest=initial["plan_digest"])

                async def control_request():
                    nonlocal paused
                    for _ in range(2000):
                        if entered.exists():
                            observations.append(harness.state()["last_seq"])
                            paused = True
                            release.write_text("owner requested pause")
                            return
                        await asyncio.sleep(.01)
                    self.fail("actual version probe never began")

                controller = asyncio.create_task(control_request())
                try:
                    await asyncio.wait_for(harness.run_async(should_pause=lambda: paused), timeout=40)
                    await controller
                    events = harness.events()
                finally:
                    release.write_text("test cleanup")
                    controller.cancel()
                    await asyncio.gather(controller, return_exceptions=True)
                self.assertTrue(entered.is_file(), "real guarded probe was not exercised")
                self.assertTrue(observations, "owner control callback was not exercised")
                self.assertFalse(timed_out.exists(), "box preparation blocked the control event loop")
                self.assertFalse(any(event["type"] in {"TASK_LEASED", "TASK_STARTED"} for event in events),
                                 "pause during preparation must be rechecked before admission/lease")

    async def test_cancel_during_probe_settles_tracked_child_without_admission(self):
        binary_dir, entered, release, timed_out = self._gated_interpreter()
        with patch.dict(os.environ, {"PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", "")}):
            with Harness(self.workspace, self.state_dir) as harness:
                initial = harness.prepare(fixture.ROOT / "examples/local-n-box-runbook.json")
                harness.approve(by="owner", digest=initial["plan_digest"])
                driver = asyncio.create_task(harness.run_async())
                try:
                    for _ in range(2000):
                        if entered.exists():
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.exists())
                    driver.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(driver, timeout=10)
                    self.assertFalse(timed_out.exists())
                    records = [json.loads(path.read_text()) for path in
                        (self.state_dir / "packets").rglob("*.invocation.json")]
                    self.assertTrue(records)
                    self.assertTrue(all(record["state"] == "terminated" for record in records))
                    self.assertFalse(harness.state()["admissions"])
                    cursor = harness.state()["last_seq"]
                    release.write_text("release after settled cancellation")
                    await asyncio.sleep(.05)
                    self.assertEqual(harness.state()["last_seq"], cursor)
                finally:
                    release.write_text("test cleanup")
                    driver.cancel()
                    await asyncio.gather(driver, return_exceptions=True)

    async def test_terminal_change_during_prepare_cannot_publish_late_result(self):
        binary_dir, entered, release, timed_out = self._gated_interpreter()
        with patch.dict(os.environ, {"PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", "")}):
            with Harness(self.workspace, self.state_dir) as harness:
                initial = harness.prepare(fixture.ROOT / "examples/local-n-box-runbook.json")
                harness.approve(by="owner", digest=initial["plan_digest"])
                driver = asyncio.create_task(harness.run_async())
                try:
                    for _ in range(2000):
                        if entered.exists():
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.exists())
                    harness.orchestrator.block_run(initial["run_id"], "owner_fixture_stop", {})
                    cursor = harness.state()["last_seq"]
                    release.write_text("owner stopped run")
                    await asyncio.wait_for(driver, timeout=20)
                    self.assertFalse(timed_out.exists())
                    self.assertEqual(harness.state()["last_seq"], cursor)
                    self.assertFalse(harness.state()["admissions"])
                finally:
                    release.write_text("test cleanup")
                    driver.cancel()
                    await asyncio.gather(driver, return_exceptions=True)

    async def test_authenticated_supervisor_status_and_drain_during_probe(self):
        binary_dir, entered, release, timed_out = self._gated_interpreter()
        with patch.dict(os.environ, {"PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", "")}):
            supervisor = Supervisor(fixture.ROOT / "examples/local-n-box-runbook.json", self.workspace, self.state_dir)
            serving = asyncio.create_task(supervisor.serve())
            try:
                for _ in range(500):
                    if supervisor.paths.socket.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(supervisor.paths.socket.exists())
                approved = await send_control(self.state_dir, "approve", requested_by="owner")
                self.assertTrue(approved["ok"])
                for _ in range(2000):
                    if entered.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.exists())
                status = await asyncio.wait_for(send_control(self.state_dir, "status"), timeout=2)
                self.assertTrue(status["ok"])
                drained = await asyncio.wait_for(send_control(self.state_dir, "drain", requested_by="owner"), timeout=2)
                self.assertTrue(drained["ok"])
                release.write_text("authenticated drain delivered")
                for _ in range(500):
                    status = await send_control(self.state_dir, "status")
                    if status["result"]["mode"] == "drained":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(status["result"]["mode"], "drained")
                self.assertFalse(timed_out.exists())
                self.assertFalse(supervisor.orchestrator.state(supervisor.run_id)["admissions"])
                stopped = await send_control(self.state_dir, "stop", requested_by="owner")
                self.assertTrue(stopped["ok"])
                await asyncio.wait_for(serving, timeout=5)
            finally:
                release.write_text("test cleanup")
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
