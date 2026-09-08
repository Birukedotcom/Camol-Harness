import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from camol.app import InteractiveController, SLASH_COMMANDS
from camol.cli import main
from camol.orchestrator import Orchestrator
from camol.overview import fleet_overview, planned_state, render_overview
from camol.schema import canonical_digest
from camol.store import SQLiteEventStore


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.document = json.loads((Path(__file__).resolve().parents[1] / "examples/local-n-box-runbook.json").read_text())

    def test_snapshot_deterministic_detached_and_no_content(self):
        state = planned_state(self.document)
        state["evidence"] = {"secret": "raw model output must not appear"}
        original = copy.deepcopy(state)
        report = fleet_overview(state, basis="plan_only")
        self.assertEqual(report, fleet_overview(state, basis="plan_only"))
        self.assertEqual(state, original)
        self.assertFalse(report["live_connection_proven"])
        self.assertEqual(report["snapshot_digest"], canonical_digest({key: value for key, value in report.items() if key != "snapshot_digest"}))
        self.assertNotIn("raw model output", json.dumps(report))
        report["tasks"][0]["dependencies"].append("mutation")
        self.assertEqual(state, original)

    def test_dependencies_waits_and_human_gates_are_distinct(self):
        state = planned_state(self.document)
        identifiers = list(state["tasks"])
        state["tasks"][identifiers[1]]["depends_on"] = [identifiers[0]]
        state["tasks"][identifiers[0]].update(status="succeeded")
        state["tasks"][identifiers[1]].update(status="waiting", waiting={"code": "CAPACITY_EXHAUSTED"})
        state["tasks"][identifiers[2]].update(status="submitted", gate_wait={"phase": "candidate"})
        report = fleet_overview(state, attention=True)
        tasks = {item["task_id"]: item for item in report["tasks"]}
        self.assertEqual(tasks[identifiers[1]]["unmet_dependencies"], [])
        self.assertEqual(tasks[identifiers[1]]["waiting_code"], "CAPACITY_EXHAUSTED")
        self.assertEqual(tasks[identifiers[2]]["gate_phase"], "candidate")
        self.assertIn("gate:candidate", render_overview(report))

    def test_pagination_never_assumes_three_boxes(self):
        state = planned_state(self.document)
        agent = next(iter(state["agents"].values()))
        state["agents"] = {"box-{:03d}".format(n): dict(agent) for n in range(77)}
        report = fleet_overview(state, limit=10, offset=70)
        self.assertEqual(report["counts"]["boxes"], 77)
        self.assertEqual(len(report["boxes"]), 7)
        self.assertFalse(report["page"]["more_boxes"])
        for changes in ({"limit": True}, {"offset": -1}, {"limit": 201}, {"attention": 1}):
            with self.assertRaises(ValueError):
                fleet_overview(state, **changes)

    def invoke(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(arguments)
        return code, output.getvalue(), error.getvalue()

    def test_cli_uses_exact_run_and_never_creates_missing_store(self):
        database = self.root / "state" / "camol.sqlite3"
        args = ["overview", "--db", str(database), "--run-id", self.document["run"]["id"], "--json"]
        self.assertEqual(self.invoke(args)[0], 2)
        self.assertFalse(database.parent.exists())
        store = SQLiteEventStore(database)
        try:
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(self.document)
            other = copy.deepcopy(self.document)
            other["run"]["id"] = "unrelated-latest"
            orchestrator.initialize(other)
        finally:
            store.close()
        code, output, error = self.invoke(args)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["run_id"], state["run_id"])

    def test_slash_overview_planned_only_no_model_probe_or_spawn(self):
        workspace = self.root / "repo"
        workspace.mkdir()
        controller = InteractiveController(workspace, state_root=self.root / "interactive")
        controller.spawn_fn = Mock(side_effect=AssertionError("no spawn"))
        controller.connections.refresh = Mock(side_effect=AssertionError("no probe"))
        plan = dict(schema="camol.product_plan", schema_version=2, proposal=None,
            run_id=self.document["run"]["id"], runbook=self.document, execution_status="ready",
            execution_limitation="fixture", source=dict(workspace=str(workspace), revision="a" * 40))
        controller.session = controller.store.update(controller.session, plan=plan, plan_digest=canonical_digest(plan), run_id=plan["run_id"])
        original = copy.deepcopy(controller.session["plan"])
        response = controller.handle("/overview --json --limit 2")
        report = json.loads(response.messages[0])
        self.assertEqual(report["basis"], "plan_only")
        self.assertEqual(len(report["boxes"]), 2)
        self.assertEqual(controller.session["plan"], original)
        self.assertIsNone(controller.session["approved_digest"])
        self.assertFalse((Path(controller.session["state_dir"]) / "camol.sqlite3").exists())
        self.assertIn("/overview", {item.command for item in SLASH_COMMANDS})
        for command in ("/overview --limit 0", "/overview --offset -1", "/overview --json --json", "/overview --unknown"):
            self.assertIn("denied", controller.handle(command).messages[0])

    def test_corrupt_existing_ledger_never_falls_back_to_planned_green(self):
        workspace = self.root / "repo"
        workspace.mkdir()
        controller = InteractiveController(workspace, state_root=self.root / "interactive")
        controller.session = controller.store.update(controller.session, run_id="chosen-run")
        state = Path(controller.session["state_dir"])
        state.mkdir(parents=True)
        (state / "camol.sqlite3").write_bytes(b"not sqlite")
        response = controller.handle("/overview")
        self.assertIn("denied", response.messages[0])
        self.assertNotIn("plan_only", response.messages[0])
        code, output, error = self.invoke(["overview", "--db", str(state / "camol.sqlite3"), "--run-id", "chosen-run"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("unreadable", error)

    def test_text_renderer_strips_terminal_controls_and_has_explicit_page(self):
        state = planned_state(self.document)
        report = fleet_overview(state, limit=1)
        report["boxes"][0]["box_id"] = "unsafe\x1b]52;c;payload\x07name"
        rendered = render_overview(report)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertIn("More rows", rendered)
