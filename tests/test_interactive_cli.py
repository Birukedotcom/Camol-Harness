import os
import json
import fcntl
import pty
import re
import select
import signal
import struct
import termios
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]

# A test-only stack-dump hook, not a second CLI or a production signal handler.
# run_module executes the same real package entry point and command arguments.
PTY_ENTRY = "import faulthandler,runpy,signal; faulthandler.register(signal.SIGUSR1, all_threads=True); runpy.run_module('camol',run_name='__main__')"


def _terminal_state(master):
    try:
        settings = termios.tcgetattr(master)
        return {"canonical": bool(settings[3] & termios.ICANON), "echo": bool(settings[3] & termios.ECHO),
                "signals": bool(settings[3] & termios.ISIG), "foreground_pgid": os.tcgetpgrp(master)}
    except OSError as error:
        return {"terminal_errno": error.errno}


def _pty_diagnostic(captured, state):
    from camol.probes import Redactor
    text = captured[-32768:].decode("utf-8", errors="replace")
    # Strip terminal escape/control bytes before publishing the fixture tail.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    return "PTY lifecycle: " + json.dumps(state, sort_keys=True) + "\nSanitized terminal tail (bounded):\n" + Redactor().text(text)


def _reap_owned_client(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # This exact Popen is the disposable child we created, not a saved PID.
        # Escalation ensures a noncooperative client cannot replace the original
        # test failure with a graceful-shutdown timeout or leak into later tests.
        process.kill()
        process.wait(timeout=5)


class InteractiveCliTests(unittest.TestCase):
    def test_noncooperative_owned_client_cleanup_escalates_and_reaps(self):
        process = Mock(poll=Mock(return_value=None), wait=Mock(side_effect=[subprocess.TimeoutExpired("fixture", 5), -9]))
        _reap_owned_client(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_pty_failure_diagnostics_are_bounded_and_redacted(self):
        secret = "sk-live-abcdefghijklmnopqrstuvwxyz123456"
        rendered = _pty_diagnostic(("old " * 10000 + "\x1b[31m" + secret).encode(), {"canonical": False})
        self.assertNotIn(secret, rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertLess(len(rendered), 34000)

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
                [sys.executable, "-c", PTY_ENTRY, "--no-boot", "--workspace", temporary],
                cwd=str(ROOT), env=environment, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True,
                preexec_fn=lambda: (os.setsid(), fcntl.ioctl(slave, termios.TIOCSCTTY, 0)),
            )
            os.close(slave)
            captured = bytearray()
            started = time.monotonic()
            diagnostic = {}
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and (b"ORCH" not in captured or b"CAMOL PRODUCT V0" not in captured):
                    readable, _, _ = select.select([master], [], [], 0.2)
                    if readable:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            diagnostic["readiness_read_errno"] = error.errno
                            break
                        if not chunk:
                            break
                        captured.extend(chunk)
                        del captured[:-1048576]
                diagnostic["readiness_seconds"] = round(time.monotonic() - started, 3)
                diagnostic["ready_terminal"] = _terminal_state(master)
                self.assertIn(b"ORCH", captured, _pty_diagnostic(captured, diagnostic))
                self.assertIn(b"CAMOL PRODUCT V0", captured, _pty_diagnostic(captured, diagnostic))
                # Keep draining redraws while the asynchronous connection inventory
                # updates the top rail; a real terminal always consumes this output.
                redraw_deadline = time.monotonic() + 1.0
                while time.monotonic() < redraw_deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            captured.extend(os.read(master, 65536))
                        except OSError as error:
                            diagnostic["redraw_read_errno"] = error.errno
                            break
                        del captured[:-1048576]
                # Ctrl+C must safely detach the disposable client rather than
                # leave the full-screen terminal trapped.
                diagnostic["before_ctrl_c"] = _terminal_state(master)
                diagnostic["ctrl_c_seconds"] = round(time.monotonic() - started, 3)
                os.write(master, b"\x03")
                exit_deadline = time.monotonic() + 8
                while process.poll() is None and time.monotonic() < exit_deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            captured.extend(os.read(master, 65536))
                        except OSError:
                            break
                        del captured[:-1048576]
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    diagnostic["shutdown_seconds"] = round(time.monotonic() - started, 3)
                    diagnostic["after_ctrl_c"] = _terminal_state(master)
                    diagnostic["pid"] = process.pid
                    # The test has already failed its original deadline. One
                    # bounded stack collection supplies evidence, never a retry
                    # or an extension of the successful-exit acceptance window.
                    try:
                        process.send_signal(signal.SIGUSR1)
                    except ProcessLookupError:
                        pass
                    dump_deadline = time.monotonic() + .5
                    while time.monotonic() < dump_deadline:
                        readable, _, _ = select.select([master], [], [], .05)
                        if readable:
                            try:
                                captured.extend(os.read(master, 65536))
                            except OSError:
                                break
                        del captured[:-1048576]
                    self.fail(_pty_diagnostic(captured, diagnostic))
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
                _reap_owned_client(process)
        self.assertIn(b"ORCH", captured)
        self.assertIn(b"CAMOL PRODUCT V0", captured)
        self.assertEqual(process.returncode, 0)

    def test_native_login_url_is_visible_while_full_screen_client_is_suspended(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "repo"
            workspace.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            claude = fake_bin / "claude"
            claude.write_text(
                "#!{}\n".format(sys.executable)
                + "import json, sys\n"
                + "args = sys.argv[1:]\n"
                + "if args == ['--version']:\n"
                + "    print('claude-test 1.0')\n"
                + "elif args == ['auth', 'status']:\n"
                + "    print(json.dumps({'loggedIn': True, 'orgId': 'test', 'email': 'user@example.test', 'authMethod': 'claudeai'}))\n"
                + "elif args == ['auth', 'login']:\n"
                + "    print('LOGIN_URL=https://claude.ai/login/camol-test', flush=True)\n"
                + "else:\n"
                + "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            environment = dict(
                os.environ,
                CAMOL_STATE_HOME=str(root / "state"),
                PATH=str(fake_bin) + os.pathsep + "/usr/bin:/bin",
                TERM="xterm-256color",
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "camol", "--no-boot", "--workspace", str(workspace)],
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
                        captured.extend(os.read(master, 65536))
                ready_deadline = time.time() + 1.0
                while time.time() < ready_deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        captured.extend(os.read(master, 65536))
                os.write(master, b"/login claude")
                typed_deadline = time.time() + 2
                while time.time() < typed_deadline and b"/login claude" not in captured:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        captured.extend(os.read(master, 65536))
                # Keep this PTY assertion focused on the native-login handoff.
                # Textual's pilot tests independently cover plain Enter; the
                # hidden Ctrl+Enter compatibility binding is unambiguous here.
                os.write(master, b"\x1b[13;5u")
                deadline = time.time() + 8
                while time.time() < deadline and b"LOGIN_URL=" not in captured:
                    readable, _, _ = select.select([master], [], [], 0.2)
                    if readable:
                        captured.extend(os.read(master, 65536))
                # Let Camol return from native login and paint the verified model.
                deadline = time.time() + 5
                while time.time() < deadline and b"claude:fable" not in captured:
                    readable, _, _ = select.select([master], [], [], 0.2)
                    if readable:
                        captured.extend(os.read(master, 65536))
            finally:
                os.close(master)
                _reap_owned_client(process)
        self.assertIn(b"LOGIN_URL=https://claude.ai/login/camol-test", captured)
        self.assertIn(b"claude:fable", captured)


if __name__ == "__main__":
    unittest.main()
