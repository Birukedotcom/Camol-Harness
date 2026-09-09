import copy
import json
import shlex
import time
import unittest
from unittest.mock import Mock, patch

from camol.api import Harness
from camol.conversation import ConversationReply
from camol.revision_ui import RevisionUI
from tests import test_revision_commands, test_proposal
from tests.test_gate_runtime import v5_plan


class DelegationProposalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_revision_commands.RevisionCommandTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.controller.session = self.controller.store.update(self.controller.session, model="claude:fable")
        self.seed = json.loads(self.fixture.path.read_text())
        self.goal = "refine once safely"
        self.candidate = test_proposal.proposed(self.seed, self.goal)
        self.provider = Mock(side_effect=lambda *a, **k: self.reply())
        self.controller.converse_fn = self.provider
        self.command = "/delegate --propose --from {} --reason 'Reviewed follow-up' --goal {}".format(
            shlex.quote(str(self.fixture.path)), shlex.quote(self.goal))

    def reply(self, value=None):
        return ConversationReply(json.dumps(value or {"runbook": self.candidate}), "claude", "fable", "fixture-model", 11, 17)

    def events(self):
        with Harness(self.fixture.fixture.source, self.fixture.fixture.state) as harness:
            return harness.store.read("original")

    def review(self):
        response = self.controller.handle(self.command)
        self.assertIn("MODEL DELEGATION REVIEW", response.messages[0])
        self.assertTrue(response.focus_orchestrator)
        return RevisionUI(self.controller.store).inspect(self.controller.session)

    def test_model_review_preserves_parent_and_records_exact_provenance(self):
        before = copy.deepcopy(self.controller.session)
        review = self.review()
        for key in ("plan", "plan_digest", "approved_digest", "run_id", "state_dir"):
            self.assertEqual(self.controller.session[key], before[key])
        self.provider.assert_called_once()
        self.assertTrue(self.provider.call_args.kwargs["no_tools"])
        self.assertIn("REVISION_CONTEXT", self.provider.call_args.args[1])
        self.assertIn('"source_run_id":"original"', self.provider.call_args.args[1])
        call = self.controller.store.planning_calls()[-1]
        event = self.controller.store.proposal_events()[-1]
        self.assertEqual(call["request_kind"], "delegation_proposal")
        self.assertEqual(call["input_tokens"], 11)
        self.assertEqual(event["outcome"], "revision_review_ready")
        self.assertEqual(event["origin"]["review_digest"], review["review_digest"])
        self.assertEqual(event["origin"]["planning_call_id"], call["call_id"])
        self.assertEqual(review["proposal"]["new_runbook"]["run"]["objective"], self.goal)
        self.assertTrue(all(gate["policy"]["human_approval"] for gate in review["proposal"]["new_runbook"]["state_model"]["gates"]))
        self.assertIn("denied", self.controller.handle("/revise apply yes").messages[0])
        self.controller.spawn_fn.assert_not_called()
        self.assertFalse(any(event["type"] == "RUN_SUPERSEDED" for event in self.events()))

    def test_questions_leave_parent_and_kernel_unchanged(self):
        events = self.events()
        self.provider.side_effect = lambda *a, **k: self.reply({"questions": ["Which additional evaluator is authorized?"]})
        response = self.controller.handle(self.command)
        self.assertIn("QUESTIONS", response.messages[0])
        self.assertEqual(self.events(), events)
        self.assertEqual(self.controller.session["run_id"], "original")
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "questions")

    def test_live_owner_is_denied_before_model_or_review(self):
        with Harness(self.fixture.fixture.source, self.fixture.fixture.state):
            response = self.controller.handle(self.command)
        self.assertIn("denied", response.messages[0])
        self.provider.assert_not_called()

    def test_cancelled_preflight_never_calls_provider(self):
        original = RevisionUI._planning_context_locked
        def cancel(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            self.controller._cancel_event.set()
            return result
        events = self.events()
        with patch.object(RevisionUI, "_planning_context_locked", cancel):
            self.assertIn("cancelled", self.controller.handle(self.command).messages[0])
        self.provider.assert_not_called()
        self.assertEqual(self.events(), events)

    def test_changed_source_checkout_never_calls_provider(self):
        target = self.fixture.fixture.source / "agent.py"
        target.write_text(target.read_text() + "\n# owner change\n")
        self.assertIn("denied", self.controller.handle(self.command).messages[0])
        self.provider.assert_not_called()

    def test_invalid_effect_policy_is_denied_before_provider(self):
        effects = self.fixture.fixture.root / "effects.json"
        effects.write_text('{"not":"an array"}')
        self.assertIn("denied", self.controller.handle(self.command + " --effects " + shlex.quote(str(effects))).messages[0])
        self.provider.assert_not_called()

    def test_invalid_options_and_same_run_seed_never_call_model(self):
        for command in ("/delegate --propose", self.command + " --goal duplicate",
                self.command + " --effects", self.command + " --unknown yes"):
            self.assertIn("denied", self.controller.handle(command).messages[0])
        self.seed["run"]["id"] = "original"
        self.fixture.path.write_text(json.dumps(self.seed))
        self.assertIn("denied", self.controller.handle(self.command).messages[0])
        self.provider.assert_not_called()

    def test_source_advancement_during_model_call_refuses_stale_review(self):
        def answer(*args, **kwargs):
            with Harness(self.fixture.fixture.source, self.fixture.fixture.state) as harness:
                harness.propose_revision(v5_plan("concurrent-successor"), reason="Concurrent owner proposal")
            return self.reply()
        self.provider.side_effect = answer
        response = self.controller.handle(self.command)
        self.assertIn("source advanced", response.messages[0])
        proposals = [item for item in self.events() if item["type"] == "REVISION_PROPOSED"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["payload"]["destination_run_id"], "concurrent-successor")
        self.assertEqual(self.controller.session["run_id"], "original")
        self.assertEqual(self.controller.store.planning_calls()[-1]["input_tokens"], 11)

    def test_changed_seed_and_increased_authority_are_rejected_usage_retained(self):
        events = self.events()
        def answer(*args, **kwargs):
            self.fixture.path.write_text(json.dumps(self.seed) + "\n")
            return self.reply()
        self.provider.side_effect = answer
        self.assertIn("seed changed", self.controller.handle(self.command).messages[0])
        self.provider.side_effect = lambda *a, **k: self.reply()
        self.candidate["run"]["max_concurrency"] += 1
        self.assertIn("denied", self.controller.handle(self.command).messages[0])
        self.assertEqual(self.events(), events)
        self.assertEqual(len(self.controller.store.planning_calls()), 2)
        self.controller.spawn_fn.assert_not_called()

    def test_cancellation_does_not_publish_review_or_lose_usage(self):
        events = self.events()
        def answer(*args, **kwargs):
            self.controller._cancel_event.set()
            return self.reply()
        self.provider.side_effect = answer
        response = self.controller.handle(self.command)
        self.assertIn("denied", response.messages[0])
        self.assertEqual(self.events(), events)
        call = self.controller.store.planning_calls()[-1]
        self.assertEqual(call["status"], "cancelled")
        self.assertEqual(call["input_tokens"], 11)
        self.assertEqual(self.controller.store.proposal_events()[-1]["outcome"], "cancelled")

    def test_reopened_client_recovers_exact_review_not_a_new_model_call(self):
        from camol.app import InteractiveController
        review = self.review()
        restarted = InteractiveController(self.fixture.fixture.source,
            state_root=self.fixture.fixture.root / "interactive", converse_fn=Mock(side_effect=AssertionError("must not call model")))
        response = restarted.handle("/revise")
        self.assertIn(review["review_digest"], response.messages[0])
        self.assertEqual(restarted.session["run_id"], "original")
        restarted.spawn_fn = Mock()
        applied = restarted.handle("/revise apply " + review["review_digest"])
        self.assertNotIn("denied", applied.messages[0])
        self.assertEqual(restarted.session["run_id"], "successor")
        restarted.spawn_fn.assert_not_called()
        restarted.converse_fn.assert_not_called()

    def test_journal_failure_after_publication_leaves_recoverable_unapproved_review(self):
        original = self.controller.store.append_proposal_event
        def write(event):
            if event["type"] == "PROPOSAL_FINISHED":
                raise OSError("fixture final-journal failure")
            original(event)
        with patch.object(self.controller.store, "append_proposal_event", write):
            response = self.controller.handle(self.command)
        self.assertIn("denied", response.messages[0])
        review = RevisionUI(self.controller.store).inspect(self.controller.session)
        self.assertEqual(self.controller.session["run_id"], "original")
        self.assertIn(review["review_digest"], self.controller.handle("/revise").messages[0])
        self.provider.assert_called_once()
        self.controller.spawn_fn.assert_not_called()
        self.assertEqual(self.controller.store.planning_calls()[-1]["input_tokens"], 11)
        self.assertFalse(any(event["type"] == "RUN_SUPERSEDED" for event in self.events()))

    def test_real_detached_successor_requires_exact_revision_task_and_final_gates(self):
        from camol.supervisor import spawn_supervisor
        from camol.debug_execution import source_identity
        from tests.test_product_e2e import ProductFlowTests
        from camol.store import ReadOnlyEventStore
        from camol.state import project
        baseline = source_identity(self.fixture.fixture.source)
        review = self.review()
        self.controller.handle("/revise apply " + review["review_digest"])
        self.assertEqual(self.controller.session["run_id"], "successor")
        self.controller.spawn_fn.assert_not_called()
        self.controller.spawn_fn = spawn_supervisor
        approvals = set()
        try:
            response = self.controller.handle("/run")
            self.assertIn("detached", response.messages[-1])
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status = self.controller._control("status")
                if status["run"]["status"] == "awaiting_acceptance":
                    break
                waiting = self.controller._control("acceptance")["pending_gates"].get("change")
                if waiting and waiting["assessment_digest"] not in approvals:
                    digest = waiting["assessment_digest"]
                    self.assertNotIn(digest, approvals)
                    self.assertIn("denied", self.controller.handle("/gate change sha256:" + "0" * 64).messages[0])
                    self.assertNotIn("denied", self.controller.handle("/gate change " + digest).messages[0])
                    approvals.add(digest)
                time.sleep(.05)
            self.assertEqual(status["run"]["status"], "awaiting_acceptance", status)
            self.assertTrue(approvals)
            acceptance = self.controller._control("acceptance")["acceptance"]
            self.assertNotIn("denied", self.controller.handle("/accept " + acceptance["outcome_digest"]).messages[0])
            self.assertEqual(self.controller._control("status")["run"]["status"], "completed")
            self.assertEqual(source_identity(self.fixture.fixture.source), baseline)
        finally:
            ProductFlowTests.stop_fixture(self, self.fixture.fixture.state)
        store = ReadOnlyEventStore(self.fixture.fixture.state / "camol.sqlite3")
        try:
            self.assertEqual(project(store.read("original"))["status"], "superseded")
            self.assertEqual(project(store.read("successor"))["status"], "completed")
        finally:
            store.close()


class DelegationProposalTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_revision_review_is_visible_from_box_and_preserves_next_draft(self):
        from camol.tui import CamolApp, PromptArea
        from tests.test_tui import TuiTests
        fixture = DelegationProposalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(110, 32)) as pilot:
            app._render_box("builder", "events", "previous box view")
            app.query_one("#prompt", PromptArea).load_text(fixture.command)
            await pilot.press("enter")
            def reviewed():
                return not fixture.controller._command_lock.locked() and "MODEL DELEGATION REVIEW" in "\n".join(
                    line.text for line in app.query_one("#transcript").lines)
            await TuiTests.wait_for_ui(self, pilot, reviewed, "visible model revision", timeout=5)
            self.assertFalse(app.in_box)
            app.query_one("#prompt", PromptArea).load_text("unsent next question")
            await app._refresh_fleet()
            self.assertEqual(app.query_one("#prompt", PromptArea).text, "unsent next question")
            self.assertEqual(fixture.controller.session["run_id"], "original")
            fixture.provider.assert_called_once()
            fixture.controller.spawn_fn.assert_not_called()
