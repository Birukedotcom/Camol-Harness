import asyncio
import copy
import getpass
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from camol.app import InteractiveController, validate_envelope
from camol.conversation import ConversationReply
from camol.draft_creation import creation_prompt, parse_creation_response
from camol.gates import GatePolicy, Invariant, Obligation
from camol.planning import PlanningError
from camol.runbook import validate_runbook
from camol.schema import canonical_digest
from camol.supervisor import SupervisorPaths, send_control


WORKER = r'''import hashlib,json,sys,os
from pathlib import Path
packet_path,result_path=map(Path,sys.argv[1:3]); os.chdir(Path(__file__).resolve().parent)
raw=packet_path.read_bytes(); packet=json.loads(raw)
task=packet['task']['id']
Path(task+'.txt').write_text('good\n')
artifact=Path('proof-'+task+'.txt').resolve();artifact.write_text('actual fixture output\n')
result_path.write_text(json.dumps(dict(status='complete',packet_sha256=hashlib.sha256(raw).hexdigest(),
checkpoint='fixture implemented',completed_step_ids=['work'],input_tokens=100,output_tokens=50,
evidence=[dict(kind='command',data={'action':'write task output'}),
dict(kind='artifact',data={'path':str(artifact),'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}),
dict(kind='claim',data={'task':task})],summary='fixture implemented')))
'''


def model_candidate(envelope):
    runbook = dict(schema_version=5, run=copy.deepcopy(envelope["run"]),
                   agents=copy.deepcopy(envelope["agents"]), rules=copy.deepcopy(envelope["rules"]), tasks=[])
    requirements = envelope["requirements"]
    invariant_records = []
    for item in requirements:
        identity = item["requirement_id"]
        scope = "global" if item["kind"] == "invariants" else "task:" + ("left" if identity.endswith("1") else "right")
        modality = "preserved" if item["kind"] == "invariants" else "eventually"
        invariant_records.append(Invariant(identity, 1, envelope["owner"], scope, modality, item["text"], (),
            ("source/output bytes",), "normal", ("change the expected output byte",), ("test_result",), True, "human").to_dict())
    gates, obligations = [], []
    for index, identity in enumerate(("left", "right"), 1):
        ids = ["outcomes-" + str(index), "invariants-1"]
        command = ["python3", "-c", "from pathlib import Path; assert Path('" + identity + ".txt').read_text() == 'good\\n'; assert Path('README').read_text() == 'baseline\\n'"]
        runbook["tasks"].append(dict(id=identity, goal="Create " + identity + ".txt", depends_on=[],
            capabilities=["code"], acceptance=[requirements[index - 1]["text"]],
            required_evidence=["command", "artifact", "claim", "test_result"], max_attempts=2,
            steps=[dict(id="work", instruction="Implement this bounded output", commands=[dict(purpose="Proposed new work command", argv=["python3", "-c", "pass"])], completion=["output exists"])],
            verification=[dict(purpose="Independent expected-byte oracle", argv=command, cwd="workspace_root")], evaluator_assets=[]))
        obligation = Obligation(identity + "-accepted", envelope["owner"], "ACCEPTED", tuple(ids), (identity,))
        obligations.append(obligation.to_dict())
        policy = GatePolicy.compile(identity + "-gate", "basic").to_dict()
        policy["human_approval"] = True
        gates.append(dict(task_id=identity, policy=policy, invariant_ids=ids, obligation_ids=[obligation.obligation_id],
                          evaluators=[dict(verification_index=0, family="deterministic", invariant_ids=ids)]))
    runbook["state_model"] = dict(invariants=invariant_records, obligations=obligations, gates=gates, final_acceptance="human")
    return dict(runbook=validate_runbook(runbook), coverage=[dict(requirement_id=r["requirement_id"], invariant_ids=[r["requirement_id"]],
                rationale="The explicit expected-byte assertion falsifies a changed output or README.") for r in requirements], unresolved_questions=[])


class DraftCreationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "worker.py").write_text(WORKER)
        (self.workspace / "README").write_text("baseline\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.provider = Mock()
        self.controller = InteractiveController(self.workspace, state_root=self.root / "camol", converse_fn=self.provider)
        self.controller.handle("/model claude:fable")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True).stdout.decode().strip()

    def tearDown(self):
        state = Path(self.controller.session["state_dir"])
        if SupervisorPaths.under(state).socket.exists():
            asyncio.run(send_control(state, "stop", requested_by=getpass.getuser()))
            deadline = time.monotonic() + 5
            while SupervisorPaths.under(state).socket.exists() and time.monotonic() < deadline:
                time.sleep(.02)
        self.temporary.cleanup()

    def wizard(self, worker="process --trusted python3 {workspace}/worker.py {packet} {result}"):
        result = self.controller.handle("/grill --draft Build two independently verified output files")
        self.assertIn("DRAFT GRILL", result.messages[0])
        for answer in ("left.txt contains good; right.txt contains good", "No deployment or upload", "README stays baseline",
                       "Unknown whether the output should include a newline; ask before choosing the exact byte oracle", worker,
                       "boxes=2 concurrency=2 turns=4 tokens=20000 cost_cents=0 timeout=30 tasks=4 attempts=2"):
            result = self.controller.handle(answer)
            self.assertNotIn("denied:", result.messages[0])
        return self.controller.session["grill"]["envelope"]

    def confirm(self):
        envelope = self.controller.session["grill"]["envelope"]
        self.assertIn("confirmed", self.controller.handle("/draft confirm " + canonical_digest(envelope)).messages[0])

    def propose(self):
        self.provider.return_value = ConversationReply(json.dumps(model_candidate(self.controller.session["grill"]["envelope"])), "claude", "fable", None, 5, 8)
        result = self.controller.handle("/propose")
        self.assertIn("MODEL CANDIDATE", result.messages[0])
        return result

    def test_natural_goal_questions_candidate_real_n_boxes_and_human_acceptance(self):
        envelope = self.wizard()
        self.provider.assert_not_called()
        self.assertLessEqual(len(creation_prompt(envelope)), 12000)
        self.assertIn("confirm", self.controller.handle("/propose").messages[0])
        self.confirm()
        self.provider.return_value = ConversationReply('{"questions":["Should both output files contain good followed by a newline?"]}', "claude", "fable", None, 2, 3)
        result = self.controller.handle("/propose")
        self.assertIn("DRAFT QUESTIONS", result.messages[0])
        self.assertIsNone(self.controller.session["plan"])
        self.controller.handle("Yes. Each output file must contain exactly good followed by one newline.")
        self.assertEqual(self.provider.call_count, 1)
        self.assertIn("confirm", self.controller.handle("/propose").messages[0])
        self.confirm()
        self.propose()
        self.assertTrue(self.provider.call_args.kwargs["no_tools"])
        digest = self.controller.session["plan_digest"]
        self.assertIn("/review", self.controller.handle("/approve " + digest).messages[0])
        self.assertIn("exact /plan digest", self.controller.handle("/approve yes").messages[0])
        review = self.controller.handle("/review")
        self.assertIn("UNEXECUTED", review.messages[0])
        self.assertIn("behavioral_only_unenforced", review.messages[0])
        self.assertIn("review acknowledged", self.controller.handle("/review " + digest).messages[-1])
        self.assertIn("Approved", self.controller.handle("/approve " + digest).messages[0])
        self.assertIn("cannot enforce hard no-egress", self.controller.handle("/run").messages[0])
        started = self.controller.handle("/run --accept-draft-policy " + digest)
        paths = SupervisorPaths.under(Path(self.controller.session["state_dir"]))
        self.assertIn("detached", started.messages[-1], str(started.messages) + "\n" + (paths.log.read_text()[-20000:] if paths.log.exists() else "no supervisor log"))
        self.assertEqual(self.controller._control("plan")["source_binding"]["source"], envelope["source"])
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            snapshot = self.controller._control("acceptance")
            self.assertNotEqual(snapshot["status"], "blocked", snapshot)
            if snapshot["status"] == "awaiting_acceptance":
                break
            for task, gate in snapshot["pending_gates"].items():
                self.controller.handle("/gate {} {}".format(task, gate["assessment_digest"]))
            time.sleep(.05)
        self.assertEqual(snapshot["status"], "awaiting_acceptance", (snapshot, self.controller._control("status")))
        self.assertIn("Final outcome accepted", self.controller.handle("/accept " + snapshot["acceptance"]["outcome_digest"]).messages[0])
        final = self.controller._control("status")["run"]
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(final["agents"]), 2)
        self.assertEqual(self.provider.call_count, 2)
        self.assertFalse((self.workspace / "left.txt").exists())

    def test_model_cannot_change_frozen_profiles_limits_source_or_human_requirements(self):
        envelope = self.wizard()
        candidate = model_candidate(envelope)
        self.assertIn("runbook", parse_creation_response(json.dumps(candidate), envelope))
        for change in (
            lambda c: c["runbook"]["run"]["token_policy"].update(max_total_tokens=999999),
            lambda c: c["runbook"]["agents"][0]["adapter"].update(argv=["sh", "-c", "anything"]),
            lambda c: c["runbook"]["agents"][0].update(trust_tier="developer_sandboxed"),
            lambda c: c["runbook"]["tasks"][0].update(max_attempts=10),
            lambda c: c["runbook"]["state_model"]["invariants"][0].update(predicate="Tests pass"),
            lambda c: c["runbook"]["state_model"]["invariants"][0].update(preconditions=["Only if convenient"]),
            lambda c: c["runbook"]["state_model"]["invariants"][-1].update(modality="eventually"),
            lambda c: c["runbook"]["state_model"]["gates"][0]["policy"].update(human_approval=False),
        ):
            value = copy.deepcopy(candidate)
            change(value)
            with self.assertRaises((ValueError, PlanningError)):
                parse_creation_response(json.dumps(value), envelope)
        value = copy.deepcopy(candidate)
        value["coverage"].pop()
        self.assertIn("questions", parse_creation_response(json.dumps(value), envelope))
        value["unresolved_questions"] = ["Missing a real oracle"]
        self.assertEqual(parse_creation_response(json.dumps(value), envelope), {"questions": ["Missing a real oracle"]})

    def test_model_cannot_narrow_global_human_invariant_to_one_task(self):
        envelope = self.wizard()
        candidate = model_candidate(envelope)
        candidate["runbook"]["state_model"]["invariants"][-1]["scope"] = "task:left"
        gate = candidate["runbook"]["state_model"]["gates"][1]
        gate["invariant_ids"].remove("invariants-1")
        gate["evaluators"][0]["invariant_ids"].remove("invariants-1")
        candidate["runbook"]["state_model"]["obligations"][1]["invariant_ids"].remove("invariants-1")
        with self.assertRaisesRegex(PlanningError, "scope or modality"):
            parse_creation_response(json.dumps(candidate), envelope)

    def test_creation_candidate_is_durable_and_source_drift_rejects_approval(self):
        self.wizard()
        self.confirm()
        self.propose()
        original = self.controller.session["plan"]
        reloaded = InteractiveController(self.workspace, state_root=self.root / "camol", converse_fn=self.provider)
        self.assertEqual(validate_envelope(reloaded.session["plan"]), original)
        self.assertEqual(reloaded.session["grill"]["phase"], "candidate")
        digest = self.controller.session["plan_digest"]
        self.controller.handle("/review " + digest)
        (self.workspace / "README").write_text("drift\n")
        self.assertIn("denied", self.controller.handle("/approve " + digest).messages[0])
        self.assertIsNone(self.controller.session["approved_digest"])

    def test_no_implicit_call_and_provider_change_invalidates_creation_envelope(self):
        self.wizard()
        self.assertIn("confirm", self.controller.handle("/draft confirm yes").messages[0])
        self.controller.handle("/model local:fixture")
        self.assertIsNone(self.controller.session["grill"])
        self.assertIn("denied", self.controller.handle("/propose").messages[0])
        self.provider.assert_not_called()

    def test_malformed_candidate_is_denied_without_losing_previous_candidate_or_usage(self):
        self.wizard()
        self.confirm()
        self.propose()
        previous = copy.deepcopy(self.controller.session["plan"])
        digest = self.controller.session["plan_digest"]
        self.controller.handle("/review " + digest)
        mutations = (
            lambda c: c["runbook"]["state_model"]["invariants"][0].update(modality=[]),
            lambda c: c["runbook"]["state_model"]["gates"][0].update(policy=[]),
            lambda c: c["coverage"][0].update(requirement_id=[]),
            lambda c: c["runbook"]["tasks"][0]["steps"][0].update(commands=[None]),
        )
        for mutate in mutations:
            value = model_candidate(self.controller.session["grill"]["envelope"])
            mutate(value)
            self.provider.return_value = ConversationReply(json.dumps(value), "claude", "fable", None, 11, 7)
            result = self.controller.handle("/propose")
            self.assertIn("denied:", result.messages[0])
            self.assertEqual(self.controller.session["plan"], previous)
            self.assertIsNone(self.controller.session["approved_digest"])
            self.assertEqual(self.controller.store.planning_calls()[-1]["input_tokens"], 11)
            self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "rejected")
        self.assertFalse(Path(self.controller.session["state_dir"]).exists())

    def test_source_change_during_planning_rejects_candidate_with_usage_retained(self):
        self.wizard()
        self.confirm()
        envelope = self.controller.session["grill"]["envelope"]
        def mutate_source(*args, **kwargs):
            (self.workspace / "README").write_text("mutated by outside owner during request\n")
            return ConversationReply(json.dumps(model_candidate(envelope)), "claude", "fable", None, 9, 4)
        self.provider.side_effect = mutate_source
        self.assertIn("denied:", self.controller.handle("/propose").messages[0])
        self.assertIsNone(self.controller.session["plan"])
        self.assertEqual(self.controller.store.planning_calls()[-1]["input_tokens"], 9)
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "rejected")

    def test_creation_cancellation_keeps_usage_and_never_installs_candidate(self):
        self.wizard()
        self.confirm()
        def cancelled_reply(*args, **kwargs):
            self.controller.cancel_active()
            return ConversationReply('{"questions":["Would you clarify?"]}', "claude", "fable", None, 6, 2)
        self.provider.side_effect = cancelled_reply
        self.assertIn("cancelled", self.controller.handle("/propose").messages[0])
        self.assertIsNone(self.controller.session["plan"])
        self.assertIsNone(self.controller.session["approved_digest"])
        call = self.controller.store.planning_calls()[-1]
        self.assertEqual((call["status"], call["input_tokens"]), ("cancelled", 6))
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "cancelled")

    def test_duplicate_keys_are_denied_and_sensitive_output_is_not_persisted(self):
        self.wizard()
        self.confirm()
        self.provider.return_value = ConversationReply('{"questions":["one"],"questions":["two"]}', "claude", "fable", None, 2, 1)
        self.assertIn("denied:", self.controller.handle("/propose").messages[0])
        secret = "sk-live-abcdefghijklmnopqrstuvwxyz123456"
        self.provider.return_value = ConversationReply(json.dumps({"questions": [secret]}), "claude", "fable", None, 3, 1)
        self.assertIn("denied:", self.controller.handle("/propose").messages[0])
        self.assertIsNone(self.controller.session["plan"])
        for path in self.controller.store.project_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
