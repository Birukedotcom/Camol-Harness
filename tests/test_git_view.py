import asyncio
import copy
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from camol.admission import AdmissionController
from camol.git_view import GitViewError, execution_environment, load_view, prepare_view, view_path
from camol.runbook import runbook_digest
from camol.sandbox import MacOSSandboxBackend, select_backend
from tests import test_admission as fixture


class GitInspectionTests(unittest.TestCase):
    setUp = fixture.AdmissionControllerTests.setUp
    tearDown = fixture.AdmissionControllerTests.tearDown

    def prepare(self, hardened=False):
        runbook = copy.deepcopy(self.runbook)
        if hardened:
            runbook["agents"][0]["trust_tier"] = "developer_sandboxed"
            # Exercise the exact declared runtime, including non-system venvs.
            runbook["agents"][0]["adapter"]["argv"][0] = sys.executable
        return AdmissionController(runbook, self.manager).prepare(plan_digest=runbook_digest(runbook),
            task=runbook["tasks"][0], agent=runbook["agents"][0], granted_by="owner")

    def test_private_view_excludes_source_config_history_and_unreachable_objects(self):
        previous = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"]).decode().strip()
        (self.source / "tracked.txt").write_text("approved\n")
        fixture.git(self.source, "add", ".")
        fixture.git(self.source, "commit", "-qm", "approved snapshot")
        unreachable = subprocess.check_output(["git", "-C", str(self.source), "hash-object", "-w", "--stdin"], input=b"other-box-private-object").decode().strip()
        fixture.git(self.source, "config", "remote.origin.url", "https://user:secret@example.invalid/private")
        fixture.git(self.source, "config", "core.hooksPath", str(self.root / "untrusted-hooks"))
        bundle, handle = self.prepare()
        root = view_path(self.state, bundle.workspace.workspace_id)
        environment = dict(os.environ, **execution_environment(self.state, handle.path, bundle))
        git = lambda *args: subprocess.run(["git", *args], cwd=str(handle.path), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(git("rev-parse", "HEAD").stdout.decode().strip(), handle.receipt.base_revision)
        self.assertEqual(git("rev-list", "--count", "HEAD").stdout.strip(), b"1")
        self.assertNotEqual(git("cat-file", "-e", previous).returncode, 0)
        self.assertNotEqual(git("cat-file", "-e", unreachable).returncode, 0)
        self.assertNotEqual(git("config", "--get", "remote.origin.url").returncode, 0)
        self.assertNotIn(b"secret", (root / "config").read_bytes())
        self.assertFalse((root / "objects/info/alternates").exists())
        manifest = load_view(root, workspace=handle.path)
        self.assertEqual(prepare_view(self.source, self.state, handle)[1], manifest)
        (root / "HEAD").write_text(previous + "\n")
        with self.assertRaisesRegex(GitViewError, "bytes differ"):
            execution_environment(self.state, handle.path, bundle)

    def test_legacy_admission_without_pinned_view_is_denied(self):
        bundle, handle = self.prepare()
        missing = replace(bundle.probe_policy, required_probes=tuple(probe for probe in bundle.probe_policy.required_probes
                          if probe.probe_id != "control-plane.git-inspection"))
        with self.assertRaisesRegex(GitViewError, "migration required"):
            execution_environment(self.state, handle.path, replace(bundle, probe_policy=missing))

    def test_oversized_view_and_symlinked_storage_fail_closed(self):
        handle = self.manager.prepare_task("bounded", "task", "worker")
        with patch("camol.git_view.MAX_BYTES", 1):
            with self.assertRaisesRegex(GitViewError, "byte bound"):
                prepare_view(self.source, self.state, handle)
        self.assertFalse(view_path(self.state, handle.receipt.workspace_id).exists())
        self.assertEqual(list((self.state / "git-views").iterdir()), [])
        (self.state / "git-views").rmdir()
        outside = self.root / "outside-view-storage"
        outside.mkdir()
        (self.state / "git-views").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(GitViewError, "symlinks"):
            prepare_view(self.source, self.state, handle)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_real_sandbox_git_status_diff_and_scratch_without_shared_metadata_access(self):
        (self.source / "tracked.txt").write_text("before\n")
        (self.source / ".gitattributes").write_text("tracked.txt filter=trap diff=trap\n")
        fixture.git(self.source, "add", ".")
        fixture.git(self.source, "commit", "-qm", "inspection fixture")
        sentinel = self.root / "hook-ran"
        included = self.root / "source-only-config"
        included.write_text('[filter "trap"]\n clean = touch {}\n smudge = touch {}\n[remote "origin"]\n url = https://user:secret@example.invalid/private\n'.format(sentinel, sentinel))
        fixture.git(self.source, "config", "include.path", str(included))
        bundle, handle = self.prepare(hardened=True)
        environment = execution_environment(self.state, handle.path, bundle)
        script = '''import json,os,pathlib,subprocess,tempfile
def git(*args):
 p=subprocess.run(['git',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 return {'code':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()}
pathlib.Path('tracked.txt').write_text('after\\n')
data={'status':git('status','--porcelain'), 'diff':git('diff','--no-ext-diff','--no-textconv'), 'remote':git('config','--get','remote.origin.url')}
denied=[]
for target in (os.environ['GIT_DIR']+'/HEAD',os.environ['GIT_DIR']+'/config',%r,%r):
 try: pathlib.Path(target).write_text('BAD')
 except OSError: denied.append(target)
data['denied']=denied
try: data['shared_secret']=pathlib.Path(%r).read_text()
except OSError: data['shared_secret']='denied'
data['commit']=git('commit','-am','unauthorized')
with tempfile.NamedTemporaryFile() as f: data['scratch']=str(pathlib.Path(f.name).parent)==os.environ['TMPDIR']
print(json.dumps(data))
''' % (str(self.source / ".git/config"), str(included), str(included))
        result = asyncio.run(select_backend(bundle.sandbox_policy).run([sys.executable, "-c", script],
            cwd=handle.path, policy=bundle.sandbox_policy, environment=environment, timeout_seconds=20))
        self.assertEqual(result.exit_code, 0, result.stderr.decode())
        report = json.loads(result.stdout)
        self.assertEqual(report["status"]["code"], 0, report)
        self.assertIn("tracked.txt", report["status"]["stdout"])
        self.assertEqual(report["diff"]["code"], 0, report)
        self.assertIn("+after", report["diff"]["stdout"])
        self.assertNotEqual(report["remote"]["code"], 0)
        self.assertNotEqual(report["commit"]["code"], 0)
        self.assertEqual(len(report["denied"]), 4)
        self.assertEqual(report["shared_secret"], "denied")
        self.assertTrue(report["scratch"])
        self.assertFalse(sentinel.exists())
        execution_environment(self.state, handle.path, bundle)


if __name__ == "__main__":
    unittest.main()
