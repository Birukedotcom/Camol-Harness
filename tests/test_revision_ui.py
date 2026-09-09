"""Stopped-owner interactive revisions use the real kernel, never a launcher."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.api import Harness
from camol.app import validate_envelope
from camol.artifacts import RunArchive
from camol.debug_execution import source_identity
from camol.revision_ui import RevisionUI, render_review, validate_review, validate_revision_envelope
from camol.revisions import RevisionError
from camol.orchestrator import Orchestrator
from camol.schema import canonical_digest
from camol.session import SessionStore
from camol.source_binding import SourceBindingError
from camol.supervisor import SupervisorError
from tests import test_evaluation as fixture
from tests.test_gate_runtime import v5_plan


class RevisionUITests(unittest.TestCase):
    setUp = fixture.EvaluationLoopTests.setUp
    tearDown = fixture.EvaluationLoopTests.tearDown

    def prepare(self, *, execute=False, bound=True):
        self.sessions = SessionStore(self.source, root=self.root / "interactive")
        session = self.sessions.create()
        plan = validate_envelope(dict(schema="camol.product_plan", schema_version=3,
            proposal=None, run_id="original", runbook=v5_plan("original"),
            execution_status="ready", execution_limitation="Fixture source-bound process plan",
            source=source_identity(self.source), origin=dict(kind="seed_assisted_model_proposal",
                seed_path=str(self.source.resolve() / "fixture.json"),
                **{key: canonical_digest({}) for key in ("seed_digest", "seed_bytes_digest", "request_digest", "response_digest")},
                planning_call_id="fixture-call")))
        self.state = Path(session["state_dir"])
        session = self.sessions.update(session, plan=plan, plan_digest=canonical_digest(plan),
            approved_digest=canonical_digest(plan), run_id="original", status="approved", selected_box="builder", event_cursor=12)
        with Harness(self.source, self.state) as harness:
            initial = harness.prepare(plan["runbook"])
            if bound:
                harness.orchestrator.bind_source("original", plan["source"])
            harness.approve(by="human-owner", digest=initial["plan_digest"])
            if execute:
                final = harness.run()
                self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
        self.ui = RevisionUI(self.sessions)
        return session

    def propose(self, session, identifier="successor"):
        return self.ui.propose(session, v5_plan(identifier), reason="Reviewed contract amendment", owner="human-owner", effect_reruns=[])

    def test_review_and_apply_are_stopped_exact_and_do_not_execute(self):
        session = self.prepare()
        with patch("camol.supervisor.spawn_supervisor", side_effect=AssertionError("must not spawn")), patch("camol.runner.HarnessRunner.run_until_terminal", side_effect=AssertionError("must not execute")):
            review = self.propose(session)
            self.assertEqual(self.sessions.load(), session)
            self.assertEqual(self.ui.inspect(session), review)
            text = render_review(review)
            self.assertIn("Exact successor runbook", text)
            self.assertIn("/revise apply " + review["review_digest"], text)
            self.assertIn("Inherited charges/unknown usage", text)
            with self.assertRaisesRegex(RevisionError, "exact current"):
                self.ui.apply(session, "sha256:" + "0" * 64, owner="human-owner")
            successor = self.ui.apply(session, review["review_digest"], owner="human-owner")
        self.assertEqual(successor["state_dir"], session["state_dir"])
        self.assertEqual(successor["session_id"], session["session_id"])
        self.assertEqual(successor["run_id"], "successor")
        self.assertEqual(successor["plan"]["schema_version"], 5)
        self.assertEqual(successor["plan"]["source"], session["plan"]["source"])
        self.assertEqual(successor["status"], "approved")
        self.assertEqual(successor["event_cursor"], 0)
        self.assertIsNone(successor["selected_box"])
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state()["status"], "ready")
            self.assertEqual(harness.state()["tasks"]["change"]["attempts"], 0)
            self.assertFalse(any(event["type"] == "TASK_LEASED" for event in harness.events()))
            self.assertEqual(harness.orchestrator.state("original")["status"], "superseded")

    def test_accepted_code_usage_and_lineage_survive_explicit_successor_execution(self):
        session = self.prepare(execute=True)
        with Harness(self.source, self.state) as harness:
            parent = harness.state()
        review = self.propose(session)
        self.assertEqual(review["proposal"]["integration_head"], parent["integration_head"])
        self.assertEqual(review["proposal"]["inherited_usage"]["accounted_tokens"], 300)
        successor = self.ui.apply(session, review["review_digest"], owner="human-owner")
        with Harness(self.source, self.state) as harness:
            ready = harness.state()
            self.assertEqual(ready["gate_assessments"], {})
            self.assertEqual(ready["revision"]["base_revision"], parent["integration_head"])
            final = harness.run()
            self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
            self.assertEqual(final["tasks"]["change"]["attempts"], 2)
            archive = self.root / "export"
            harness.export(archive)
            self.assertEqual(RunArchive.replay(archive), final)
        third = self.propose(successor, "third")
        third_session = self.ui.apply(successor, third["review_digest"], owner="human-owner")
        self.assertEqual(third_session["run_id"], "third")
        self.assertEqual(third_session["state_dir"], session["state_dir"])

    def test_crash_after_kernel_commit_recovers_exact_session_without_reapply(self):
        session = self.prepare()
        review = self.propose(session)
        with patch.object(self.sessions, "update", side_effect=OSError("simulated session write crash")):
            with self.assertRaisesRegex(OSError, "write crash"):
                self.ui.apply(session, review["review_digest"], owner="human-owner")
        self.assertEqual(self.sessions.load(), session)
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.orchestrator.state("original")["status"], "superseded")
            before = harness.store.read("original"), harness.store.read("successor")
        with patch.object(Harness, "apply_revision", side_effect=AssertionError("recovery must not reapply")):
            result = RevisionUI(self.sessions).recover(session, owner="human-owner")
        self.assertEqual(result["run_id"], "successor")
        with Harness(self.source, self.state) as harness:
            self.assertEqual((harness.store.read("original"), harness.store.read("successor")), before)
        with self.assertRaisesRegex(RevisionError, "advanced"):
            self.ui.apply(session, review["review_digest"], owner="human-owner")

    def test_failure_before_kernel_commit_does_not_seal_or_approve_successor(self):
        session = self.prepare()
        review = self.propose(session)
        with patch.object(Harness, "apply_revision", side_effect=OSError("simulated transaction failure")):
            with self.assertRaises(OSError):
                self.ui.apply(session, review["review_digest"], owner="human-owner")
        self.assertEqual(self.sessions.load(), session)
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state()["status"], "ready")
            self.assertFalse(harness.store.has_run("successor"))
        with self.assertRaisesRegex(RevisionError, "not committed"):
            self.ui.recover(session, owner="human-owner")

    def test_changed_review_owner_source_or_session_cannot_apply(self):
        session = self.prepare()
        first = self.propose(session, "first")
        second = self.propose(session, "second")
        for digest, owner in ((first["review_digest"], "human-owner"), (second["review_digest"], "builder"), (second["review_digest"], "another-owner")):
            with self.subTest(owner=owner, digest=digest), self.assertRaises(RevisionError):
                self.ui.apply(session, digest, owner=owner)
        (self.source / "agent.py").write_text("unapproved changes\n")
        with self.assertRaises(SourceBindingError):
            self.ui.apply(session, second["review_digest"], owner="human-owner")
        self.assertEqual(self.sessions.load(), session)
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state()["run_id"], "original")
            self.assertFalse(harness.store.has_run("second"))

    def test_live_owner_and_active_process_marker_deny_revision(self):
        session = self.prepare()
        with Harness(self.source, self.state):
            with self.assertRaisesRegex(SupervisorError, "owns"):
                self.propose(session)
        record = self.state / "packets" / "orphan.invocation.json"
        record.parent.mkdir()
        record.write_text(json.dumps(dict(schema="camol.process_invocation", schema_version=1, state="active",
            owner_pid=999998, pid=999999, pgid=999999, process_started="Mon Sep 7 01:02:03 2026",
            cwd=str(self.source), argv_digest=canonical_digest(["fixture"]), policy_digest=canonical_digest({}),
            started_at="2026-09-07T00:00:00+00:00", finished_at=None, exit_code=None)))
        with self.assertRaisesRegex(RevisionError, "active or orphan"):
            self.propose(session)
        self.assertEqual(json.loads(record.read_text())["state"], "active")

    def test_legacy_and_missing_full_source_authority_are_not_fabricated(self):
        session = self.prepare(bound=False)
        with self.assertRaisesRegex(RevisionError, "exact approved source"):
            self.propose(session)
        legacy = copy.deepcopy(session["plan"])
        legacy["schema_version"] = 2
        legacy.pop("origin")
        legacy["source"] = {key: legacy["source"][key] for key in ("workspace", "revision")}
        session = self.sessions.update(session, plan=legacy, plan_digest=canonical_digest(legacy), approved_digest=canonical_digest(legacy))
        with self.assertRaisesRegex(RevisionError, "legacy unbound"):
            self.propose(session)

    def test_review_tampering_and_full_risk_delta_are_validated(self):
        session = self.prepare()
        successor = v5_plan("successor")
        successor["run"]["objective"] = "Human amended objective"
        successor["tasks"][0]["goal"] = "Human amended task goal"
        review = self.ui.propose(session, successor, reason="Exact amendment", owner="human-owner", effect_reruns=[])
        self.assertIn("run", review["risk_delta"]["global_contract_changes"])
        self.assertIn("change", review["risk_delta"]["task_contract_changes"])
        for key, value in (("risk_delta", {}), ("successor_product_digest", canonical_digest({})), ("workspace", str(self.root))):
            changed = copy.deepcopy(review)
            changed[key] = value
            changed["review_digest"] = canonical_digest({key: value for key, value in changed.items() if key != "review_digest"})
            with self.subTest(key=key), self.assertRaises(RevisionError):
                validate_review(changed)
        path = self.ui._path(review["review_digest"])
        payload = json.loads(path.read_text())
        payload["owner"] = "another-owner"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(RevisionError, "digest"):
            self.ui.apply(session, review["review_digest"], owner="human-owner")

    def test_changed_kernel_state_invalidates_review_before_sealing(self):
        session = self.prepare()
        review = self.propose(session)
        with Harness(self.source, self.state) as harness:
            harness.orchestrator._emit("original", "RUN_STARTED", {}, actor_id="orchestrator")
            before = harness.events()
        with self.assertRaisesRegex(RevisionError, "advanced"):
            self.ui.apply(session, review["review_digest"], owner="human-owner")
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.events(), before)
            self.assertFalse(harness.store.has_run("successor"))

    def test_unsettled_kernel_surfaces_are_never_discarded_by_ui(self):
        session = self.prepare()
        with Harness(self.source, self.state) as harness:
            state = harness.state()
        cases = [
            {"global_capacity": {"held": {"status": "suspect"}}},
            {"effects": {"pending": {"state": "EFFECT_UNKNOWN"}}},
            {"watchers": {"watch": {"status": "waiting"}}},
            {"gate_assessments": {"candidate:integration": {"phase": "integration", "candidate_id": "candidate"}}},
            {"debug_cases": {"case": {"status": "open"}}},
            {"tasks": {"change": dict(state["tasks"]["change"], gate_wait={"assessment_digest": canonical_digest({})})}},
        ]
        for changed in cases:
            image = copy.deepcopy(state)
            image.update(changed)
            with self.subTest(surface=next(iter(changed))), patch.object(Orchestrator, "state", return_value=image), self.assertRaises(RevisionError):
                self.propose(session)
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state(), state)

    def test_oversized_session_handoff_is_rejected_before_kernel_apply(self):
        session = self.prepare()
        review = self.propose(session)
        with patch("camol.revision_ui.MAX_SESSION_BYTES", 100), self.assertRaisesRegex(RevisionError, "capacity"):
            self.ui.apply(session, review["review_digest"], owner="human-owner")
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state()["status"], "ready")
            self.assertFalse(harness.store.has_run("successor"))

    def test_recovery_refuses_missing_review_without_replaying_kernel(self):
        session = self.prepare()
        review = self.propose(session)
        with patch.object(self.sessions, "update", side_effect=OSError("session crash")), self.assertRaises(OSError):
            self.ui.apply(session, review["review_digest"], owner="human-owner")
        self.ui._path(review["review_digest"]).unlink()
        with patch.object(Harness, "apply_revision", side_effect=AssertionError("must not reapply")), self.assertRaises(FileNotFoundError):
            self.ui.recover(session, owner="human-owner")
        with Harness(self.source, self.state) as harness:
            self.assertEqual(harness.state()["status"], "ready")
            self.assertEqual(harness.orchestrator.state("original")["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
