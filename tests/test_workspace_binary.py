import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from camol.workspace import WorkspaceManager, WorkspaceError
from camol.probes import ProbeExecutionError
from tests import test_probes as fixture


class WorkspaceBinaryTests(unittest.TestCase):
    def test_probe_guard_protects_source_separately_from_observed_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            source.mkdir()
            script = source / "git"
            script.write_text("not executed")
            with patch("camol.probes.run_command", side_effect=AssertionError("no subprocess")):
                context = fixture.make_context(root / "worktree", root / "state", source_workspace=source,
                    which=lambda _: str(script), runner=lambda *args: self.fail("source executable reached runner"))
                self.assertIsNone(context.path_binary("git"))
                with self.assertRaises(ProbeExecutionError):
                    context.runner([str(script), "--version"], None, 1)
                alias = root / "git-alias"
                alias.symlink_to(script)
                with self.assertRaises(ProbeExecutionError):
                    context.runner([str(alias), "--version"], None, 1)

    def test_workspace_git_trap_on_path_never_executes_during_preparation(self):
        binary = shutil.which("git", path=os.defpath)
        if not binary:
            self.skipTest("requires system Git")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run([binary, "init", "-q", str(source)], check=True)
            trap = source / "bin"
            trap.mkdir()
            sentinel = root / "git-trap-ran"
            script = trap / "git"
            script.write_text("#!/bin/sh\ntouch '" + str(sentinel) + "'\nexit 1\n")
            script.chmod(0o700)
            with patch.dict(os.environ, {"PATH": str(trap) + os.pathsep + os.defpath}):
                try:
                    manager = WorkspaceManager(source, root / "state")
                except WorkspaceError:
                    manager = None
            self.assertFalse(sentinel.exists(), "untrusted source binary executed before any worker lease")
            self.assertIsNotNone(manager, "trusted system Git should still inspect the repository")
