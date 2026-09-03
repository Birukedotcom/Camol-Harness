import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.sandbox import process_start_fingerprint
from camol.schema import canonical_digest
from camol.supervisor import LeaderLock, Supervisor, SupervisorError, send_control


ROOT = Path(__file__).resolve().parents[1]


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.state = root / "state"
        (self.source / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", self.source / "examples/fake_agent.py")
        subprocess.run(["git", "-C", str(self.source), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-q", "-m", "fixture"], check=True)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _wait_for(self, path, exists=True):
        for _ in range(100):
            if path.exists() is exists:
                return
            await asyncio.sleep(0.02)
        self.fail("timed out waiting for {} existence={}".format(path, exists))

    async def test_authenticated_detachable_control_and_single_leader(self):
        supervisor = Supervisor(
            ROOT / "examples/three-agent-runbook.json", self.source, self.state,
        )
        serving = asyncio.create_task(supervisor.serve())
        await self._wait_for(supervisor.paths.socket)
        self.assertEqual(stat.S_IMODE(supervisor.paths.socket.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(supervisor.paths.token.stat().st_mode), 0o600)
        status = await send_control(self.state, "status")
        self.assertEqual(status["result"]["mode"], "awaiting_approval")
        self.assertFalse(serving.done(), "closing a client must not stop the supervisor")

        reader, writer = await asyncio.open_unix_connection(str(supervisor.paths.socket))
        writer.write((json.dumps({
            "schema": "camol.control_request", "schema_version": 1,
            "token": "wrong-token", "command": "status",
        }) + "\n").encode())
        await writer.drain()
        denied = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "control authentication failed")

        contender = LeaderLock(supervisor.paths.lock)
        with self.assertRaisesRegex(SupervisorError, "another Camol supervisor"):
            contender.acquire()

        await send_control(self.state, "drain")
        await send_control(self.state, "approve", requested_by="human-owner")
        for _ in range(100):
            current = await send_control(self.state, "status")
            if current["result"]["mode"] == "drained":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(current["result"]["run"]["approved_by"], "human-owner")
        self.assertEqual(current["result"]["mode"], "drained")
        await send_control(self.state, "stop", requested_by="human-owner")
        await asyncio.wait_for(serving, timeout=5)
        self.assertFalse(supervisor.paths.socket.exists())
        self.assertFalse(supervisor.paths.pid.exists())

    async def test_control_client_never_creates_credentials_when_daemon_is_absent(self):
        with self.assertRaisesRegex(SupervisorError, "control token"):
            await send_control(self.state, "status")
        self.assertFalse((self.state / "control/control.token").exists())

    async def test_database_and_control_symlink_must_stay_inside_state(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.state.mkdir()
        (self.state / "control").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SupervisorError, "must not be a symlink"):
            Supervisor(ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        (self.state / "control").unlink()
        with self.assertRaisesRegex(SupervisorError, "database must stay"):
            Supervisor(
                ROOT / "examples/three-agent-runbook.json", self.source, self.state,
                database=outside / "db.sqlite3",
            )

    async def test_live_orphan_blocks_resume_until_explicit_force_stop(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        record = self.state / "packets/run/task/turn-001.invocation.json"
        record.parent.mkdir(parents=True)
        payload = {
            "schema": "camol.process_invocation",
            "schema_version": 1,
            "state": "active",
            "owner_pid": 999999,
            "pid": process.pid,
            "pgid": process.pid,
            "process_started": process_start_fingerprint(process.pid),
            "cwd": str(self.source),
            "argv_digest": canonical_digest([sys.executable, "-c", "sleep"]),
            "policy_digest": canonical_digest({"fixture": "policy"}),
            "started_at": "2026-09-03T00:00:00+00:00",
            "finished_at": None,
            "exit_code": None,
        }
        record.write_text(json.dumps(payload), encoding="utf-8")
        supervisor = Supervisor(ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        serving = asyncio.create_task(supervisor.serve())
        try:
            await self._wait_for(supervisor.paths.socket)
            for _ in range(100):
                status = await send_control(self.state, "status")
                if status["result"]["mode"] == "orphaned":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(status["result"]["mode"], "orphaned")
            self.assertEqual(status["result"]["orphan_invocations"][0]["pid"], process.pid)
            with self.assertRaisesRegex(SupervisorError, "explicit force-stop"):
                await send_control(self.state, "resume")
            reaped = asyncio.create_task(asyncio.to_thread(process.wait))
            await send_control(self.state, "force-stop", requested_by="human-owner")
            await asyncio.wait_for(reaped, timeout=5)
            await asyncio.wait_for(serving, timeout=5)
            self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["state"], "terminated_by_supervisor")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()
            if not serving.done():
                serving.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await serving

    async def test_cli_start_really_detaches_after_source_checkout_changes_cwd(self):
        environment = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in os.environ
        }
        command = [
            sys.executable, "-m", "camol", "start", str(ROOT / "examples/three-agent-runbook.json"),
            "--workspace", str(self.source), "--state-dir", str(self.state),
        ]
        started = subprocess.run(
            command, cwd=str(ROOT), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        response = json.loads(started.stdout)
        self.assertGreater(response["pid"], 1)
        status = subprocess.run(
            [sys.executable, "-m", "camol", "ctl", "status", "--state-dir", str(self.state)],
            cwd=str(ROOT), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["mode"], "awaiting_approval")
        stopped = subprocess.run(
            [sys.executable, "-m", "camol", "ctl", "stop", "--state-dir", str(self.state)],
            cwd=str(ROOT), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        await self._wait_for(self.state / "control/camol.sock", exists=False)


if __name__ == "__main__":
    unittest.main()
