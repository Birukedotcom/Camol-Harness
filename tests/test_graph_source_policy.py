import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from camol.app import InteractiveController
from camol.archive_io import ArchiveRoot, ArchiveIOError
from camol.cli import main
from camol.repository_graph import CrawlPolicy, GraphError, crawl_repository


class GraphSourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
        self.alias = self.root / "shared-source.py"
        self.alias.write_text("import chosen_shared_module\n")
        os.link(self.alias, self.repo / "module.py")
        self.policy = CrawlPolicy(allow_hardlinked_source=True)

    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(args)
        return code, output.getvalue(), errors.getvalue()

    def test_explicit_policy_reads_shared_source_binds_policy_and_does_not_modify_it(self):
        original = self.alias.stat()
        strict = crawl_repository(self.repo).to_dict()
        allowed = crawl_repository(self.repo, policy=self.policy).to_dict()
        again = crawl_repository(self.repo).to_dict()
        self.assertNotIn("chosen_shared_module", json.dumps(strict))
        self.assertIn("1 hard-linked source files skipped", "\n".join(strict["warnings"]))
        self.assertIn("--allow-hardlinked-source", "\n".join(strict["warnings"]))
        self.assertIn("chosen_shared_module", json.dumps(allowed))
        self.assertIn("aliases may exist outside", "\n".join(allowed["warnings"]))
        self.assertEqual(allowed["status"], "OBSERVATION_INCOMPLETE")
        self.assertNotEqual(strict["configuration_digest"], allowed["configuration_digest"])
        self.assertEqual(strict["snapshot_id"], again["snapshot_id"])
        current = self.alias.stat()
        self.assertEqual((original.st_ino, original.st_nlink, original.st_mode, original.st_mtime_ns),
                         (current.st_ino, current.st_nlink, current.st_mode, current.st_mtime_ns))
        self.assertEqual(self.alias.read_text(), "import chosen_shared_module\n")

    def test_archive_reader_remains_single_link_after_source_opt_in(self):
        crawl_repository(self.repo, policy=self.policy)
        with ArchiveRoot(self.repo) as reader, self.assertRaises(ArchiveIOError):
            reader.read("module.py", 1024)

    def test_opt_in_does_not_allow_symlinks_special_files_or_excluded_names(self):
        (self.repo / "escape.py").symlink_to(self.alias)
        os.mkfifo(self.repo / "pipe.py")
        os.link(self.alias, self.repo / ".env")
        snapshot = crawl_repository(self.repo, policy=self.policy).to_dict()
        paths = {item["path"] for item in snapshot["evidence"]}
        self.assertIn("module.py", paths)
        self.assertTrue(paths.isdisjoint({"escape.py", "pipe.py", ".env"}))

    def test_mutation_through_other_alias_still_refuses_inventory(self):
        original = ArchiveRoot.read
        def mutate(reader, relative, maximum):
            content = original(reader, relative, maximum)
            if relative == "module.py":
                self.alias.write_text("import changed_via_alias\n")
            return content
        with patch.object(ArchiveRoot, "read", mutate), self.assertRaises(GraphError):
            crawl_repository(self.repo, policy=self.policy)

    def test_source_opt_in_keeps_byte_limits(self):
        snapshot = crawl_repository(self.repo, policy=CrawlPolicy(max_file_bytes=1, allow_hardlinked_source=True)).to_dict()
        self.assertNotIn("chosen_shared_module", json.dumps(snapshot))
        self.assertIn("per-file byte ceiling", "\n".join(snapshot["warnings"]))

    def test_source_policy_requires_a_boolean(self):
        for value in ("yes", 1, None, []):
            with self.subTest(value=value), self.assertRaises(GraphError):
                CrawlPolicy(allow_hardlinked_source=value)

    def test_cli_opt_in_is_per_crawl_and_saved_snapshot_does_not_gain_permission(self):
        database = self.root / "graph.sqlite3"
        common = ["repo", "crawl", "--workspace", str(self.repo), "--format", "json"]
        code, text, error = self.invoke(common + ["--allow-hardlinked-source", "--db", str(database)])
        self.assertEqual(code, 0, error)
        self.assertIn("chosen_shared_module", text)
        saved = json.loads(text)
        code, text, error = self.invoke(common)
        self.assertEqual(code, 0, error)
        self.assertNotIn("chosen_shared_module", text)
        code, text, error = self.invoke(["repo", "show", "--db", str(database), "--format", "json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["snapshot_id"], saved["snapshot_id"])
        code, _, error = self.invoke(["repo", "show", "--db", str(database), "--allow-hardlinked-source"])
        self.assertEqual(code, 2)
        self.assertIn("not a stored snapshot", error)

    def test_interactive_opt_in_is_explicit_and_not_retained_by_next_command(self):
        controller = InteractiveController(self.repo, state_root=self.root / "state")
        authority_fields = ("plan", "plan_digest", "approved_digest", "run_id", "status", "model", "effort")
        original = {key: controller.session.get(key) for key in authority_fields}
        selected = controller.handle("/repo --allow-hardlinked-source")
        self.assertIn("chosen_shared_module", selected.messages[0])
        self.assertIn("aliases may exist outside", selected.messages[0])
        default = controller.handle("/repo")
        self.assertNotIn("chosen_shared_module", default.messages[0])
        self.assertEqual({key: controller.session.get(key) for key in authority_fields}, original)
        self.assertNotIn("allow_hardlinked_source", controller.session)
        duplicate = controller.handle("/repo --allow-hardlinked-source --allow-hardlinked-source")
        self.assertIn("usage:", duplicate.messages[0])


if __name__ == "__main__":
    unittest.main()
