import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.connections import ConnectionError, ConnectionRegistry, _record


class ConnectionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = ConnectionRegistry(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def result(stdout=b"", stderr=b"", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def test_claude_probe_persists_only_a_non_reversible_fingerprint(self):
        identity = {"loggedIn": True, "authMethod": "claude.ai", "orgId": "secret-org", "email": "person@example.com"}
        with patch("camol.connections.shutil.which", return_value="/usr/bin/claude"), patch.object(
            self.registry,
            "_run",
            side_effect=[self.result(b"2.0\n"), self.result(json.dumps(identity).encode())],
        ):
            record = self.registry.probe_claude()
            self.registry.save([record])

        persisted = self.registry.path.read_text()
        self.assertEqual(record["status"], "ready")
        self.assertTrue(record["account_fingerprint"].startswith("hmac-sha256:"))
        self.assertNotIn("secret-org", persisted)
        self.assertNotIn("person@example.com", persisted)
        self.assertEqual(stat.S_IMODE(self.registry.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry.key_path.stat().st_mode), 0o600)

    def test_openai_environment_records_presence_not_value(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live-super-secret-value"}):
            record = self.registry.probe_openai_environment()
            self.registry.save([record])
        self.assertEqual(record["credential_ref"], "env:OPENAI_API_KEY")
        self.assertNotIn("super-secret-value", self.registry.path.read_text())

    def test_non_loopback_local_endpoint_is_denied_without_network_access(self):
        with self.assertRaisesRegex(ConnectionError, "loopback"):
            self.registry.probe_local("https://example.com/v1")

    def test_cli_probe_failures_are_typed_not_credentials(self):
        with patch("camol.connections.shutil.which", return_value="/usr/bin/codex"), patch.object(
            self.registry,
            "_run",
            side_effect=[self.result(b"codex 1"), self.result(stderr=b"not logged in", returncode=1)],
        ):
            record = self.registry.probe_codex()
        self.assertEqual(record["status"], "auth_required")

    def test_one_broken_runtime_does_not_hide_other_connections(self):
        with patch.object(self.registry, "probe_claude", side_effect=ConnectionError("broken")), patch.object(
            self.registry, "probe_codex", return_value=_record(
                "codex-cli", "openai", "cli", status="ready", runtime="codex"
            )
        ), patch.object(
            self.registry, "probe_openai_environment", return_value=_record(
                "openai-api-env", "openai", "api", status="auth_required", runtime="https"
            )
        ), patch.object(
            self.registry, "probe_local", return_value=_record(
                "local-openai", "local", "openai_compatible", status="unavailable", runtime="loopback"
            )
        ):
            records = self.registry.probe_all()
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0]["status"], "error")
        self.assertEqual(records[1]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
