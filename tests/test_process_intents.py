import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import camol
from camol.adapter import AdapterError, ProcessAgentAdapter, ProcessTurnUncertain
from camol import Harness
from camol.sandbox import DeveloperTrustedBackend, SandboxPolicy
from tests import test_api as api_fixture


class ProcessIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace, self.state = self.root / "source", self.root / "state"
        self.workspace.mkdir()
        self.counter = self.workspace / "effects.txt"
        program = "from pathlib import Path; p=Path('effects.txt'); p.write_text((p.read_text() if p.exists() else '')+'effect\\n'); raise SystemExit(7)"
        self.agent = dict(id="builder", box=".", adapter=dict(kind="process", argv=[sys.executable, "-c", program], timeout_seconds=2))
        self.assignment = dict(task_id="task", agent_id="builder", lease_id="lease-1", fence_digest="sha256:" + "a" * 64)
        self.packet = dict(run=dict(id="run"), lease=dict(lease_id="lease-1"))

    def adapter(self):
        return ProcessAgentAdapter(self.workspace, "run", state_dir=self.state)

    async def test_lost_result_does_not_authorize_same_turn_reexecution(self):
        for _ in range(2):
            with self.assertRaises(AdapterError):
                await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1)
        self.assertEqual(self.counter.read_text(), "effect\n", "the same turn repeated an already performed effect")

    async def test_another_lease_or_configuration_cannot_replace_prior_packet(self):
        with self.assertRaises(ProcessTurnUncertain):
            await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1)
        path = self.state / "packets/run/task/turn-001.packet.json"
        original = path.read_bytes()
        for assignment, agent, packet in (
            (dict(self.assignment, lease_id="lease-2"), self.agent, dict(self.packet, lease=dict(lease_id="lease-2"))),
            (self.assignment, dict(self.agent, box="other"), self.packet)):
            with self.assertRaises(ProcessTurnUncertain):
                await self.adapter().execute_turn(agent, assignment, packet, 1)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.counter.read_text(), "effect\n")

    async def test_concurrent_adapters_allocate_only_one_process(self):
        results = await asyncio.gather(*(self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1)
                                        for _ in range(2)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ProcessTurnUncertain) for result in results))
        self.assertEqual(self.counter.read_text(), "effect\n")

    async def test_separate_controllers_cannot_race_the_shared_packet(self):
        entered, release = self.root / "entered", self.root / "release"
        code = """import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from camol.adapter import ProcessAgentAdapter,AdapterError
data=json.loads(sys.argv[2]); entered=Path(data['entered']); release=Path(data['release'])
if data['delay']:
 original=ProcessAgentAdapter._execute_turn
 async def gated(self,*args,**kwargs):
  entered.write_text('controller holds turn before packet preparation')
  for _ in range(500):
   if release.exists(): return await original(self,*args,**kwargs)
   await asyncio.sleep(.01)
  raise RuntimeError('fixture gate timed out')
 ProcessAgentAdapter._execute_turn=gated
async def main():
 try:
  await ProcessAgentAdapter(Path(data['workspace']),'run',state_dir=Path(data['state'])).execute_turn(data['agent'],data['assignment'],data['packet'],1)
 except AdapterError: return 2
 return 0
raise SystemExit(asyncio.run(main()))
"""
        children = []
        async def start(delay, assignment):
            data = dict(workspace=str(self.workspace), state=str(self.state), agent=self.agent,
                assignment=assignment, packet=dict(self.packet, lease=dict(lease_id=assignment["lease_id"])),
                entered=str(entered), release=str(release), delay=delay)
            child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code,
                str(Path(camol.__file__).resolve().parent.parent), json.dumps(data),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            children.append(child)
            return child
        try:
            first = await start(True, self.assignment)
            for _ in range(500):
                if entered.exists():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.exists())
            second = await start(False, dict(self.assignment, lease_id="lease-2"))
            _, err = await asyncio.wait_for(second.communicate(), 5)
            self.assertEqual(second.returncode, 2, err.decode()[:500])
            self.assertFalse(self.counter.exists())
            self.assertFalse((self.state / "packets/run/task/turn-001.packet.json").exists())
            release.write_text("release first controller")
            _, err = await asyncio.wait_for(first.communicate(), 5)
            self.assertEqual(first.returncode, 2, err.decode()[:500])
            self.assertEqual(self.counter.read_text(), "effect\n")
        finally:
            release.write_text("fixture cleanup")
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.communicate()

    async def test_prelaunch_denial_can_retry_without_claiming_an_effect(self):
        adapter = self.adapter()
        async def deny(*args):
            raise AdapterError("fixture admission denied before process")
        adapter.before_launch = deny
        with self.assertRaises(AdapterError) as failure:
            await adapter.execute_turn(self.agent, self.assignment, self.packet, 1)
        self.assertNotIsInstance(failure.exception, ProcessTurnUncertain)
        self.assertFalse(self.counter.exists())
        assignment = dict(self.assignment, lease_id="lease-2")
        packet = dict(self.packet, lease=dict(lease_id="lease-2"))
        with self.assertRaises(ProcessTurnUncertain):
            await self.adapter().execute_turn(self.agent, assignment, packet, 1)
        self.assertEqual(self.counter.read_text(), "effect\n")

    async def test_unjournaled_packet_and_corrupt_cached_result_are_retained(self):
        directory = self.state / "packets/run/task"
        directory.mkdir(parents=True)
        packet = directory / "turn-001.packet.json"
        packet.write_text(json.dumps(self.packet))
        with self.assertRaises(ProcessTurnUncertain):
            await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1)
        result = directory / "turn-001.result.json"
        result.write_text("corrupt fixture result")
        with self.assertRaises(ProcessTurnUncertain):
            await self.adapter().execute_turn(self.agent, self.assignment, self.packet, 1)
        self.assertEqual(result.read_text(), "corrupt fixture result")
        self.assertFalse(self.counter.exists())

    async def test_success_cache_is_reused_but_missing_cache_never_reexecutes(self):
        code = """import hashlib,json,sys
from pathlib import Path
p=Path('effects.txt'); p.write_text((p.read_text() if p.exists() else '')+'effect\\n')
result=dict(status='complete',checkpoint='fixture result',summary='fixture complete',completed_step_ids=[],input_tokens=1,output_tokens=1,evidence=[],packet_sha256=hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
Path(sys.argv[2]).write_text(json.dumps(result))
"""
        agent = dict(self.agent, adapter=dict(kind="process", argv=[sys.executable, "-c", code, "{packet}", "{result}"], timeout_seconds=2))
        first = await self.adapter().execute_turn(agent, self.assignment, self.packet, 1)
        second = await self.adapter().execute_turn(agent, self.assignment, self.packet, 1)
        self.assertEqual(first["packet_sha256"], second["packet_sha256"])
        self.assertEqual(self.counter.read_text(), "effect\n")
        cache = self.state / "packets/run/task/turn-001.result.json"
        cache.unlink()  # Simulated loss of this test-owned trusted cache only.
        with self.assertRaises(ProcessTurnUncertain):
            await self.adapter().execute_turn(agent, self.assignment, self.packet, 1)
        self.assertEqual(self.counter.read_text(), "effect\n")

    async def test_cancellation_settles_child_and_preserves_launch_intent(self):
        agent = dict(self.agent, adapter=dict(kind="process", argv=[sys.executable, "-c",
            "from pathlib import Path; import time; Path('effects.txt').write_text('effect\\n'); time.sleep(3)"], timeout_seconds=5))
        policy = SandboxPolicy("fixture", str(self.workspace), (str(self.workspace), str(self.state)),
            (str(self.workspace), str(self.state / "packets/run/task/worker-output")), ("PATH",), (), (), "developer_trusted")
        adapter = ProcessAgentAdapter(self.workspace, "run", state_dir=self.state,
            sandbox_backend=DeveloperTrustedBackend(), sandbox_policy=policy)
        driver = asyncio.create_task(adapter.execute_turn(agent, self.assignment, self.packet, 1))
        try:
            for _ in range(500):
                if self.counter.exists():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.counter.exists())
            driver.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(driver, 5)
            record = json.loads((self.state / "packets/run/task/turn-001.invocation.json").read_text())
            self.assertEqual(record["state"], "terminated")
            with self.assertRaises(ProcessTurnUncertain):
                await adapter.execute_turn(agent, self.assignment, self.packet, 1)
            self.assertEqual(self.counter.read_text(), "effect\n")
        finally:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)


