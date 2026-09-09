import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from camol.repository_graph import GraphError, crawl_repository
from camol.archive_io import ArchiveRoot
from camol.preflight_process import bounded_preflight_run


class GraphBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(self.repo), *args],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_repository_path_git_trap_never_executes(self):
        sentinel = self.root / "executed"
        trap = self.repo / "git"
        trap.write_text("#!/bin/sh\nprintf bad > " + shlex.quote(str(sentinel)) + "\nexit 9\n")
        trap.chmod(0o700)
        with patch.dict(os.environ, {"PATH": str(self.repo) + os.pathsep + os.environ.get("PATH", "")}):
            try:
                crawl_repository(self.repo)
            except GraphError:
                pass
        self.assertFalse(sentinel.exists(), "a read-only graph crawl executed repository code")

    def test_hardlinked_outside_content_is_not_parsed(self):
        outside = self.root / "outside.py"
        outside.write_text("import private_graph_marker\n")
        os.link(outside, self.repo / "inside.py")
        graph = crawl_repository(self.repo).to_dict()
        self.assertNotIn("private_graph_marker", json.dumps(graph))
        self.assertEqual(graph["status"], "OBSERVATION_INCOMPLETE")
        self.assertEqual(outside.read_text(), "import private_graph_marker\n")

    def test_parent_swap_to_external_symlink_never_reads_external_content(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "module.py").write_text("import ordinary_module\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "module.py").write_text("import private_graph_marker\n")
        original = ArchiveRoot.read
        def swap(reader, relative, maximum):
            if relative == "nested/module.py":
                nested.rename(self.repo / "retained-nested")
                nested.symlink_to(outside, target_is_directory=True)
            return original(reader, relative, maximum)
        with patch.object(ArchiveRoot, "read", swap):
            result = crawl_repository(self.repo).to_dict()
        self.assertEqual(result["status"], "OBSERVATION_INCOMPLETE")
        self.assertNotIn("private_graph_marker", json.dumps(result))

    def test_change_after_read_refuses_snapshot_instead_of_parsing_a_stale_cut(self):
        path = self.repo / "module.py"
        path.write_text("import ordinary_module\n")
        original = ArchiveRoot.read
        def mutate(reader, relative, maximum):
            result = original(reader, relative, maximum)
            if relative == "module.py":
                path.write_text("import replacement_module\n")
            return result
        with patch.object(ArchiveRoot, "read", mutate), self.assertRaises(GraphError):
            crawl_repository(self.repo)

    def test_file_swapped_for_fifo_is_not_opened_as_a_blocking_stream(self):
        path = self.repo / "module.py"
        path.write_text("import ordinary_module\n")
        original = ArchiveRoot.read
        def replace(reader, relative, maximum):
            if relative == "module.py":
                path.unlink()
                os.mkfifo(path)
            return original(reader, relative, maximum)
        with patch.object(ArchiveRoot, "read", replace):
            result = crawl_repository(self.repo).to_dict()
        self.assertEqual(result["status"], "OBSERVATION_INCOMPLETE")
        self.assertNotIn("ordinary_module", json.dumps(result))

    def test_repo_callback_and_inherited_git_environment_do_not_execute(self):
        sentinel = self.root / "callback-executed"
        trap = self.root / "callback"
        trap.write_text("#!/bin/sh\nprintf bad > " + shlex.quote(str(sentinel)) + "\n")
        trap.chmod(0o700)
        self.git("config", "core.fsmonitor", str(trap))
        (self.repo / "module.py").write_text("import ordinary_module\n")
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "wrong-git"),
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                     "GIT_CONFIG_VALUE_0": str(trap)}):
            result = crawl_repository(self.repo)
        self.assertFalse(sentinel.exists())
        self.assertIn("ordinary_module", json.dumps(result.to_dict()))

    def test_missing_trusted_git_is_explicit_and_does_not_try_path_fallback(self):
        with patch("camol.repository_graph.shutil.which", return_value=None), \
                patch("camol.repository_graph.bounded_preflight_run", side_effect=AssertionError("no launch")), \
                self.assertRaisesRegex(GraphError, "trusted system Git"):
            crawl_repository(self.repo)

    def test_inventory_clears_even_explicit_inherited_protocol_permission(self):
        observed = []
        def inspect(argv, **kwargs):
            observed.append(kwargs["env"].copy())
            return bounded_preflight_run(argv, **kwargs)
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file:ext", "GIT_NO_LAZY_FETCH": "0"}), \
                patch("camol.repository_graph.bounded_preflight_run", inspect):
            crawl_repository(self.repo)
        self.assertTrue(observed)
        for environment in observed:
            self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(environment["PATH"], "/usr/bin:/bin")


class SharedBoundedCaptureTests(unittest.TestCase):
    def test_custom_output_limit_is_enforced_while_child_is_running(self):
        with self.assertRaisesRegex(OSError, "output ceiling"):
            bounded_preflight_run([sys.executable, "-c", "import os,time; os.write(1,b'x'*8192); time.sleep(20)"],
                                  cwd=None, env=os.environ.copy(), timeout=5, max_output_bytes=1024)

    def test_invalid_output_limit_refuses_before_any_subprocess(self):
        for maximum in (False, 0, -1, 1.5, (32 << 20) + 1):
            with self.subTest(maximum=maximum), \
                    patch("camol.preflight_process.subprocess.Popen", side_effect=AssertionError("no launch")), \
                    self.assertRaises(ValueError):
                bounded_preflight_run(["unused"], cwd=None, env={}, max_output_bytes=maximum)


if __name__ == "__main__":
    unittest.main()
