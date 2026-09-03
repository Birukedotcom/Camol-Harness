import os
import fcntl
import pty
import select
import struct
import termios
import time
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

    def test_bare_command_renders_as_a_real_full_screen_pty(self):
        with tempfile.TemporaryDirectory() as temporary:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            environment = dict(
                os.environ,
                CAMOL_STATE_HOME=str(Path(temporary) / "state"),
                TERM="xterm-256color",
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "camol", "--no-boot", "--workspace", temporary],
                cwd=str(ROOT), env=environment, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True,
                preexec_fn=lambda: (os.setsid(), fcntl.ioctl(slave, termios.TIOCSCTTY, 0)),
            )
            os.close(slave)
            captured = bytearray()
            try:
                deadline = time.time() + 8
                while time.time() < deadline and b"ORCH" not in captured:
                    readable, _, _ = select.select([master], [], [], 0.2)
                    if readable:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        captured.extend(chunk)
                # Keep draining redraws while the asynchronous connection inventory
                # updates the top rail; a real terminal always consumes this output.
                redraw_deadline = time.time() + 1.0
                while time.time() < redraw_deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        captured.extend(os.read(master, 65536))
                # Exercise Camol's disposable-client contract instead of relying
                # on Textual's platform signal handling.
                os.write(master, b"\x11")
                exit_deadline = time.time() + 8
                while process.poll() is None and time.time() < exit_deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            captured.extend(os.read(master, 65536))
                        except OSError:
                            break
                process.wait(timeout=1)
                while True:
                    readable, _, _ = select.select([master], [], [], 0.05)
                    if not readable:
                        break
                    try:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        captured.extend(chunk)
                    except OSError:
                        break
            finally:
                os.close(master)
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        self.assertIn(b"ORCH", captured)
        self.assertIn(b"CAMOL PRODUCT V0", captured)


if __name__ == "__main__":
    unittest.main()