class ProcessIntentRunTests(unittest.IsolatedAsyncioTestCase):
    setUp = api_fixture.HarnessApiTests.setUp
    tearDown = api_fixture.HarnessApiTests.tearDown

    async def test_real_run_exposes_uncertainty_and_reopen_does_not_repeat_effect(self):
        runbook = json.loads((api_fixture.ROOT / "examples/local-n-box-runbook.json").read_text())
        runbook["run"]["max_concurrency"] = 1
        runbook["agents"] = runbook["agents"][:1]
        runbook["tasks"] = runbook["tasks"][:1]
        runbook["agents"][0]["adapter"]["argv"] = ["python3", "-c",
            "from pathlib import Path; p=Path('effects.txt'); p.write_text((p.read_text() if p.exists() else '')+'effect\\n'); raise SystemExit(7)"]
        plan = Path(self.temp.name) / "runbook.json"
        plan.write_text(json.dumps(runbook))
        with Harness(self.workspace, self.state_dir) as harness:
            state = harness.prepare(plan)
            harness.approve(by="owner", digest=state["plan_digest"])
            result = await asyncio.wait_for(harness.run_async(), 30)
            task = next(iter(result["tasks"].values()))
            self.assertEqual(task["waiting"]["code"], "EFFECT_UNKNOWN", task["waiting"])
            self.assertEqual(task["turn_count"], 0)
            self.assertEqual(task["attempts"], 1)
            self.assertFalse(any(event["type"] == "TASK_RETRY_SCHEDULED" for event in harness.events()))
        with Harness(self.workspace, self.state_dir) as harness:
            await asyncio.wait_for(harness.run_async(), 30)
            self.assertEqual(harness.state()["total_tokens"], 0)
            self.assertEqual(next(iter(harness.state()["tasks"].values()))["attempts"], 1,
                             "an unresolved turn must gate admission before issuing another lease")
        effects = list((self.state_dir / "worktrees").rglob("effects.txt"))
        self.assertTrue(effects)
        self.assertTrue(all(path.read_text() == "effect\n" for path in effects))
