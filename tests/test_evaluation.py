import asyncio
import copy
import hashlib
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from camol.evaluation import EvaluationError, EvaluatorStore, IntegrationReceipt, evaluator_digest
from camol.orchestrator import Orchestrator
from camol.readiness import WorkspaceReceipt
from camol.runbook import runbook_digest, validate_runbook
from camol.runner import HarnessRunner
from camol.schema import canonical_digest
from camol.state import project
from camol.store import SQLiteEventStore


AGENT = r'''import hashlib
import json
import sys
from pathlib import Path

packet_path, result_path = map(Path, sys.argv[1:3])
packet_bytes = packet_path.read_bytes()
packet = json.loads(packet_bytes)
attempt = packet["task"]["attempt"]
if attempt > 1 and packet.get("last_counterexample") is None:
    raise SystemExit("refinement turn did not receive its counterexample")
mode = packet["run"]["objective"]
if "tamper" in mode:
    Path("oracle.txt").write_text("weakened\n", encoding="utf-8")
    value = "good"
else:
    value = "bad" if attempt == 1 else "good"
Path("output.txt").write_text(value + "\n", encoding="utf-8")
artifact = Path("artifact.txt")
artifact.write_text("attempt={} value={}\n".format(attempt, value), encoding="utf-8")
content = artifact.read_bytes()
result_path.write_text(json.dumps({
    "status": "complete",
    "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
    "checkpoint": "candidate {}".format(value),
    "completed_step_ids": ["work"],
    "input_tokens": 100,
    "output_tokens": 50,
    "evidence": [
        {"kind": "command", "data": {"attempt": attempt}},
        {"kind": "artifact", "data": {"path": str(artifact), "sha256": hashlib.sha256(content).hexdigest()}},
        {"kind": "claim", "data": {"value": value}},
    ],
    "summary": "candidate {}".format(value),
}) + "\n", encoding="utf-8")
'''


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def runbook(run_id, objective, *, attempts=2, assets=None, verification=None):
    return validate_runbook({
        "schema_version": 4,
        "run": {
            "id": run_id, "objective": objective, "max_concurrency": 1,
            "completion": [
                "all_tasks_succeeded", "all_required_evidence_present",
                "all_verifications_green", "no_open_blockers", "no_open_debug_cases",
            ],
            "token_policy": {
                "max_tokens_per_turn": 4000, "checkpoint_reserve": 400,
                "max_total_tokens": 10000, "max_turns_per_task": 4,
            },
            "readiness_policy": {"receipt_ttl_seconds": 300},
        },
        "rules": [{"id": "frozen-eval", "text": "Evaluator inputs are immutable.", "enforcement": "hard"}],
        "agents": [{
            "id": "builder", "role": "Build candidate", "box": ".",
            "capabilities": ["code"], "trust_tier": "developer_trusted",
            "adapter": {
                "kind": "process",
                "argv": ["python3", "{workspace}/agent.py", "{packet}", "{result}"],
                "timeout_seconds": 30,
            },
        }],
        "tasks": [{
            "id": "change", "goal": "Produce a good output", "depends_on": [],
            "capabilities": ["code"], "acceptance": ["output is exactly good"],
            "required_evidence": ["command", "artifact", "claim", "test_result"],
            "max_attempts": attempts,
            "steps": [{
                "id": "work", "instruction": "Implement the requested output.",
                "commands": [{"purpose": "Declared work", "argv": ["python3", "-c", "pass"]}],
                "completion": ["output exists"],
            }],
            "verification": verification or [{
                "purpose": "Frozen output check",
                "argv": ["python3", "-c", "from pathlib import Path; assert Path('output.txt').read_text() == 'good\\n'"],
            }],
            "evaluator_assets": assets or [],
        }],
    })


class EvaluationLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.source.mkdir()
        (self.source / "agent.py").write_text(AGENT, encoding="utf-8")
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Camol Test")
        git(self.source, "config", "user.email", "camol@example.invalid")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, plan):
        store = SQLiteEventStore(self.state / "events.sqlite3")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(store)
        state = orchestrator.initialize(plan)
        orchestrator.approve_plan(state["run_id"], "human-owner", state["plan_digest"])
        final = asyncio.run(HarnessRunner(orchestrator, self.source, state_dir=self.state).run_until_terminal(state["run_id"]))
        return final, store.read(state["run_id"])

    def test_failed_candidate_returns_counterexample_then_refines_and_integrates(self):
        plan = runbook("refinement-proof", "refine once")
        final, events = self.execute(plan)
        self.assertEqual(final["status"], "completed", final.get("terminal"))
        self.assertEqual(final["tasks"]["change"]["attempts"], 2)
        self.assertEqual(len(final["counterexamples"]), 1)
        self.assertEqual(final["counterexamples"][0]["phase"], "candidate")
        self.assertEqual(len(final["candidates"]), 2)
        self.assertEqual(len(final["integrations"]), 1)
        self.assertEqual(final["integration_head"], final["integrations"][0]["revision"])
        event_types = [event["type"] for event in events]
        self.assertLess(event_types.index("COUNTEREXAMPLE_RECORDED"), event_types.index("INTEGRATION_ACCEPTED"))
        self.assertFalse((self.source / "output.txt").exists(), "original checkout must remain unchanged")
        evaluator_invocations = list(
            (self.state / "packets" / "refinement-proof" / "change").glob("evaluator-*.invocation.json")
        )
        self.assertEqual(len(evaluator_invocations), 3, "every evaluator call needs its own durable invocation log")

    def test_builder_cannot_change_frozen_evaluator_asset(self):
        (self.source / "oracle.txt").write_text("strict\n", encoding="utf-8")
        git(self.source, "add", "oracle.txt")
        git(self.source, "commit", "-q", "-m", "oracle")
        verification = [{
            "purpose": "Protected oracle check",
            "argv": [
                "python3", "-c",
                "from pathlib import Path; Path('verifier-ran').write_text('yes'); assert Path('oracle.txt').read_text() == 'strict\\n'",
            ],
        }]
        final, _ = self.execute(runbook(
            "asset-tamper-proof", "tamper with frozen evaluator", attempts=1,
            assets=["oracle.txt"], verification=verification,
        ))
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["tasks"]["change"]["status"], "blocked")
        self.assertEqual(len(final["counterexamples"]), 1)
        self.assertIn("changed frozen evaluator asset", final["counterexamples"][0]["summary"])
        self.assertFalse(any(self.state.glob("worktrees/**/verifier-ran")))
        self.assertEqual((self.source / "oracle.txt").read_text(encoding="utf-8"), "strict\n")

    def test_changed_evaluator_stays_waiting_without_a_second_builder_turn(self):
        (self.source / "oracle.txt").write_text("strict\n", encoding="utf-8")
        git(self.source, "add", "oracle.txt")
        git(self.source, "commit", "-q", "-m", "oracle")
        final, events = self.execute(runbook(
            "asset-readmission-proof", "tamper with frozen evaluator", attempts=2,
            assets=["oracle.txt"],
        ))
        self.assertEqual(final["status"], "running")
        self.assertEqual(final["tasks"]["change"]["status"], "waiting")
        self.assertEqual(final["tasks"]["change"]["attempts"], 1)
        self.assertEqual(
            sum(event["type"] == "AGENT_TURN_RECORDED" for event in events), 1,
            "a mismatched evaluator must deny readmission before another builder turn",
        )

    def test_bundle_is_outside_builder_and_asset_bytes_are_content_addressed(self):
        (self.source / "oracle.txt").write_text("strict\n", encoding="utf-8")
        plan = runbook("bundle-proof", "compile only", assets=["oracle.txt"])
        store = EvaluatorStore(self.state)
        bundle = store.compile(plan, self.source, runbook_digest(plan))
        self.assertEqual(bundle.evaluator_digest, evaluator_digest(plan, self.source))
        record = self.state / "evaluators" / plan["run"]["id"] / (bundle.evaluator_digest[7:] + ".json")
        self.assertTrue(record.is_file())
        self.assertFalse(str(record).startswith(str(self.source)))
        (self.source / "oracle.txt").write_text("weakened\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "changed frozen evaluator asset"):
            store.verify_assets(bundle, self.source)

        before = bundle.evaluator_digest
        with self.assertRaises(TypeError):
            bundle.definition["tasks"][0]["verification"][0]["purpose"] = "weakened"
        with self.assertRaises(TypeError):
            bundle.definition["tasks"] += ({"id": "late"},)
        with self.assertRaises(TypeError):
            bundle.assets[0]["digest"] = canonical_digest({"weakened": True})
        exported = bundle.to_dict()
        exported["definition"]["tasks"][0]["acceptance"].append("caller mutation")
        self.assertEqual(bundle.evaluator_digest, before)
        self.assertNotIn("caller mutation", bundle.definition["tasks"][0]["acceptance"])

    def test_projection_rejects_a_forked_or_duplicate_integration_receipt(self):
        _, events = self.execute(runbook("integration-lineage-proof", "integrate once", attempts=2))
        accepted_index = next(index for index, event in enumerate(events) if event["type"] == "INTEGRATION_ACCEPTED")
        accepted = copy.deepcopy(events[accepted_index])
        accepted["payload"]["receipt"]["workspace"]["base_revision"] = "forked-parent"
        accepted["payload"]["receipt"]["workspace_digest"] = WorkspaceReceipt.from_dict(
            accepted["payload"]["receipt"]["workspace"]
        ).digest()
        accepted["payload"]["receipt_digest"] = IntegrationReceipt.from_dict(
            accepted["payload"]["receipt"]
        ).digest()
        with self.assertRaisesRegex(ValueError, "current integration head"):
            project(events[:accepted_index] + [accepted])

        prefix = events[:accepted_index + 1]
        duplicate = copy.deepcopy(events[accepted_index])
        duplicate["seq"] = prefix[-1]["seq"] + 1
        with self.assertRaisesRegex(ValueError, "duplicates an accepted candidate"):
            project(prefix + [duplicate])


if __name__ == "__main__":
    unittest.main()
