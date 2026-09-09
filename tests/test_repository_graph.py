import json
import subprocess
import tempfile
import unittest
import sys
import importlib
from unittest.mock import patch
from pathlib import Path
from xml.etree import ElementTree

from camol.repository_graph import CrawlPolicy, GraphError, GraphSnapshot, GraphStore, RepositoryGraph, crawl_repository, diff_snapshots


class RepositoryGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_repeat_crawl_is_deterministic_with_evidence_backed_cycles_and_impact(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "import a\n")
        first, second = crawl_repository(self.repo), crawl_repository(self.repo)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        graph = RepositoryGraph(first)
        self.assertEqual(len(graph.cycles()), 1)
        self.assertEqual(len(graph.cycles()[0]), 2)
        impact = graph.impact("a.py")
        self.assertEqual([node["display_name"] for node in impact["nodes"]], ["b.py"])
        why = graph.why("a.py", "b.py")
        self.assertEqual(why["paths"][0][0]["kind"], "imports")
        self.assertTrue(why["paths"][0][0]["evidence_refs"])
        self.assertIn("[cycle]", graph.render_text())

    def test_crawl_never_executes_source_and_skips_secrets_ignored_files_and_symlinks(self):
        sentinel = self.root / "executed"
        self.write("trap.py", "from pathlib import Path\nPath({!r}).write_text('wrong')\n".format(str(sentinel)))
        self.write(".env", "PASSWORD=must-never-be-read")
        self.write(".env.production", "secret-key")
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", "raise RuntimeError('ignored')")
        secret = self.root / "outside.py"
        secret.write_text("external-secret-marker", encoding="utf-8")
        (self.repo / "escape.py").symlink_to(secret)
        self.git("add", "--force", ".env", ".env.production")
        snapshot = crawl_repository(self.repo)
        rendered = json.dumps(snapshot.to_dict())
        self.assertFalse(sentinel.exists())
        self.assertNotIn("must-never-be-read", rendered)
        self.assertNotIn("external-secret-marker", rendered)
        self.assertNotIn("file:.env", rendered)
        self.assertNotIn("file:ignored.py", rendered)
        self.assertEqual(snapshot.data["status"], "OBSERVATION_INCOMPLETE")
        self.assertTrue(any("symlink" in warning for warning in snapshot.data["warnings"]))

    def test_manifest_edges_do_not_claim_runtime_readiness_or_execute_scripts(self):
        self.write("package.json", json.dumps({"name": "top", "workspaces": ["packages/*"], "dependencies": {"inner": "workspace:*", "external": "1"}, "scripts": {"build": "touch SHOULD_NOT_RUN"}}))
        self.write("packages/inner/package.json", json.dumps({"name": "inner"}))
        self.write("Dockerfile", "FROM python:3.12\nRUN touch SHOULD_NOT_RUN\n")
        graph = RepositoryGraph(crawl_repository(self.repo))
        self.assertFalse((self.repo / "SHOULD_NOT_RUN").exists())
        self.assertTrue(graph.why("top", "inner")["paths"])
        self.assertTrue(graph.why("dockerfile:Dockerfile", "python:3.12")["paths"])
        self.assertIn("runtime capability", graph.render_text())

    def test_partial_parsing_and_limits_are_explicit(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "not valid python ???")
        invalid = crawl_repository(self.repo)
        self.assertEqual(invalid.data["status"], "OBSERVATION_INCOMPLETE")
        bounded = crawl_repository(self.repo, policy=CrawlPolicy(max_files=1))
        self.assertIn("file-count ceiling reached", bounded.data["warnings"])
        self.assertNotIn("file:b.py", json.dumps(bounded.to_dict()))

    def test_snapshot_export_diff_and_durable_store_preserve_evidence(self):
        self.write("a.py", "import b\n")
        first = crawl_repository(self.repo)
        self.write("b.py", "pass\n")
        second = crawl_repository(self.repo)
        changes = diff_snapshots(first, second)
        self.assertTrue(changes["nodes"]["added"])
        self.assertTrue(changes["edges"]["removed"])
        graph = RepositoryGraph(second)
        self.assertEqual(json.loads(graph.export("json"))["snapshot_id"], second.snapshot_id)
        self.assertIn("digraph camol", graph.export("dot"))
        self.assertTrue(ElementTree.fromstring(graph.export("graphml")).tag.endswith("graphml"))
        database = self.root / "graphs.sqlite3"
        store = GraphStore(database)
        try:
            store.save(first)
            store.save(second)
            store.save(second)
            self.assertEqual(len(store.list_snapshots()), 2)
            self.assertEqual(store.load(first.snapshot_id).snapshot_id, first.snapshot_id)
        finally:
            store.close()
        store = GraphStore(database, read_only=True)
        try:
            self.assertEqual(store.load().snapshot_id, second.snapshot_id)
            with self.assertRaises(GraphError):
                store.save(first)
        finally:
            store.close()

    def test_unknown_graph_fields_and_tampered_evidence_are_rejected(self):
        self.write("a.py", "pass\n")
        snapshot = crawl_repository(self.repo).to_dict()
        snapshot["nodes"][0]["runtime_ready"] = True
        with self.assertRaises(GraphError):
            GraphSnapshot.from_dict(snapshot)
        snapshot = crawl_repository(self.repo).to_dict()
        snapshot["edges"][0]["evidence_refs"] = ["missing"]
        with self.assertRaises(GraphError):
            GraphSnapshot.from_dict(snapshot)

    def test_custom_scanner_protocol_does_not_require_a_terminal(self):
        self.write("a.py", "pass\n")
        class Scanner:
            name, version = "fixture", "1"
            def supports(self, inventory):
                return True
            def scan(self, context):
                context.node("module", "fixture:item", name="fixture")
                return context.fragment
        snapshot = crawl_repository(self.repo, scanners=[Scanner()])
        self.assertEqual(snapshot.data["node_count"], 1)
        self.assertEqual(snapshot.data["scanner_versions"], {"fixture": "1"})

    def test_repository_cannot_shadow_optional_toml_parser_with_executable_code(self):
        sentinel = self.root / "parser-executed"
        self.write("pyproject.toml", '[project]\nname="fixture"\n')
        for parser in ("tomli", "tomllib"):
            self.write(parser + ".py", "from pathlib import Path\nPath({!r}).write_text('bad')\n".format(str(sentinel)))
        sys.path.insert(0, str(self.repo))
        try:
            # Exercise cold resolution even if an earlier test imported the
            # trusted stdlib parser. A cached trusted parser is safe to reuse.
            with patch.dict(sys.modules):
                sys.modules.pop("tomllib", None)
                sys.modules.pop("tomli", None)
                snapshot = crawl_repository(self.repo)
        finally:
            sys.path.pop(0)
        self.assertFalse(sentinel.exists())
        self.assertEqual(snapshot.data["status"], "OBSERVATION_INCOMPLETE")

    @unittest.skipUnless(sys.version_info >= (3, 11), "stdlib TOML parser requires Python 3.11")
    def test_preloaded_trusted_parser_ignores_repository_shadow_without_executing_it(self):
        parser = importlib.import_module("tomllib")
        sentinel = self.root / "parser-executed"
        self.write("pyproject.toml", '[project]\nname="fixture"\n')
        self.write("tomllib.py", "from pathlib import Path\nPath({!r}).write_text('bad')\n".format(str(sentinel)))
        sys.path.insert(0, str(self.repo))
        try:
            snapshot = crawl_repository(self.repo)
        finally:
            sys.path.pop(0)
        self.assertFalse(sentinel.exists())
        self.assertIs(sys.modules["tomllib"], parser)
        self.assertEqual(snapshot.data["status"], "STATIC_OBSERVED")


if __name__ == "__main__":
    unittest.main()
