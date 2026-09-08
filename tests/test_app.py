import tempfile
import unittest
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from camol.app import InteractiveController
from camol.connections import _record
from camol.conversation import ConversationReply
from camol.planning import compile_runbook
from camol.schema import canonical_digest
from camol.supervisor import SupervisorPaths


class InteractiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.state_root = root / "state"
        self.controller = InteractiveController(self.workspace, state_root=self.state_root)

    def tearDown(self):
        self.temporary.cleanup()

    def complete_grill(self, model="manual"):
        if model != "manual":
            self.controller.handle("/model " + model)
        self.controller.handle("/grill Build it safely")
        for answer in (
            "A feature and passing tests",
            "Do not deploy or expose credentials",
            "No secrets in output",
            "core | implement core\ntests | add tests | after=core",
            "python3 -m unittest discover -v",
            "boxes=2 turns=4 tokens=12000 cost_cents=100 turn_timeout_seconds=600",
        ):
            response = self.controller.handle(answer)
        return response

    def test_grill_renders_n_box_plan_and_requires_second_step_approval(self):
        response = self.complete_grill("claude:fable")
        self.assertIn("Nothing has started", response.messages[0])
        self.assertIn("tests <- [core]", response.messages[1])
        self.assertIn("canonical product plan JSON:", response.messages[1])
        self.assertIn('"schema": "camol.product_plan"', response.messages[1])
        digest = self.controller.session["plan_digest"]
        self.assertIsNone(self.controller.session["approved_digest"])
        confirmation = self.controller.handle("/approve")
        self.assertIn(digest, confirmation.messages[0])
        self.assertIsNone(self.controller.session["approved_digest"])
        approved = self.controller.handle("/approve yes")
        self.assertIn("Approved", approved.messages[0])
        self.assertEqual(self.controller.session["approved_digest"], digest)

    def test_setting_change_invalidates_frozen_candidate(self):
        self.complete_grill("claude:fable")
        self.controller.handle("/approve yes")
        self.controller.handle("/effort low")
        self.assertIsNone(self.controller.session["plan"])
        self.assertIsNone(self.controller.session["approved_digest"])

    def test_saved_v1_proposal_remains_visible_after_upgrade(self):
        self.complete_grill()
        plan = dict(self.controller.session["plan"])
        proposal = dict(plan["proposal"])
        proposal["schema_version"] = 1
        plan["proposal"] = proposal
        digest = canonical_digest(plan)
        self.controller.session = self.controller.store.update(
            self.controller.session, plan=plan, plan_digest=digest,
        )

        reloaded = InteractiveController(self.workspace, state_root=self.state_root)
        response = reloaded.handle("/plan")
        self.assertIn("limits (legacy V1)", response.messages[0])
        self.assertIn(digest, response.messages[0])

    def test_no_run_before_approval_or_without_executable_provider(self):
        self.complete_grill()
        denied = self.controller.handle("/run --accept-spend")
        self.assertIn("requires the exact current plan", denied.messages[0])
        self.controller.handle("/approve yes")
        denied = self.controller.handle("/run --accept-spend")
        self.assertIn("planning-only", denied.messages[0])

    def test_quit_detaches_without_stop(self):
        response = self.controller.handle("/quit")
        self.assertTrue(response.exit_client)
        self.assertIn("not stopped", response.messages[0])

    def test_login_without_provider_requests_the_keyboard_picker(self):
        response = self.controller.handle("/login")
        self.assertEqual(response.login_choices, ("claude", "codex"))
        self.assertIsNone(response.login_argv)
        self.assertIn("Choose a provider", response.messages[0])

    def test_named_login_always_launches_native_provider_flow(self):
        self.controller.connections.save([
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="ready",
                runtime="claude",
                detail="Authenticated Claude CLI",
            )
        ])
        self.controller.connections.login_argv = Mock(return_value=["/provider/claude", "auth", "login"])

        response = self.controller.handle("/login claude")

        self.assertEqual(response.login_argv, ("/provider/claude", "auth", "login"))
        self.assertEqual(response.login_provider, "claude")
        self.controller.connections.login_argv.assert_called_once_with("claude")

    def test_confirmed_provider_becomes_active_planning_model(self):
        self.controller.connections.save([
            _record(
                "codex-cli",
                "openai",
                "cli",
                status="ready",
                runtime="codex",
                detail="Authenticated Codex CLI",
            )
        ])

        response = self.controller.confirm_provider_connection("codex", login_returncode=0)

        self.assertEqual(self.controller.session["model"], "codex")
        self.assertIn("Codex connection confirmed", response.messages[0])
        reloaded = InteractiveController(self.workspace, state_root=self.state_root)
        self.assertEqual(reloaded.session["model"], "codex")

    def test_unconfirmed_provider_does_not_change_active_model(self):
        self.controller.connections.save([
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="auth_required",
                runtime="claude",
                detail="Run /login claude to authenticate",
            )
        ])

        response = self.controller.confirm_provider_connection("claude", login_returncode=1)

        self.assertEqual(self.controller.session["model"], "manual")
        self.assertIn("did not complete", response.messages[0])

    def test_cancelled_reconnect_does_not_promote_an_older_green_record(self):
        self.controller.connections.save([
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="ready",
                runtime="claude",
                detail="Authenticated Claude CLI",
            )
        ])

        response = self.controller.confirm_provider_connection("claude", login_returncode=1)

        self.assertEqual(self.controller.session["model"], "manual")
        self.assertIn("active model was not changed", response.messages[0])

    def test_skills_and_history_are_inspectable_without_persisting_the_rendered_dump(self):
        self.controller.handle("a retained note")
        before = len(self.controller.session["messages"])

        skills = self.controller.handle("/skills")
        history = self.controller.handle("/history 10")

        self.assertIn("debugger", skills.messages[0])
        self.assertIn("refine", skills.messages[0])
        self.assertIn("a retained note", history.messages[0])
        self.assertNotIn("BUILT-IN PROTOCOLS", history.messages[0])
        self.assertEqual(len(self.controller.session["messages"]), before + 2)

    def test_normal_message_persists_dialogue_separately_from_model_identity(self):
        controller = InteractiveController(
            self.workspace,
            state_root=self.state_root,
            converse_fn=lambda *args, **kwargs: ConversationReply(
                "What must remain invariant?", "codex", None, "gpt-5.6", 10, 5
            ),
        )
        controller.handle("/model codex")

        response = controller.handle("Help me plan this")

        self.assertIn("What must remain invariant?", response.messages[0])
        self.assertEqual(controller.session["messages"][-2]["role"], "orchestrator")
        self.assertEqual(controller.session["messages"][-2]["kind"], "conversation")
        self.assertEqual(controller.session["messages"][-1]["role"], "system")
        self.assertEqual(controller.session["messages"][-1]["kind"], "notice")
        receipt = controller.store.planning_calls()[-1]
        self.assertEqual(receipt["input_tokens"], 10)
        self.assertEqual(receipt["output_tokens"], 5)
        self.assertIsNone(receipt["total_cost_usd"])
        self.assertIn("recorded_input_tokens=10", controller.handle("/usage").messages[0])

    def test_commands_do_not_overlap_active_planning_and_cancel_discards_late_reply(self):
        entered, release = threading.Event(), threading.Event()
        def conversation(*args, **kwargs):
            entered.set()
            release.wait(3)
            return ConversationReply("late reply", "codex", None, None, 4, 5)
        self.controller.converse_fn = conversation
        self.controller.handle("/model codex")
        thread = threading.Thread(target=lambda: self.controller.handle("plan this"))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            response = self.controller.handle("/model manual")
            self.assertIn("still running", response.messages[0])
            self.assertEqual(self.controller.session["model"], "codex")
            self.controller.handle("/cancel")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(any(item["content"] == "late reply" for item in self.controller.session["messages"]))
        self.assertEqual(self.controller.store.planning_calls()[-1]["status"], "cancelled")

    def test_new_plan_reconciles_finished_run_and_gets_distinct_state_directory(self):
        self.complete_grill()
        old_dir = self.controller.session["state_dir"]
        self.controller.session = self.controller.store.update(self.controller.session, status="running")
        with patch.object(self.controller, "_control", return_value={"run": {"status": "completed"}}):
            response = self.controller.handle("/grill second goal")
        self.assertIn("GRILL 1", response.messages[0])
        self.assertNotEqual(self.controller.session["state_dir"], old_dir)

    def test_two_clients_reload_latest_session_instead_of_losing_each_others_changes(self):
        other = InteractiveController(self.workspace, state_root=self.state_root)
        self.controller.handle("/effort low")
        other.handle("/model codex")
        reloaded = self.controller.store.load()
        self.assertEqual(reloaded["effort"], "low")
        self.assertEqual(reloaded["model"], "codex")

    def test_box_subviews_show_artifact_contents_and_mark_disconnected_cache_stale(self):
        snapshot = {
            "adapter_kind": "process", "task_ids": ["build"], "workspace": None,
            "events": [], "artifacts": {
                "sha256:fixture": {"reference": {"digest": "sha256:fixture", "producer": {"channel": "context-packet"}}, "preview": "frozen task context"},
            },
        }
        with patch.object(self.controller, "_control", return_value=snapshot):
            self.assertIn("frozen task context", self.controller.inspect_box("builder", "context"))
        from camol.supervisor import SupervisorError
        with patch.object(self.controller, "_control", side_effect=SupervisorError("offline")):
            self.assertIn("STALE", self.controller.inspect_box("builder", "context"))

    def test_final_acceptance_requires_exact_outcome_and_goes_through_supervisor(self):
        digest = "sha256:" + "a" * 64
        snapshot = {"status": "awaiting_acceptance", "acceptance": {"outcome_digest": digest}, "pending_gates": {}}
        with patch.object(self.controller, "_control", return_value=snapshot) as control:
            challenge = self.controller.handle("/accept")
            self.assertIn(digest, challenge.messages[-1])
            denied = self.controller.handle("/accept yes")
            self.assertIn("exact current outcome", denied.messages[0])
            self.assertTrue(all(call.args[0] == "acceptance" for call in control.call_args_list))
        with patch.object(self.controller, "_control", side_effect=[snapshot, {"status": "completed"}]) as control:
            accepted = self.controller.handle("/accept " + digest)
            self.assertIn("Final outcome accepted", accepted.messages[0])
            self.assertEqual(control.call_args_list[-1].args[0], "accept")
            self.assertEqual(self.controller.session["status"], "terminal")

    def test_gate_approval_rejects_stale_assessment_without_mutation(self):
        digest = "sha256:" + "b" * 64
        snapshot = {"pending_gates": {"task": {"assessment_digest": digest}}}
        with patch.object(self.controller, "_control", return_value=snapshot) as control:
            self.assertIn(digest, self.controller.handle("/gate task").messages[-1])
            self.assertIn("exact current assessment", self.controller.handle("/gate task wrong").messages[0])
            self.assertTrue(all(call.args[0] == "acceptance" for call in control.call_args_list))

    def test_confirmed_provider_does_not_mutate_a_frozen_plan(self):
        self.complete_grill()
        digest = self.controller.session["plan_digest"]
        self.controller.connections.save([
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="ready",
                runtime="claude",
                detail="Authenticated Claude CLI",
            )
        ])

        response = self.controller.confirm_provider_connection("claude", login_returncode=0)

        self.assertEqual(self.controller.session["model"], "manual")
        self.assertEqual(self.controller.session["plan_digest"], digest)
        self.assertIsNotNone(self.controller.session["plan"])
        self.assertIn("Active model remains manual", response.messages[1])

    def test_box_pool_is_derived_not_fixed_at_three(self):
        self.complete_grill("claude:fable")
        boxes = self.controller.box_summaries()
        self.assertEqual(len(boxes), 2)
        self.assertEqual([item["box_id"] for item in boxes], ["builder", "builder-2"])

    def test_effective_worker_policy_matches_approved_effort_cost_and_timeout(self):
        self.controller.handle("/effort xhigh")
        self.complete_grill("claude:fable")
        adapter = self.controller.session["plan"]["runbook"]["agents"][0]["adapter"]
        self.assertEqual(adapter["profile_snapshot"]["effort"], "xhigh")
        self.assertEqual(adapter["profile_snapshot"]["max_run_usd_cents"], 100)
        self.assertEqual(adapter["timeout_seconds"], 600)
        self.controller.handle("/approve yes")
        workspace = SimpleNamespace(assert_source_ready=lambda: None)
        with patch("camol.app.WorkspaceManager", return_value=workspace):
            denied = self.controller.handle("/run --accept-spend --worker-cents 99")
        self.assertIn("exactly match the approved 100 cent", denied.messages[0])

    def test_secret_shaped_grill_input_is_rejected_before_plan_persistence(self):
        response = self.controller.handle("/grill use sk-live-abcdefghijklmnopqrstuvwxyz123456")
        self.assertIn("credential material", response.messages[0])
        self.assertIsNone(self.controller.session["grill"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", self.controller.store.path.read_text())

    def test_stale_socket_is_not_mistaken_for_a_live_supervisor(self):
        self.complete_grill()
        plan = dict(self.controller.session["plan"])
        plan["runbook"] = compile_runbook(
            plan["proposal"], run_id=plan["run_id"],
            adapter={"kind": "process", "argv": ["python3", "agent.py"]},
        )
        plan["execution_status"] = "ready"
        plan["execution_limitation"] = "test process"
        digest = canonical_digest(plan)
        self.controller.session = self.controller.store.update(
            self.controller.session, plan=plan, plan_digest=digest, approved_digest=digest, status="approved"
        )
        socket = SupervisorPaths.under(Path(self.controller.session["state_dir"])).socket
        socket.parent.mkdir(parents=True)
        socket.write_text("stale")
        self.controller.spawn_fn = Mock(return_value={"pid": 123, "started": True})
        workspace = SimpleNamespace(assert_source_ready=lambda: None)
        with patch("camol.app.WorkspaceManager", return_value=workspace):
            response = self.controller.handle("/run")
        self.assertIn("detached", response.messages[1])
        self.controller.spawn_fn.assert_called_once()

    def test_models_command_is_passive_and_does_not_create_a_missing_catalog(self):
        from camol.models import DownloadFile, DownloadPlan, ModelStore
        self.assertIn("No local model", self.controller.handle("/models").messages[0])
        root = self.state_root / "models"
        self.assertFalse(root.exists())
        plan = DownloadPlan("fixture", "fixture/model", "revision-one", "owner",
                            (DownloadFile("model.gguf", "https://fixture.invalid/model.gguf", canonical_digest("bytes"), 10),),
                            ("https://fixture.invalid",), 10, 20)
        with ModelStore(root) as store:
            store.prepare(plan)
            before = store.events(plan.digest())
        with patch("camol.models.HTTPSDownloadTransport.open", side_effect=AssertionError("inspection fetched model")):
            response = self.controller.handle("/models")
            detail = self.controller.handle("/models status " + plan.digest())
        self.assertIn("fixture/model", response.messages[0])
        self.assertIn('"loaded": "unverified"', response.messages[0])
        self.assertIn('"status": "planned"', detail.messages[0])
        with ModelStore(root, read_only=True) as store:
            self.assertEqual(store.events(plan.digest()), before)
        self.assertIn("usage:", self.controller.handle("/models download").messages[0])

    def test_watch_command_reads_existing_ledger_without_observer_execution(self):
        from camol.orchestrator import Orchestrator
        from camol.runbook import load_runbook
        from camol.store import SQLiteEventStore
        from camol.watchers import Watcher, WatchSpec
        from contextlib import closing
        self.complete_grill()
        database = Path(self.controller.session["state_dir"]) / "camol.sqlite3"
        with closing(SQLiteEventStore(database)) as store:
            orchestrator = Orchestrator(store)
            runbook = load_runbook(Path(__file__).resolve().parents[1] / "examples/three-agent-runbook.json")
            runbook["run"]["id"] = self.controller.session["run_id"]
            state = orchestrator.initialize(runbook)
            orchestrator.approve_plan(state["run_id"], "owner", state["plan_digest"])
            spec = WatchSpec("calls", "fixture", "correlated fixture calls", ("start", "end"), ("job",),
                             "end", (("job", "one"),), "v1", "v1", canonical_digest("fixture"))
            Watcher.create(orchestrator, state["run_id"], spec, approved_by="owner")
            before = store.read(state["run_id"])
        with patch("camol.watchers.Watcher.poll", side_effect=AssertionError("inspection polled observer")):
            response = self.controller.handle("/watch show calls")
        self.assertIn("no observer or remote poll", response.messages[0])
        self.assertIn('"watcher_id": "calls"', response.messages[0])
        self.assertEqual(self.controller._persisted_events(), before)

    def test_repo_command_reads_static_graph_without_importing_workspace_code(self):
        import subprocess
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "mod.py").write_text("raise RuntimeError('must not be imported')\nimport json\n")
        subprocess.run(["/usr/bin/git", "-C", str(self.workspace), "add", "mod.py"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(self.workspace), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        response = self.controller.handle("/repo")
        self.assertIn("REPOSITORY GRAPH", response.messages[0])
        self.assertIn("unverified", response.messages[0])
        self.assertIn("mod.py", response.messages[0])
        self.assertIn("usage:", self.controller.handle("/repo execute mod.py").messages[0])

    def _import_codex_fixture(self, *, local=False, snapshot=True):
        import json
        from tests.test_codex_adapter import codex_profile
        from tests.test_gate_runtime import v5_plan
        plan = v5_plan("product-codex")
        profile = codex_profile(local)
        plan["agents"][0]["adapter"] = {"kind": profile["adapter_kind"], "profile": "codex.json", "timeout_seconds": 30}
        if snapshot:
            plan["agents"][0]["adapter"]["profile_snapshot"] = profile
        path = self.workspace.parent / "runbook.json"
        path.write_text(json.dumps(plan))
        workspace = SimpleNamespace(assert_source_ready=lambda: None, head_revision=lambda: "a" * 40)
        with patch("camol.app.WorkspaceManager", return_value=workspace):
            result = self.controller.handle("/import " + str(path))
        return workspace, result

    def test_codex_import_launch_requires_exact_policy_and_hosted_spend_ack(self):
        workspace, imported = self._import_codex_fixture()
        self.assertIn("IMPORTED PLAN", imported.messages[0])
        self.controller.handle("/approve yes")
        self.controller.preflight_fn = Mock(side_effect=AssertionError("Codex invoked paid Claude preflight"))
        self.controller.spawn_fn = Mock(return_value={"pid": 123, "started": True})
        with patch("camol.app.WorkspaceManager", return_value=workspace):
            review = self.controller.handle("/run")
            self.assertIn("quota_available", review.messages[0])
            self.assertIn("no worker or paid preflight started", review.messages[0])
            digest = canonical_digest(self.controller._codex_launch_policy(self.controller.session["plan"]))
            self.assertIn("exact current", self.controller.handle("/run --accept-provider-policy sha256:" + "0" * 64 + " --accept-spend").messages[0])
            self.assertIn("requires --accept-spend", self.controller.handle("/run --accept-provider-policy " + digest).messages[0])
            self.controller.spawn_fn.assert_not_called()
            launched = self.controller.handle("/run --accept-provider-policy " + digest + " --accept-spend")
        self.assertIn("not a hard spend cap", launched.messages[0])
        self.controller.spawn_fn.assert_called_once()
        self.controller.preflight_fn.assert_not_called()

    def test_local_codex_launch_ack_does_not_call_model_or_claim_inference(self):
        workspace, imported = self._import_codex_fixture(local=True)
        self.assertIn("IMPORTED PLAN", imported.messages[0])
        self.controller.handle("/approve yes")
        self.controller.spawn_fn = Mock(return_value={"pid": 123, "started": True})
        digest = canonical_digest(self.controller._codex_launch_policy(self.controller.session["plan"]))
        with patch("camol.app.WorkspaceManager", return_value=workspace), patch("camol.codex_policy.require_local_model", side_effect=AssertionError("client starts inference/catalog")):
            denied = self.controller.handle("/run --accept-provider-policy " + digest + " --accept-spend")
            self.assertIn("local Codex must not", denied.messages[0])
            launched = self.controller.handle("/run --accept-provider-policy " + digest)
        self.assertIn("no model is downloaded or loaded", launched.messages[0])
        self.controller.spawn_fn.assert_called_once()

    def test_codex_interactive_import_rejects_unfrozen_profile_files(self):
        _, result = self._import_codex_fixture(snapshot=False)
        self.assertIn("embedded profile_snapshot", result.messages[0])
        self.assertIsNone(self.controller.session["plan"])


if __name__ == "__main__":
    unittest.main()
