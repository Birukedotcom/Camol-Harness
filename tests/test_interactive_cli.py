import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InteractiveCliTests(unittest.TestCase):
    def test_bare_command_enters_client_instead_of_argparse_help(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ, CAMOL_STATE_HOME=str(Path(temporary) / "state"))
            completed = subprocess.run(
                [sys.executable, "-m", "camol", "--no-tui", "--no-boot", "--workspace", temporary],
                input="/quit\n",
                text=True,
                cwd=str(ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Camol Product V0 line mode", completed.stdout)
        self.assertIn("Client detached", completed.stdout)
        self.assertNotIn("usage: camol", completed.stdout)


if __name__ == "__main__":
    unittest.main()
