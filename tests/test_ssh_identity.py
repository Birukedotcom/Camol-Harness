"""Public runtime measurement is distinct from private control-file trust."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.ssh_bridge import bridge_identity, _measure_public_file
from camol.ssh_protocol import SSHTransportError, read_regular, sha256


class SSHIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="camol-public-identity-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.executable = self.root / "python-fixture"
        self.executable.write_bytes(b"public interpreter bytes")

    def test_writable_managed_runtime_can_be_measured_without_weakening_private_readers(self):
        # GitHub's Linux image chmods the preinstalled hostedtoolcache to 0777.
        # Reproduce that file property without changing the real interpreter.
        self.executable.chmod(0o777)
        with self.assertRaises(SSHTransportError):
            read_regular(self.executable, maximum=1024, owner=False)
        with patch("camol.ssh_bridge.sys.executable", str(self.executable)):
            identity = bridge_identity()
        self.assertEqual(identity["python_sha256"], sha256(self.executable.read_bytes()))
        self.assertEqual(identity["python_executable"], str(self.executable))
        for private in (False, True):
            with self.subTest(private=private), self.assertRaises(SSHTransportError):
                read_regular(self.executable, maximum=1024, private=private)
        self.assertEqual(self.executable.stat().st_mode & 0o777, 0o777)

    def test_public_measurement_refuses_symlink_fifo_and_oversize_inputs(self):
        link = self.root / "link"
        link.symlink_to(self.executable)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path, maximum in ((link, 1024), (fifo, 1024), (self.executable, 2)):
            with self.subTest(path=path.name), self.assertRaises(SSHTransportError):
                _measure_public_file(path, maximum=maximum)

    def test_public_measurement_refuses_bytes_changed_during_read(self):
        real_fstat = os.fstat
        calls = []
        def changing_stat(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                self.executable.write_bytes(b"different runtime bytes")
                os.utime(self.executable, ns=(1_000_000_000, 1_000_000_000))
            return real_fstat(descriptor)
        with patch("camol.ssh_bridge.os.fstat", side_effect=changing_stat):
            with self.assertRaisesRegex(SSHTransportError, "changed"):
                _measure_public_file(self.executable, maximum=1024)

    def test_public_measurement_is_identical_for_readonly_and_writable_bytes(self):
        self.executable.chmod(0o444)
        baseline = _measure_public_file(self.executable, maximum=1024)
        self.executable.chmod(0o777)
        self.assertEqual(_measure_public_file(self.executable, maximum=1024), baseline)
