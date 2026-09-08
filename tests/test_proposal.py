import asyncio
import copy
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from camol.app import InteractiveController, validate_envelope
from camol.conversation import ConversationCancelled, ConversationReply
from camol.planning import PlanningError
from camol.proposal import parse_response, request_prompt, validate_candidate, validate_seed
from camol.schema import canonical_digest
from camol.supervisor import SupervisorPaths, send_control
from tests.test_evaluation import AGENT
from tests.test_gate_runtime import v5_plan


def proposed(seed, goal="refine once safely"):
    candidate = copy.deepcopy(seed)
    candidate["run"]["objective"] = goal
    for invariant in candidate["state_model"]["invariants"]:
        invariant["approval_policy"] = "human"
    for gate in candidate["state_model"]["gates"]:
        gate["policy"]["human_approval"] = True
    candidate["tasks"][0]["goal"] += " with an explicit owner-reviewed outcome"
    return candidate


class ProposalContractTests(unittest.TestCase):
    def setUp(self):
        self.seed = validate_seed(v5_plan("model-proposal"))
        self.candidate = proposed(self.seed)

    def test_seed_authority_is_preserved_and_only_human_candidate_is_accepted(self):
        self.assertEqual(validate_candidate(self.seed, self.candidate, goal="refine once safely", owner="owner"), self.candidate)
        self.assertEqual(parse_response('{"questions":["Which additional oracle is approved?"]}', self.seed, goal="g", owner="owner"),
                         {"questions": ["Which additional oracle is approved?"]})
        self.assertLessEqual(len(request_prompt(self.seed, "refine once safely", {"revision": "a" * 40}, "owner")), 12000)

    def test_escalations_weakening_and_unmapped_claims_are_rejected(self):
        changes = [
            lambda plan: plan["run"]["token_policy"].update(max_total_tokens=99999),
            lambda plan: plan["run"].update(max_concurrency=2),
            lambda plan: plan["agents"][0].update(trust_tier="sandboxed"),
            lambda plan: plan["agents"][0]["adapter"].update(argv=["sh", "-c", "anything"]),
            lambda plan: plan["rules"][0].update(enforcement="soft"),
            lambda plan: plan["tasks"][0].update(max_attempts=20),
            lambda plan: plan["tasks"][0].update(acceptance=["just return success"]),
            lambda plan: plan["tasks"][0]["steps"][0]["commands"][0].update(argv=["sh", "-c", "anything"]),
            lambda plan: plan["tasks"][0]["verification"][0].update(argv=["python3", "-c", "pass"]),
            lambda plan: plan["tasks"][0].update(evaluator_assets=["different-oracle"]),
            lambda plan: plan["state_model"]["invariants"][0].update(predicate="anything is correct"),
            lambda plan: plan["state_model"]["invariants"][0].update(approval_policy="preauthorized"),
            lambda plan: plan["state_model"]["gates"][0]["policy"].update(human_approval=False),
            lambda plan: plan["state_model"]["gates"][0].update(evaluators=[]),
            lambda plan: plan["state_model"].update(final_acceptance="automatic"),
            lambda plan: plan.update(schema_version=6),
            lambda plan: plan.update(unknown="must reject"),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                value = copy.deepcopy(self.candidate)
                change(value)
                with self.assertRaises((ValueError, PlanningError)):
                    validate_candidate(self.seed, value, goal="refine once safely", owner="owner")

    def test_json_ambiguity_oversize_fences_and_undeclared_fields_fail_closed(self):
        for text in ('{"questions":["a"],"questions":["b"]}', '{"questions":["a"],"approved":true}',
                     '```json\n{"questions":["a"]}\n```', '{"questions":[]}', '{"questions":[NaN]}',
                     '{"questions":' + '1' * 129 + '}', ' ' * 64001):
            with self.subTest(text=text[:60]), self.assertRaises((ValueError, PlanningError)):
                parse_response(text, self.seed, goal="g", owner="owner")

    def test_bounded_extra_task_uses_existing_oracle_and_redistributes_attempts(self):
        value = copy.deepcopy(self.candidate)
        value["tasks"][0]["max_attempts"] = 1
        extra = copy.deepcopy(value["tasks"][0])
        extra.update(id="review", goal="Review the captured output", depends_on=["change"])
        extra["steps"][0]["commands"] = []
        value["tasks"].append(extra)
        invariant = dict(value["state_model"]["invariants"][0], invariant_id="review-output", owner="owner", scope="task:review")
        obligation = dict(value["state_model"]["obligations"][0], obligation_id="review-accepted", owner="owner", invariant_ids=["review-output"], task_ids=["review"])
        gate = copy.deepcopy(value["state_model"]["gates"][0])
        gate.update(task_id="review", invariant_ids=["review-output"], obligation_ids=["review-accepted"])
        gate["policy"]["policy_id"] = "review-gate"
        gate["evaluators"][0]["invariant_ids"] = ["review-output"]
        value["state_model"]["invariants"].append(invariant)
        value["state_model"]["obligations"].append(obligation)
        value["state_model"]["gates"].append(gate)
        self.assertEqual(len(validate_candidate(self.seed, value, goal="refine once safely", owner="owner")["tasks"]), 2)
        value["tasks"][1]["steps"][0]["commands"] = copy.deepcopy(self.seed["tasks"][0]["steps"][0]["commands"])
        with self.assertRaisesRegex(PlanningError, "repeated"):
            validate_candidate(self.seed, value, goal="refine once safely", owner="owner")

    def test_v6_capacity_and_provider_profiles_cannot_be_rewritten(self):
        from tests.test_capacity import v6_plan
        from tests.test_codex_adapter import codex_profile
        seed = validate_seed(v6_plan("model-capacity"))
        value = proposed(seed)
        self.assertEqual(validate_candidate(seed, value, goal="refine once safely", owner="owner"), value)
        value["run"]["capacity_policy"]["allow_owner_declared_supply"] = True
        with self.assertRaisesRegex(PlanningError, "authority"):
            validate_candidate(seed, value, goal="refine once safely", owner="owner")
        seed = copy.deepcopy(self.seed)
        seed["agents"][0]["adapter"] = {"kind": "codex_cli", "profile": "codex.json", "profile_snapshot": codex_profile(), "timeout_seconds": 30}
        seed = validate_seed(seed)
        value = proposed(seed)
        value["agents"][0]["adapter"]["profile_snapshot"]["max_run_usd_cents"] += 1
        with self.assertRaisesRegex(PlanningError, "authority"):
            validate_candidate(seed, value, goal="refine once safely", owner="owner")


class ProposalUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "agent.py").write_text(AGENT)
        (self.workspace / "README").write_text("clean source fixture\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Proposal Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.seed = v5_plan("model-proposal")
        self.path = self.root / "seed.json"
        self.path.write_text(json.dumps(self.seed))
        self.provider = Mock(return_value=ConversationReply(json.dumps({"runbook": proposed(self.seed)}), "claude", "fable", "fixture-model", 11, 17))
        self.controller = InteractiveController(self.workspace, state_root=self.root / "camol", converse_fn=self.provider)
        self.controller.handle("/model claude:fable")
        self.command = "/propose --from {} refine once safely".format(self.path)

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(self.workspace), *args], check=True, capture_output=True).stdout.decode().strip()

    def tearDown(self):
        state_dir = Path(self.controller.session["state_dir"])
        if SupervisorPaths.under(state_dir).socket.exists():
            asyncio.run(send_control(state_dir, "stop", requested_by=getpass.getuser()))
            deadline = time.monotonic() + 5
            while SupervisorPaths.under(state_dir).socket.exists() and time.monotonic() < deadline:
                time.sleep(.02)
        self.temporary.cleanup()

    def test_one_disclosed_call_unapproved_candidate_and_restart_durable_origin(self):
        notices = []
        def answer(*args, **kwargs):
            self.assertTrue(notices)
            self.assertIn("one planning-only", notices[0])
            self.assertIsNone(kwargs["on_chunk"])
            self.assertTrue(kwargs["no_tools"])
            self.assertIn("FULL_V5_OR_V6_RUNBOOK", args[1])
            self.assertNotIn("clean source fixture", args[1])
            return ConversationReply(json.dumps({"runbook": proposed(self.seed)}), "claude", "fable", "fixture-model", 11, 17)
        self.provider.side_effect = answer
        result = self.controller.handle(self.command, on_chunk=notices.append)
        self.assertIn("MODEL CANDIDATE", result.messages[0])
        self.provider.assert_called_once()
        self.assertIsNone(self.controller.session["approved_digest"])
        self.assertEqual(self.controller.session["plan"]["schema_version"], 3)
        self.assertFalse(Path(self.controller.session["state_dir"]).exists())
        self.assertIn("requires the exact", self.controller.handle("/run").messages[0])
        self.assertIn("exact /plan digest", self.controller.handle("/approve yes").messages[0])
        self.assertIn("recorded_input_tokens=11", self.controller.handle("/usage").messages[0])
        reloaded = InteractiveController(self.workspace, state_root=self.root / "camol", converse_fn=self.provider)
        self.assertIn("seed_assisted_model_proposal", reloaded.handle("/plan").messages[0])
        self.assertEqual(reloaded.session["plan_digest"], self.controller.session["plan_digest"])
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "candidate_ready")
        self.assertEqual(validate_envelope(reloaded.session["plan"]), reloaded.session["plan"])

    def test_questions_preserve_the_prior_plan_and_never_auto_call_again(self):
        self.controller.handle(self.command)
        previous = self.controller.session["plan_digest"]
        self.provider.return_value = ConversationReply('{"questions":["Approve a new integration oracle in the seed?"]}', "claude", "fable", None, 2, 3)
        result = self.controller.handle(self.command)
        self.assertIn("PROPOSAL QUESTIONS", result.messages[0])
        self.assertEqual(self.controller.session["plan_digest"], previous)
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "questions")
        from camol.conversation import planning_history
        self.assertIn("new integration oracle", str(planning_history(self.controller.session["messages"])))

    def test_escalating_reply_is_rejected_with_usage_and_no_candidate(self):
        value = proposed(self.seed)
        value["run"]["token_policy"]["max_total_tokens"] += 1
        self.provider.return_value = ConversationReply(json.dumps({"runbook": value}), "claude", "fable", None, 4, 5)
        result = self.controller.handle(self.command)
        self.assertIn("changed frozen run authority", result.messages[0])
        self.assertIsNone(self.controller.session["plan"])
        self.assertEqual(self.controller.store.planning_calls()[0]["output_tokens"], 5)
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "rejected")

    def test_source_or_seed_drift_during_the_call_rejects_without_discarding_usage(self):
        for change in (lambda: (self.workspace / "README").write_text("modified source\n"),
                       lambda: self.path.write_text(self.path.read_text() + "\n")):
            with self.subTest(change=change):
                self.git("checkout-index", "-f", "README")
                def answer(*args, **kwargs):
                    change()
                    return ConversationReply(json.dumps({"runbook": proposed(self.seed)}), "claude", "fable", None, 1, 2)
                self.provider.side_effect = answer
                result = self.controller.handle(self.command)
                self.assertIn("denied", result.messages[0])
                self.assertIsNone(self.controller.session["plan"])
                self.assertEqual(self.controller.store.planning_calls()[-1]["output_tokens"], 2)
                self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "rejected")

    def test_cancel_records_unknown_usage_and_does_not_publish_partial_proposal(self):
        active = threading.Event()
        def answer(*args, **kwargs):
            active.set()
            kwargs["cancel_event"].wait(2)
            raise ConversationCancelled("fixture cancellation")
        self.provider.side_effect = answer
        results = []
        thread = threading.Thread(target=lambda: results.append(self.controller.handle(self.command)))
        thread.start()
        self.assertTrue(active.wait(2))
        self.controller.handle("/cancel")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.controller.session["plan"])
        self.assertEqual(self.controller.store.planning_calls()[0]["status"], "cancelled")
        self.assertIsNone(self.controller.store.planning_calls()[0]["input_tokens"])
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "cancelled")

    def test_missing_or_invalid_seed_and_manual_mode_never_call_provider(self):
        self.path.write_text('{"schema_version":5,"schema_version":6}')
        self.assertIn("duplicate", self.controller.handle(self.command).messages[0])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIn("regular", self.controller.handle(self.command).messages[0])
        self.controller.handle("/model manual")
        self.assertIn("explicitly selected", self.controller.handle(self.command).messages[0])
        self.provider.assert_not_called()

    def test_codex_proposal_is_not_faked_by_mock_provider(self):
        self.controller.handle("/model codex:gpt-5.4")
        result = self.controller.handle(self.command)
        self.assertIn("all-tools-off", result.messages[0])
        self.provider.assert_not_called()
        self.assertEqual(self.controller.store.planning_calls(), [])

    def test_postproposal_assume_unchanged_edit_cannot_be_approved(self):
        self.controller.handle(self.command)
        self.git("update-index", "--assume-unchanged", "README")
        (self.workspace / "README").write_text("same revision but changed source\n")
        result = self.controller.handle("/approve " + self.controller.session["plan_digest"])
        self.assertIn("denied", result.messages[0])
        self.assertIsNone(self.controller.session["approved_digest"])

    def test_exact_source_pin_is_passed_through_detached_launch(self):
        self.controller.spawn_fn = Mock(return_value={"started": True, "pid": 42})
        self.controller.handle(self.command)
        self.controller.handle("/approve " + self.controller.session["plan_digest"])
        result = self.controller.handle("/run")
        self.assertIn("detached", result.messages[-1])
        self.assertEqual(self.controller.spawn_fn.call_args.kwargs["expected_source"], self.controller.session["plan"]["source"])

    def test_changed_source_between_client_check_and_daemon_launch_is_refused(self):
        from camol.supervisor import spawn_supervisor
        self.controller.handle(self.command)
        self.controller.handle("/approve " + self.controller.session["plan_digest"])
        def changed(*args, **kwargs):
            (self.workspace / "README").write_text("racing clean committed checkout\n")
            self.git("add", "README")
            self.git("commit", "-qm", "source changed after UI check")
            return spawn_supervisor(*args, **kwargs)
        self.controller.spawn_fn = changed
        result = self.controller.handle("/run")
        self.assertIn("source changed since human approval", result.messages[0])
        self.assertEqual(self.controller.session["status"], "approved")
        self.assertFalse(SupervisorPaths.under(Path(self.controller.session["state_dir"])).socket.exists())

    def test_reattach_rejects_same_runbook_with_different_source_binding(self):
        from camol.runbook import runbook_digest
        self.controller.handle(self.command)
        self.controller.handle("/approve " + self.controller.session["plan_digest"])
        path = SupervisorPaths.under(Path(self.controller.session["state_dir"])).socket
        path.parent.mkdir(parents=True)
        path.touch()
        self.controller._control = Mock(side_effect=[
            {"plan_digest": runbook_digest(self.controller.session["plan"]["runbook"]), "source_binding": None},
            {"pid": 42, "run": {"status": "running"}},
        ])
        try:
            self.assertIn("source binding differs", self.controller.handle("/run").messages[0])
        finally:
            path.unlink()

    def test_observed_tokens_survive_prohibited_local_tool_response(self):
        from camol.conversation import ConversationError
        error = ConversationError("prohibited tool calls")
        error.reply = ConversationReply("", "local", "fixture", "fixture", 2, 3)
        self.provider.side_effect = error
        self.controller.handle("/model local:fixture")
        self.assertIn("denied", self.controller.handle(self.command).messages[0])
        receipt = self.controller.store.planning_calls()[-1]
        self.assertEqual(receipt["output_tokens"], 3)
        self.assertEqual(receipt["tool_policy"], "none")
        self.assertIsNone(receipt["provider_request_count"])
        self.assertIsNone(self.controller.session["plan"])

    def test_controller_uses_real_conversation_no_tools_path_with_mocked_cli(self):
        from functools import partial
        from shutil import which
        from camol.conversation import converse
        invocations = []
        def cli(argv, **kwargs):
            invocations.append(argv)
            if "--help" in argv:
                output = "--tools --safe-mode --strict-mcp-config --mcp-config --setting-sources --disable-slash-commands --permission-prompts --max-turns"
            else:
                self.assertEqual(argv[argv.index("--tools") + 1], "")
                output = json.dumps({"result": json.dumps({"runbook": proposed(self.seed)}),
                                     "usage": {"input_tokens": 4, "output_tokens": 7}})
            return subprocess.CompletedProcess(argv, 0, stdout=output.encode(), stderr=b"")
        self.controller.converse_fn = partial(converse, runner=cli)
        with patch("camol.conversation.shutil.which", side_effect=lambda name, **kwargs: "/opt/camol-test/claude" if name == "claude" else which(name, **kwargs)):
            result = self.controller.handle(self.command)
        self.assertIn("MODEL CANDIDATE", result.messages[0])
        self.assertEqual(sum("-p" in argv for argv in invocations), 1)
        self.assertEqual(len(invocations), 2)
        self.assertEqual(self.controller.store.planning_calls()[0]["output_tokens"], 7)
        self.assertIsNone(self.controller.session["approved_digest"])

    def test_malformed_or_secret_response_is_rejected_with_private_redacted_trace(self):
        secret = "sk-live-abcdefghijklmnopqrstuvwxyz123456"
        for text in ('{"questions":["first"],"questions":["second"]}', '{"questions":["' + secret + '"]}'):
            self.provider.return_value = ConversationReply(text, "claude", "fable", None, 1, 2)
            response = self.controller.handle(self.command)
            self.assertIn("denied", response.messages[0])
            self.assertIsNone(self.controller.session["plan"])
            self.assertEqual(self.controller.store.planning_calls()[-1]["output_tokens"], 2)
        self.assertNotIn(secret, self.controller.store.transcript_path.read_text())
        self.assertNotIn(secret, self.controller.store.proposal_events_path.read_text())
        self.assertEqual(self.controller.store.proposal_events_path.stat().st_mode & 0o777, 0o600)

    def test_mocked_model_to_real_worker_gates_and_final_acceptance(self):
        response = self.controller.handle(self.command)
        self.assertIn("MODEL CANDIDATE", response.messages[0])
        approved = self.controller.handle("/approve " + self.controller.session["plan_digest"])
        self.assertIn("Approved", approved.messages[0])
        started = self.controller.handle("/run")
        self.assertIn("detached", started.messages[-1])
        remote_plan = self.controller._control("plan")
        self.assertEqual(remote_plan["source_binding"]["source"], self.controller.session["plan"]["source"])
        deadline, approvals = time.monotonic() + 30, 0
        while time.monotonic() < deadline:
            snapshot = self.controller._control("acceptance")
            if snapshot["status"] == "awaiting_acceptance":
                break
            self.assertNotEqual(snapshot["status"], "blocked", snapshot)
            for task_id, gate in snapshot["pending_gates"].items():
                outcome = self.controller.handle("/gate {} {}".format(task_id, gate["assessment_digest"]))
                self.assertIn("State gate approved", outcome.messages[0])
                approvals += 1
            time.sleep(.05)
        self.assertEqual(snapshot["status"], "awaiting_acceptance", snapshot)
        self.assertGreaterEqual(approvals, 2)
        self.assertIn("Final outcome accepted", self.controller.handle("/accept " + snapshot["acceptance"]["outcome_digest"]).messages[0])
        self.assertEqual(self.controller._control("status")["run"]["status"], "completed")
        self.provider.assert_called_once()


if __name__ == "__main__":
    unittest.main()
