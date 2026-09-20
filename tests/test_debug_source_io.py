"""Source inventory must reject special files without waiting for a writer."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import camol


class SourceInventoryIOTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_tracked_fifo_is_rejected_without_blocking_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tracked.txt"
            path.write_text("ordinary source\n")
            for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                         ("config", "user.email", "fixture@example.invalid"),
                         ("add", "."), ("commit", "-qm", "fixture")):
                subprocess.run(["git", "-C", str(root), *args], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            path.unlink()
            os.mkfifo(path)
            code = "from camol.debug_execution import source_identity, DebugExecutionError\nimport sys\ntry:\n source_identity(sys.argv[1])\nexcept DebugExecutionError:\n print('special-file-denied')\nelse:\n raise SystemExit('unsafe source was accepted')\n"
            result = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root,
                env=dict(os.environ, PYTHONPATH=str(Path(camol.__file__).resolve().parents[1])),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"special-file-denied\n")
