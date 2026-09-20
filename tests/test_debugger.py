import copy
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camol.debugger import Debugger, DebuggerError
from camol.artifacts import ArtifactStore
from camol.debug_execution import DebugExecutionError, environment_digest, executable_digest, source_identity
from camol.sandbox import MacOSSandboxBackend, SandboxPolicy, system_read_paths
from camol.orchestrator import Orchestrator
from camol.readiness import WaitingReason
from camol.runbook import load_runbook
from camol.schema import canonical_digest
from camol.state import project
from camol.store import SQLiteEventStore


ROOT = Path(__file__).resolve().parents[1]


class DebuggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        self.store = SQLiteEventStore(self.state_dir / "run.db")
        self.artifacts = ArtifactStore(self.state_dir)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture Owner")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.workspace / "README").write_text("fixture\n")
        (self.workspace / ".gitignore").write_text("force-failure\n")
        self.commit("baseline")
        self.orchestrator = Orchestrator(self.store)
        state = self.orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
        self.run_id = state["run_id"]
        self.orchestrator.approve_plan(self.run_id, "owner", state["plan_digest"])
        self.orchestrator.start(self.run_id)
        self.debug = Debugger(self.orchestrator, self.run_id)
        self.case = self.debug.open("missing-file", "success without a file", "success creates the file",
                                    ["run fixture", "check file"], ["test_result"], target_authority="owner approved acceptance")
        executable = str(Path(sys.executable).resolve())
        policy = SandboxPolicy("debug-fixture", str(self.workspace.resolve()),
                               (str(self.workspace.resolve()),) + system_read_paths(executable),
                               (str(self.workspace.resolve()),),
                               ("PATH", "PYTHONDONTWRITEBYTECODE"), (), (), "developer_trusted")
        self.baseline = source_identity(self.workspace)
        self.reproduction = dict(
            schema="camol.debug_reproduction", schema_version=1, run_id=self.run_id, case_id="missing-file",
            plan_digest=state["plan_digest"], target_digest=self.case["target_digest"], baseline_source=self.baseline,
            environment={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}, sandbox_policy=policy.to_dict(), reviewed_by="owner",
            evaluator_assets=[],
            commands=[dict(command_id="target", argv=[executable, "-I", "-c",
                "from pathlib import Path; import sys; print('target checked'); sys.exit(0 if Path('success.txt').exists() and not Path('force-failure').exists() else 1)"],
                executable_digest=executable_digest(executable), cwd=".", timeout_seconds=4, expected_exit_codes=[0], guardrail=None),
                dict(command_id="guardrail", argv=[executable, "-I", "-c",
                    "from pathlib import Path; import sys; print('guardrail checked'); sys.exit(0 if Path('README').exists() else 1)"],
                executable_digest=executable_digest(executable), cwd=".", timeout_seconds=4, expected_exit_codes=[0], guardrail="source-intact")],
        )
        self.debug.freeze_reproduction("missing-file", self.reproduction, approved_by="owner")
        self.contract = dict(
            experiment_id="exp-1", hypothesis="write was skipped", change="restore the write",
            reproduction=["run fixture", "check file"], expected_delta="file exists",
            guardrails=["source-intact"], rollback_ref="fixture-base", max_attempts=1,
            max_seconds=10, max_cost_usd_micros=0, environment_digest=environment_digest(self.reproduction),
            candidate_digest=canonical_digest(self.baseline), author="builder",
        )

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(self.workspace)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def execute(self, identity, experiment=None):
        self.debug.authorize_execution("missing-file", identity, source_identity(self.workspace),
                                       approved_by="owner", experiment_id=experiment)
        return asyncio.run(self.debug.execute("missing-file", identity, self.artifacts))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def outcome(self, passed, *, experiment=False, producer="verifier", epistemic="EXECUTED", guardrail=None):
        data = {"passed": passed, "target_digest": self.case["target_digest"]}
        if experiment:
            data.update({name: self.contract[name] for name in ("experiment_id", "environment_digest", "candidate_digest")})
        if guardrail:
            data["guardrail"] = guardrail
        return self.debug.observe("missing-file", "test_result", data,
                                  producer=producer, epistemic_status=epistemic)

    def begin(self):
        red = self.execute("baseline")[0]
        self.debug.reproduce("missing-file", red)
        self.debug.localize("missing-file", dict(symptom="file absent", first_divergent_event=red,
                            deciding_boundary="writer", violated_contract="success requires output",
                            cause="missing write"), [red])
        (self.workspace / "success.txt").write_text("fixed\n")
        self.commit("candidate")
        self.contract["candidate_digest"] = canonical_digest(source_identity(self.workspace))
        self.debug.experiment("missing-file", self.contract)
        return red

    def finish(self, *, producer="verifier", guardrail=True):
        ids = self.execute("candidate", "exp-1")
        green = ids[0]
        if not guardrail:
            ids = ids[:1]
        self.debug.finish_experiment("missing-file", "exp-1", ids,
                                     attempts=1, duration_seconds=2, cost_usd_micros=0)
        return green

    def test_complete_debug_ratchet_replays_and_preserves_contradiction(self):
        red = self.begin()
        green = self.finish()
        self.debug.verify("missing-file", green, "target and source guardrail observed")
        self.debug.promote("missing-file", "missing-file-regression", dict(
            revision=1, fixture="fixtures/missing-file.json", oracle="tests/test_writer.py",
            environment_digest=self.contract["environment_digest"], owner="owner", reviewed_by="owner"))
        projected = project(self.store.read(self.run_id))
        self.assertEqual(projected, self.orchestrator.state(self.run_id))
        self.assertEqual(projected["evals"]["missing-file-regression"]["red_before"], red)
        (self.workspace / "force-failure").write_text("regressed external input\n")
        new_red = self.execute("contradiction", "exp-1")[0]
        self.debug.contradict("missing-file", new_red, "new executed input fails")
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["debug_cases"]["missing-file"]["status"], "experimenting")
        self.assertEqual(state["evals"]["missing-file-regression"]["review_status"], "contradicted")
        self.assertFalse(self.orchestrator.completion_report(self.run_id)["conditions"]["no_open_debug_cases"])

    def test_reported_observation_and_optimistic_claim_cannot_verify(self):
        fake = self.outcome(False, epistemic="HUMAN_REPORTED")
        before = len(self.store.read(self.run_id))
        with self.assertRaisesRegex(DebuggerError, "reported or inferred"):
            self.debug.reproduce("missing-file", fake)
        with self.assertRaisesRegex(DebuggerError, "cannot transition"):
            self.debug.verify("missing-file", fake, "looks fixed")
        self.assertEqual(len(self.store.read(self.run_id)), before)

    def test_forged_executed_label_never_becomes_a_kernel_receipt(self):
        fake = self.outcome(False, producer="debug-executor", epistemic="EXECUTED")
        state = self.orchestrator.state(self.run_id)
        self.assertEqual(state["evidence"][fake]["epistemic_status"], "UNVERIFIED")
        with self.assertRaisesRegex(DebuggerError, "reported or inferred"):
            self.debug.reproduce("missing-file", fake)
        events = self.store.read(self.run_id)
        events[-1]["payload"]["epistemic_status"] = "EXECUTED"
        with self.assertRaisesRegex(DebugExecutionError, "finished kernel receipt"):
            project(events)

    def test_authorization_is_one_shot_and_receipt_collection_never_reruns(self):
        ids = self.execute("baseline")
        before = len(self.store.read(self.run_id))
        self.assertEqual(self.debug.collect_execution("missing-file", "baseline", self.artifacts), ids)
        self.assertEqual(len(self.store.read(self.run_id)), before)
        with self.assertRaisesRegex(DebugExecutionError, "unused reviewed authorization"):
            asyncio.run(self.debug.execute("missing-file", "baseline", self.artifacts))
        with self.assertRaisesRegex(DebugExecutionError, "already consumed"):
            self.debug.authorize_execution("missing-file", "baseline", self.baseline, approved_by="owner")
        records = list((self.state_dir / "packets").rglob("*.invocation.json"))
        self.assertEqual(len(records), 2)

    def test_source_is_rechecked_before_launch_and_ignores_cannot_hide_tracked_edits(self):
        self.debug.authorize_execution("missing-file", "stale", self.baseline, approved_by="owner")
        self.git("update-index", "--assume-unchanged", "README")
        (self.workspace / "README").write_text("hidden changed bytes\n")
        with self.assertRaisesRegex(DebugExecutionError, "working source bytes"):
            asyncio.run(self.debug.execute("missing-file", "stale", self.artifacts))
        self.assertEqual(self.debug.inspect("missing-file")["executions"]["stale"]["status"], "authorized")
        self.assertFalse((self.state_dir / "packets").exists())

    def test_source_inventory_never_executes_repository_clean_filters(self):
        (self.workspace / ".gitattributes").write_text("README filter=hostile\n")
        self.commit("filter attributes")
        sentinel = Path(self.temp.name) / "filter-executed"
        self.git("config", "filter.hostile.clean", "touch " + str(sentinel))
        identity = source_identity(self.workspace)
        self.assertTrue(identity["checkout_digest"].startswith("sha256:"))
        self.assertFalse(sentinel.exists())

    def test_active_executor_cannot_be_reconciled_away(self):
        from camol.debug_execution import EXECUTOR
        from camol.sandbox import process_start_fingerprint
        self.debug.authorize_execution("missing-file", "active", self.baseline, approved_by="owner")
        self.debug._execution_commit("DEBUG_EXECUTION_STARTED", "missing-file", EXECUTOR,
                                     execution_id="active", owner_pid=os.getpid(),
                                     owner_started=process_start_fingerprint(os.getpid()))
        with self.assertRaisesRegex(DebugExecutionError, "still alive"):
            self.debug.reconcile_execution("missing-file", "active", self.artifacts,
                                            approved_by="owner", resolution="unproved abandonment")

    def test_frozen_contract_owner_and_receipt_replay_are_enforced(self):
        with self.assertRaisesRegex(DebugExecutionError, "approving run owner"):
            self.debug.authorize_execution("missing-file", "bad-owner", self.baseline, approved_by="worker")
        ids = self.execute("baseline")
        events = self.store.read(self.run_id)
        changed = copy.deepcopy(events)
        finished = next(event for event in changed if event["type"] == "DEBUG_EXECUTION_FINISHED")
        finished["payload"]["receipt"]["results"][0]["argv"][-1] = "raise SystemExit(0)"
        with self.assertRaisesRegex(DebugExecutionError, "reviewed argv"):
            project(changed)
        changed = copy.deepcopy(events)
        evidence = next(event for event in changed if event["type"] == "EVIDENCE_RECORDED" and event["payload"]["evidence_id"] == ids[0])
        evidence["payload"]["data"]["passed"] = True
        with self.assertRaisesRegex(DebugExecutionError, "metadata differs"):
            project(changed)
        changed = copy.deepcopy(events)
        next(event for event in changed if event["type"] == "DEBUG_EXECUTION_AUTHORIZED")["actor_id"] = "worker"
        with self.assertRaisesRegex(DebugExecutionError, "approving run owner"):
            project(changed)

    def test_preexisting_red_cannot_contradict_a_verified_candidate(self):
        red = self.begin()
        green = self.finish()
        self.debug.verify("missing-file", green, "independent oracle passed")
        with self.assertRaisesRegex(DebuggerError, "newly executed"):
            self.debug.contradict("missing-file", red, "reusing old failure")

    def test_candidate_cannot_replace_a_frozen_oracle_asset(self):
        self.debug.open("asset-case", "file absent", "success creates the file", ["run fixture", "check file"],
                        ["test_result"], target_authority="owner approved acceptance")
        reproduction = copy.deepcopy(self.reproduction)
        reproduction["case_id"] = "asset-case"
        reproduction["evaluator_assets"] = [{"path": "README", "digest": "sha256:" + hashlib.sha256(b"fixture\n").hexdigest()}]
        self.debug.freeze_reproduction("asset-case", reproduction, approved_by="owner")
        self.debug.authorize_execution("asset-case", "red", self.baseline, approved_by="owner")
        red = asyncio.run(self.debug.execute("asset-case", "red", self.artifacts))[0]
        self.debug.reproduce("asset-case", red)
        self.debug.localize("asset-case", dict(symptom="file absent", first_divergent_event=red,
                            deciding_boundary="writer", violated_contract="output required", cause="write absent"), [red])
        (self.workspace / "README").write_text("weakened oracle helper\n")
        (self.workspace / "success.txt").write_text("candidate\n")
        self.commit("candidate tampers with oracle")
        source = source_identity(self.workspace)
        experiment = dict(self.contract, candidate_digest=canonical_digest(source),
                          environment_digest=environment_digest(reproduction))
        self.debug.experiment("asset-case", experiment)
        self.debug.authorize_execution("asset-case", "candidate", source, approved_by="owner", experiment_id="exp-1")
        with self.assertRaisesRegex(DebugExecutionError, "frozen evaluator asset"):
            asyncio.run(self.debug.execute("asset-case", "candidate", self.artifacts))
        self.assertEqual(self.debug.inspect("asset-case")["executions"]["candidate"]["status"], "authorized")

    def test_debug_child_cannot_receive_controller_write_authority(self):
        self.debug.open("unsafe-case", "file absent", "success creates the file", ["run fixture", "check file"],
                        ["test_result"], target_authority="owner approved acceptance")
        reproduction = copy.deepcopy(self.reproduction)
        reproduction["case_id"] = "unsafe-case"
        reproduction["sandbox_policy"]["write_paths"].append(str(self.state_dir.resolve()))
        self.debug.freeze_reproduction("unsafe-case", reproduction, approved_by="owner")
        self.debug.authorize_execution("unsafe-case", "red", self.baseline, approved_by="owner")
        with self.assertRaisesRegex(DebugExecutionError, "controller evidence"):
            asyncio.run(self.debug.execute("unsafe-case", "red", self.artifacts))
        self.assertFalse((self.state_dir / "packets").exists())

    def test_hardened_reproduction_rejects_writable_oracle_at_freeze(self):
        from camol.debug_execution import validate_contract
        reproduction = copy.deepcopy(self.reproduction)
        reproduction["sandbox_policy"]["trust_tier"] = "developer_sandboxed"
        with self.assertRaisesRegex(DebugExecutionError, "read-only source and evaluator"):
            validate_contract(reproduction)
        reproduction["sandbox_policy"]["write_paths"] = []
        with self.assertRaisesRegex(DebugExecutionError, "explicitly protect"):
            validate_contract(reproduction)

    @unittest.skipUnless(MacOSSandboxBackend.available(), "requires macOS sandbox-exec")
    def test_enforced_measurement_denies_transient_oracle_mutate_and_restore(self):
        self.debug.open("transient-oracle", "strict oracle is red", "strict oracle stays red", ["run fixture"],
                        ["test_result"], target_authority="owner approved acceptance")
        reproduction = copy.deepcopy(self.reproduction)
        reproduction["case_id"] = "transient-oracle"
        reproduction["target_digest"] = self.debug.inspect("transient-oracle")["target_digest"]
        reproduction["evaluator_assets"] = [{"path": "README", "digest": "sha256:" + hashlib.sha256(b"fixture\n").hexdigest()}]
        reproduction["sandbox_policy"].update(schema_version=2, trust_tier="developer_sandboxed",
                                                write_paths=[], readonly_paths=[str(self.workspace.resolve())])
        reproduction["commands"] = [dict(reproduction["commands"][0], argv=[str(Path(sys.executable).resolve()), "-I", "-c", """from pathlib import Path
import sys
oracle = Path('README')
original = oracle.read_bytes()
try:
    oracle.write_bytes(b'weakened')
    passed = oracle.read_bytes() == b'weakened'
finally:
    if oracle.read_bytes() != original:
        oracle.write_bytes(original)
sys.exit(0 if passed else 1)
"""])]
        self.debug.freeze_reproduction("transient-oracle", reproduction, approved_by="owner")
        self.debug.authorize_execution("transient-oracle", "blocked-write", self.baseline, approved_by="owner")
        evidence = asyncio.run(self.debug.execute("transient-oracle", "blocked-write", self.artifacts))[0]
        data = self.orchestrator.state(self.run_id)["evidence"][evidence]["data"]
        self.assertFalse(data["passed"])
        self.assertEqual(data["source_isolation"], "read_only_source_enforced")
        self.assertEqual(source_identity(self.workspace), self.baseline)

    def test_unrestricted_measurement_is_visibly_hash_checks_only(self):
        evidence = self.execute("red")[0]
        data = self.orchestrator.state(self.run_id)["evidence"][evidence]["data"]
        self.assertEqual(data["source_isolation"], "hash_checks_only")

    def test_real_counterexample_is_nonblocking_until_atomic_owner_activation(self):
        from tests.test_evaluation import AGENT, runbook
        from camol.runner import HarnessRunner
        (self.workspace / "agent.py").write_text(AGENT)
        self.commit("real bounded worker")
        plan = runbook("inbox-proof", "refine once")
        state = self.orchestrator.initialize(plan)
        self.orchestrator.approve_plan("inbox-proof", "owner", state["plan_digest"])
        final = asyncio.run(HarnessRunner(self.orchestrator, self.workspace,
                                         state_dir=self.state_dir).run_until_terminal("inbox-proof"))
        self.assertEqual(final["status"], "completed")
        debugger = Debugger(self.orchestrator, "inbox-proof")
        inbox = debugger.inbox()
        self.assertEqual(len(inbox), 1)
        self.assertFalse(inbox[0]["blocking"])
        self.assertEqual(inbox[0]["status"], "untriaged")
        self.assertTrue(inbox[0]["verifier_evidence_ids"])
        self.assertEqual(debugger.inspect(), {})
        target, authority = "output is exactly good", "owner reviewed the frozen acceptance"
        contract = copy.deepcopy(self.reproduction)
        contract.update(run_id="inbox-proof", case_id="output-gap", plan_digest=state["plan_digest"],
                        target_digest=canonical_digest({"target_behavior": target, "target_authority": authority}),
                        baseline_source=source_identity(self.workspace))
        contract["commands"] = contract["commands"][:1]
        contract["commands"][0]["argv"][-1] = "from pathlib import Path; assert Path('output.txt').read_text() == 'good\\n'"
        before = len(self.store.read("inbox-proof"))
        kwargs = dict(case_id="output-gap", target_behavior=target, target_authority=authority,
                      reproduction=["run the frozen output oracle"], contract=contract)
        with self.assertRaisesRegex(DebuggerError, "approving owner"):
            debugger.activate_counterexample(inbox[0]["counterexample_id"], approved_by="worker", **kwargs)
        self.assertEqual(len(self.store.read("inbox-proof")), before)
        activated = debugger.activate_counterexample(inbox[0]["counterexample_id"], approved_by="owner", **kwargs)
        self.assertEqual(activated["status"], "open")
        self.assertEqual(activated["counterexample_evidence_ids"], inbox[0]["verifier_evidence_ids"])
        self.assertEqual(activated["execution_contract"], contract)
        self.assertFalse(activated["evidence_ids"], "original worker failure is not a new debug execution")
        self.assertEqual(debugger.inbox()[0]["status"], "activated")
        self.assertEqual(project(self.store.read("inbox-proof")), self.orchestrator.state("inbox-proof"))

    def test_builder_cannot_verify_own_experiment_and_guardrails_are_required(self):
        self.contract["author"] = "debug-executor"
        self.begin()
        green = self.finish(producer="builder")
        with self.assertRaisesRegex(DebuggerError, "author cannot"):
            self.debug.verify("missing-file", green, "fixed")

    def test_missing_guardrail_prevents_verification(self):
        self.begin()
        green = self.finish(guardrail=False)
        with self.assertRaisesRegex(DebuggerError, "guardrail"):
            self.debug.verify("missing-file", green, "fixed")

    def test_experiment_cannot_exceed_bounds_or_swap_candidate(self):
        self.begin()
        green = self.execute("candidate", "exp-1")[0]
        with self.assertRaisesRegex(DebuggerError, "max_attempts"):
            self.debug.finish_experiment("missing-file", "exp-1", [green],
                                         attempts=2, duration_seconds=2, cost_usd_micros=0)
        bad = self.debug.observe("missing-file", "test_result", {
            "passed": True, "experiment_id": "exp-1", "candidate_digest": canonical_digest("other"),
            "environment_digest": self.contract["environment_digest"],
        }, producer="verifier", epistemic_status="EXECUTED")
        with self.assertRaisesRegex(DebuggerError, "reported or inferred"):
            self.debug.finish_experiment("missing-file", "exp-1", [bad],
                                         attempts=1, duration_seconds=2, cost_usd_micros=0)

    def test_block_resume_and_replay_reject_invalid_transition(self):
        self.debug.block("missing-file", WaitingReason(
            code="TARGET_UNREACHABLE", detail="fixture unavailable", wake_condition="fixture restored"))
        self.assertEqual(self.debug.inspect("missing-file")["status"], "blocked")
        self.debug.resume("missing-file", "fixture restored")
        self.assertEqual(self.debug.inspect("missing-file")["status"], "open")
        events = self.store.read(self.run_id)
        corrupt = copy.deepcopy(events)
        corrupt[-1]["type"] = "DEBUG_VERIFIED"
        with self.assertRaises(DebuggerError):
            project(corrupt)

    def test_projection_cache_is_detached_and_sees_other_writer(self):
        state = self.orchestrator.state(self.run_id)
        state["debug_cases"].clear()
        self.assertIn("missing-file", self.orchestrator.state(self.run_id)["debug_cases"])
        other = Orchestrator(self.store)
        Debugger(other, self.run_id).open("another", "red", "green", ["check"], ["test_result"], target_authority="owner")
        self.assertIn("another", self.orchestrator.state(self.run_id)["debug_cases"])


if __name__ == "__main__":
    unittest.main()
