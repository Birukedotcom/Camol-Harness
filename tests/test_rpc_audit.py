import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from camol.rpc_audit import RPCAudit
from camol.ssh_protocol import SSHTransportError
from camol.ssh_transport import SSHControlClient, cancellation_receipt
from tests import test_ssh_transport as fixture


class RPCAuditStoreTests(unittest.TestCase):
    def test_concurrent_clients_preserve_every_record_and_pending_calls_stay_unknown(self):
        with tempfile.TemporaryDirectory(prefix="camol-rpc-audit-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            profile = "sha256:" + "a" * 64
            def write(index):
                audit = RPCAudit(root)
                now = datetime.now(timezone.utc).isoformat()
                record = dict(request_id="ssh-{:032x}".format(index), target_digest=profile,
                              command="status", status="completed", created_at=now, finished_at=now)
                audit.start(record, mutating=False)
                if index:
                    audit.dispatched(record["request_id"])
                    audit.finish(record, elapsed_ms=10, request_bytes=100, stdout_bytes=200, stderr_bytes=0)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(32)))
            report = RPCAudit(root).inspect(profile)
            self.assertEqual(report["calls"], 32)
            self.assertEqual(len({entry["request_id"] for entry in report["entries"]}), 32)
            pending = next(item for item in report["by_command"] if item["status"] == "prepared")
            self.assertEqual(pending["calls"], 1)
            self.assertEqual(pending["elapsed_ms"]["known_calls"], 0)
            pending_entry = next(entry for entry in report["entries"]
                                 if entry["request_id"] == "ssh-" + "0" * 32)
            self.assertIsNone(pending_entry["elapsed_ms"])


class RPCAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.SSHTransportTests()
        await self.fixture.asyncSetUp()
        self.client = self.fixture.client(timeout=3)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_read_and_mutation_measurements_survive_restart_without_body_or_credentials(self):
        response = await self.client.request("status")
        self.assertEqual(self.client.receipts(), [], "read audit must not become a mutation receipt")
        await self.client.request("drain")
        restarted = self.fixture.client(read_only=True)
        report = restarted.usage(limit=1)
        self.assertEqual(report["calls"], 2)
        self.assertTrue(report["more"])
        self.assertIsNone(report["provider_tokens"])
        self.assertIsNone(report["provider_cost"])
        self.assertEqual(report["entries"][0]["request_id"], response["request_id"])
        first = report["entries"][0]
        self.assertEqual(first["request_bytes"], response["transport"]["rpc_usage"]["request_bytes"])
        self.assertGreater(first["request_bytes"], 0)
        self.assertGreater(first["stdout_bytes"], 0)
        self.assertGreater(first["elapsed_ms"], 0)
        self.assertEqual(first["stderr_bytes"], 0)
        next_page = restarted.usage(after=report["next_seq"], limit=1)
        self.assertEqual(next_page["entries"][0]["command"], "drain")
        self.assertFalse(next_page["more"])
        self.assertEqual(len(restarted.receipts()), 1)
        content = self.client.audit.path.read_bytes()
        for secret in (self.fixture.token, "NEVER-READ-THIS-PRIVATE", str(self.fixture.key), "fixture.example", "human-owner"):
            self.assertNotIn(secret.encode(), content)
            self.assertNotIn(secret, json.dumps(report))
        changed = self.fixture.client(read_only=True)
        changed.target = replace(changed.target, name="different-profile")
        self.assertEqual(changed.usage()["calls"], 0)

    async def test_unknown_read_is_visible_but_does_not_block_later_mutation(self):
        self.fixture.mode = "trailing"
        self.fixture.install_fake()
        with self.assertRaises(SSHTransportError):
            await self.client.request("status")
        report = self.client.usage()
        self.assertEqual(report["entries"][0]["status"], "unknown")
        self.assertEqual(report["entries"][0]["mutating"], 0)
        self.assertEqual(self.client.receipts(), [])
        self.fixture.mode = "bridge"
        self.fixture.install_fake()
        self.assertTrue((await self.client.request("drain"))["ok"])
        self.assertEqual(self.client.usage()["calls"], 2)

    async def test_audit_failure_and_retention_ceiling_refuse_before_ssh_launch(self):
        with patch.object(self.client.audit, "start", side_effect=SSHTransportError("AUDIT_UNAVAILABLE", "fixture")), self.assertRaises(SSHTransportError):
            await self.client.request("status")
        self.assertFalse(self.fixture.capture.exists())
        self.assertFalse(self.fixture.requests)
        await self.client.request("status")
        with patch("camol.rpc_audit.MAX_RECORDS", 1), self.assertRaises(SSHTransportError) as caught:
            await self.client.request("drain")
        self.assertEqual(caught.exception.code, "AUDIT_FULL")
        self.assertFalse(self.fixture.supervisor.draining)
        self.assertEqual(self.client.receipts(), [])

    async def test_final_audit_failure_reports_known_outcome_and_keeps_mutation_receipt(self):
        with patch.object(self.client.audit, "finish", side_effect=SSHTransportError("AUDIT_UNAVAILABLE", "fixture")), self.assertRaises(SSHTransportError) as caught:
            await self.client.request("drain")
        self.assertEqual(caught.exception.code, "AUDIT_UNAVAILABLE")
        self.assertEqual(caught.exception.outcome, "completed")
        self.assertTrue(self.fixture.supervisor.draining)
        self.assertEqual(self.client.receipts()[0]["status"], "completed")
        pending = self.client.usage()["entries"][0]
        self.assertEqual(pending["status"], "dispatched")
        self.assertIsNone(pending["elapsed_ms"])

    async def test_cancellation_survives_audit_failure_and_retains_unknown_mutation(self):
        self.fixture.response_delay = 5
        with patch.object(self.client.audit, "finish", side_effect=SSHTransportError("AUDIT_UNAVAILABLE", "fixture")):
            task = asyncio.create_task(self.client.request("drain"))
            for _ in range(150):
                if self.fixture.supervisor.draining:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.fixture.supervisor.draining)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertEqual(cancellation_receipt(caught.exception)["outcome"], "unknown")
        self.assertEqual(cancellation_receipt(caught.exception)["audit_error"], "AUDIT_UNAVAILABLE")
        self.assertEqual(self.client.receipts()[0]["status"], "unknown")

    async def test_readonly_usage_is_noncreating_and_cannot_dispatch(self):
        root = self.fixture.root / "missing-inspector"
        inspector = SSHControlClient(self.fixture.profile, state_dir=root, read_only=True)
        self.assertEqual(inspector.usage()["calls"], 0)
        self.assertFalse(root.exists())
        with self.assertRaises(SSHTransportError):
            await inspector.request("status")
        self.assertFalse(root.exists())
        with self.assertRaises(SSHTransportError):
            inspector.usage(limit=True)

    async def test_linked_special_and_changed_audit_schema_are_rejected_without_touching_target(self):
        await self.client.request("status")
        path = self.client.audit.path
        original = path.with_name("saved-audit.sqlite3")
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(SSHTransportError):
            self.client.usage()
        path.unlink()
        os.link(original, path)
        with self.assertRaises(SSHTransportError):
            self.client.usage()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(SSHTransportError):
            self.client.usage()
        path.unlink()
        original.rename(path)
        with sqlite3.connect(str(path)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(SSHTransportError):
            self.client.usage()
        self.assertFalse(self.fixture.supervisor.draining)

    async def test_readonly_cli_usage_does_not_connect(self):
        from camol.cli import main
        await self.client.request("status")
        output = io.StringIO()
        with patch("camol.cli.load_contract", return_value=self.fixture.profile.to_dict()), patch("sys.stdout", output):
            result = main(["remote", "usage", "--target", "unused", "--state-dir", str(self.fixture.root / "client-state")])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["calls"], 1)
        self.assertEqual(len(self.fixture.requests), 1)
