"""Bounded characterization: process-group exit is not descendant containment.

The child always self-terminates within 2.5 seconds and writes only inside its
disposable worker directory; all sibling write attempts must be denied.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from camol.sandbox import MacOSSandboxBackend, SandboxPolicy, select_backend, system_read_paths
from camol.workspace import WorkspaceManager


@unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS Seatbelt")
class DescendantBoundaryTests(unittest.TestCase):
    def test_current_limit_descendant_can_write_old_tree_but_not_captured_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, state = root / "source", root / "state"
            source.mkdir()
            (source / "output.txt").write_text("base\n")
            (source / ".gitignore").write_text("*.audit\n")
            def git(*args):
                return subprocess.run(["/usr/bin/git", "-C", str(source), *args], check=True, capture_output=True).stdout.decode().strip()
            git("init", "-q")
            git("config", "user.name", "Disposable Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", ".")
            git("commit", "-qm", "fixture")
            manager = WorkspaceManager(source, state)
            worker = manager.prepare_task("orphan-fixture", "change", "builder")
            verifier = manager.prepare_verifier("orphan-fixture", "change", "one", base_revision=git("rev-parse", "HEAD"))
            integration = manager.prepare_integration_generation("orphan-fixture", "change", "one", base_revision=git("rev-parse", "HEAD"))
            frozen = state / "frozen-evaluator.json"
            frozen.write_text('{"oracle":"strict"}')
            victims = [str(verifier.path / "output.txt"), str(integration.path / "output.txt"), str(frozen)]
            program = """import json,os,sys,time
from pathlib import Path
Path('output.txt').write_text('good\\n')
pid=os.fork()
if pid:
 print(json.dumps({'child_pid':pid}),flush=True)
 sys.exit(0)
os.setsid()
for fd in (0,1,2):
 try: os.close(fd)
 except OSError: pass
deadline=time.monotonic()+2.5
while not Path('begin.audit').exists() and time.monotonic()<deadline: time.sleep(.01)
counts={'writes':0,'denied':0,'escaped_writes':0,'pgid':os.getpgrp()}
while time.monotonic()<deadline:
 Path('output.txt').write_text('late-corruption\\n')
 counts['writes']+=1
 for item in json.loads(sys.argv[1]):
  try:
   Path(item).write_text('corrupt')
   counts['escaped_writes']+=1
  except PermissionError: counts['denied']+=1
 time.sleep(.02)
Path('done.audit').write_text(json.dumps(counts))
os._exit(0)
"""
            executable = str(Path(sys.executable).resolve())
            policy = SandboxPolicy("orphan-fixture", str(worker.path), (str(worker.path),) + system_read_paths(executable),
                                   (str(worker.path),), ("PATH",), (), (), "developer_sandboxed")
            async def exercise():
                result = await select_backend(policy, invocation_root=state / "packets").run(
                    [executable, "-I", "-c", program, json.dumps(victims)], cwd=worker.path, policy=policy,
                    timeout_seconds=4, invocation_record=state / "packets" / "parent.json")
                self.assertEqual(result.exit_code, 0, result.stderr)
                candidate = manager.salvage(worker)
                manager.materialize_candidate(candidate, verifier)
                manager.materialize_candidate(candidate, integration)
                revision = manager.commit_workspace(integration, "captured fixture")
                (worker.path / "begin.audit").write_text("go")
                deadline = time.monotonic() + 3
                while not (worker.path / "done.audit").exists() and time.monotonic() < deadline:
                    self.assertEqual((verifier.path / "output.txt").read_text(), "good\n")
                    self.assertEqual((integration.path / "output.txt").read_text(), "good\n")
                    await asyncio.sleep(.02)
                counts = json.loads((worker.path / "done.audit").read_text())
                self.assertNotEqual(counts["pgid"], result.process_group_id)
                self.assertGreater(counts["writes"], 0)
                self.assertGreater(counts["denied"], 0)
                self.assertEqual(counts["escaped_writes"], 0)
                self.assertEqual((worker.path / "output.txt").read_text(), "late-corruption\n")
                self.assertEqual(frozen.read_text(), '{"oracle":"strict"}')
                self.assertEqual(manager.head_revision(integration), revision)
                self.assertEqual(json.loads((state / "packets" / "parent.json").read_text())["state"], "completed")
            asyncio.run(exercise())
