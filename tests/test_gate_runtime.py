import asyncio
import copy
import unittest

from camol.gates import GateError, GatePolicy, Invariant, Obligation
from camol.gate_runtime import acceptance_digest
from camol.orchestrator import Orchestrator
from camol.runbook import RunbookError, runbook_digest, validate_runbook
from camol.runner import HarnessRunner
from camol.state import project
from camol.store import SQLiteEventStore
from tests import test_evaluation as evaluation_fixture


def v5_plan(identifier="v5-output", *, human=False):
    plan = evaluation_fixture.runbook(identifier, "refine once")
    plan["schema_version"] = 5
    invariant = Invariant("correct-output", 1, "human-owner", "task:change", "eventually",
                          "output.txt contains exactly good followed by a newline", (), ("output.txt bytes",),
                          "normal", ("wrong-output mutation",), ("test_result",), True,
                          "human" if human else "preauthorized")
    obligation = Obligation("output-accepted", "human-owner", "ACCEPTED", ("correct-output",), ("change",))
    plan["state_model"] = dict(invariants=[invariant.to_dict()], obligations=[obligation.to_dict()],
                                gates=[dict(task_id="change", policy=GatePolicy.compile("output-gate", "basic").to_dict(),
                                            invariant_ids=["correct-output"], obligation_ids=["output-accepted"],
                                            evaluators=[dict(verification_index=0, family="deterministic", invariant_ids=["correct-output"])])],
                                final_acceptance="human")
    return validate_runbook(plan)


class V5SchemaTests(unittest.TestCase):
    def test_v5_roundtrip_and_old_digest_remain_stable(self):
        old = evaluation_fixture.runbook("old", "refine once")
        digest = runbook_digest(old)
        plan = v5_plan()
        self.assertEqual(validate_runbook(plan), plan)
        self.assertEqual(runbook_digest(validate_runbook(old)), digest)
        self.assertNotEqual(runbook_digest(plan), digest)

    def test_unknown_fields_unmapped_checks_and_weakened_global_invariant_rejected(self):
        plan = v5_plan()
        cases = []
        item = copy.deepcopy(plan)
        item["state_model"]["invariants"][0]["extra"] = True
        cases.append(item)
        item = copy.deepcopy(plan)
        item["state_model"]["gates"][0]["evaluators"] = []
        cases.append(item)
        item = copy.deepcopy(plan)
        item["state_model"]["gates"][0]["evaluators"][0]["verification_index"] = 99
        cases.append(item)
        item = copy.deepcopy(plan)
        item["state_model"]["gates"][0]["policy"] = GatePolicy.compile("backed", "backed").to_dict()
        cases.append(item)
        item = copy.deepcopy(plan)
        item["state_model"]["final_acceptance"] = "automatic"
        cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(RunbookError):
                validate_runbook(item)


class V5RuntimeTests(unittest.TestCase):
    setUp = evaluation_fixture.EvaluationLoopTests.setUp
    tearDown = evaluation_fixture.EvaluationLoopTests.tearDown

    def execute(self, plan):
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(plan)
        orchestrator.approve_plan(state["run_id"], "human-owner", state["plan_digest"])
        final = asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal(state["run_id"]))
        return orchestrator, final, store

    def test_actual_candidate_and_integration_gates_precede_final_human_acceptance(self):
        orchestrator, final, store = self.execute(v5_plan())
        self.assertEqual(final["status"], "awaiting_acceptance", final.get("terminal"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 2)
        self.assertEqual(sum(item["assessment"]["status"] == "GREEN" for item in final["gate_assessments"].values()), 2)
        self.assertEqual(sum(item["assessment"]["status"] == "BLOCKED" for item in final["gate_assessments"].values()), 1)
        events = store.read(final["run_id"])
        gate_positions = [index for index, event in enumerate(events) if event["type"] == "GATE_ASSESSED"]
        integrate = next(index for index, event in enumerate(events) if event["type"] == "INTEGRATION_ACCEPTED")
        self.assertEqual(len(gate_positions), 3)
        self.assertLess(max(gate_positions), integrate)
        before = len(events)
        with self.assertRaisesRegex(GateError, "human owner"):
            orchestrator.accept_run(final["run_id"], "builder", acceptance_digest(final))
        with self.assertRaisesRegex(GateError, "stale"):
            orchestrator.accept_run(final["run_id"], "human-owner", "sha256:" + "0" * 64)
        self.assertEqual(len(store.read(final["run_id"])), before)
        orchestrator.accept_run(final["run_id"], "human-owner", acceptance_digest(final))
        self.assertEqual(orchestrator.state(final["run_id"])["status"], "completed")
        self.assertEqual(project(store.read(final["run_id"]))["status"], "completed")

    def test_replay_rejects_removed_gate_and_forged_verdict(self):
        _, final, store = self.execute(v5_plan("replay-gates"))
        events = store.read(final["run_id"])
        with self.assertRaisesRegex(GateError, "missing"):
            project([event for event in events if event["type"] != "GATE_ASSESSED"])
        forged = copy.deepcopy(events)
        event = next(event for event in forged if event["type"] == "GATE_ASSESSED" and event["payload"]["assessment"]["status"] == "GREEN")
        event["payload"]["assessment"]["status"] = "BLOCKED"
        with self.assertRaisesRegex(GateError, "reproduced"):
            project(forged)

    def test_human_task_gate_pauses_without_new_provider_attempt(self):
        orchestrator, state, store = self.execute(v5_plan("human-task-gate", human=True))
        waiting = state["tasks"]["change"].get("gate_wait")
        self.assertIsNotNone(waiting)
        self.assertEqual(state["tasks"]["change"]["status"], "verifying")
        before_attempts = state["tasks"]["change"]["attempts"]
        before_turns = state["tasks"]["change"]["turn_count"]
        for _ in range(3):
            state = orchestrator.state(state["run_id"])
            waiting = state["tasks"]["change"].get("gate_wait")
            if waiting is None:
                break
            orchestrator.approve_task_gate(state["run_id"], "change", "human-owner", waiting["assessment_digest"])
            state = asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal(state["run_id"]))
        self.assertEqual(state["status"], "awaiting_acceptance", state.get("terminal"))
        self.assertEqual(state["tasks"]["change"]["attempts"], before_attempts)
        self.assertEqual(state["tasks"]["change"]["turn_count"], before_turns)
