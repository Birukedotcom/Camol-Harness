import tempfile
import asyncio
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from textual.widgets import Input, OptionList

from camol.app import CommandResponse, InteractiveController
from camol.connections import _record
from camol.tui import CamolApp, LoginProviderScreen, PromptArea, SlashCommandScreen, BoxSwitcherScreen, _OwnedClientWork


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_mailbox_composer_send_and_inbox_preserve_draft_and_literal_text(self):
        from tests.test_mailbox_ui import MailboxUITests
        fixture = MailboxUITests()
        # This fixture owns a real fenced run projection; control is an in-process
        # dispatcher so its synchronous UI thread does not share SQLite handles.
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        controller = fixture.controller
        target = fixture.fixture.box
        state = fixture.fixture.orchestrator.state(fixture.fixture.run_id)
        controller.box_summaries = Mock(return_value=[dict(box_id=target, status="running", connected="yes")])
        recorded = []
        from camol.schema import canonical_digest
        def control(command, params=None):
            if command == "box-observe":
                return fixture.fixture.target
            if command == "box-message":
                message = dict(params, sender=dict(kind="human", id=fixture.outbox.get(params["request_id"])["sender"]),
                    message_id="message-" + canonical_digest(dict(run_id=params["run_id"], request_id=params["request_id"])).split(":")[1])
                result = dict(message=message, posted_seq=1, delivered=None, consumed=None)
                recorded.append(result)
                return result
            if command == "box-inbox":
                return dict(schema="camol.box_inbox", schema_version=1, run_id=state["run_id"], plan_digest=state["plan_digest"],
                    box_id=target, messages=recorded, total=len(recorded))
            raise AssertionError(command)
        controller._control = Mock(side_effect=control)
        app = CamolApp(controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text('/message {} "[red] literal message"'.format(target))
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: len(recorded) == 1 and not controller._command_lock.locked(), "message accepted")
            prompt.load_text("/inbox " + target)
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: app.in_box and app.selected_view == "inbox", "inbox pane")
            prompt.load_text("unsent next note")
            await app._refresh_fleet()
            rendered = "\n".join(line.text for line in app.query_one("#box-transcript").lines)
            self.assertIn("[red] literal message", rendered)
            self.assertEqual(prompt.text, "unsent next note")
            controller.converse_fn.assert_not_called()
            controller.spawn_fn.assert_not_called()

    async def wait_for_ui(self, pilot, predicate, description, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("UI did not reach " + description)
            await pilot.pause(.01)

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.controller = InteractiveController(self.workspace, state_root=root / "state")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_terminal_app_has_transcript_multiline_prompt_and_fleet(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#prompt", PromptArea).styles.height.value, 5)
            transcript_styles = app.query_one("#transcript").styles
            self.assertEqual(transcript_styles.scrollbar_size_vertical, 1)
            self.assertEqual(transcript_styles.scrollbar_background.hex, "#09100B")
            self.assertEqual(transcript_styles.scrollbar_color.hex, "#1F713C")
            self.assertEqual(app.query_one("#prompt", PromptArea).styles.background.hex, "#050B07")
            self.assertEqual(app.screen.styles.background.hex, "#030604")
            self.assertIn("ORCH[Alt+0]", str(app.query_one("#fleet").render()))
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/help")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("/grill GOAL", rendered)
            prompt.load_text("sk-live-abcdefghijklmnopqrstuvwxyz123456")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
            self.assertIn("[REDACTED]", rendered)
            prompt.load_text("MY_TOKEN=abcdefghijk")
            app.action_submit()
            await pilot.pause(0.2)
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertNotIn("abcdefghijk", rendered)
            self.assertEqual(app.history[-1], "MY_TOKEN=[REDACTED]")

    async def test_boot_timer_dismisses_only_the_boot_screen(self):
        app = CamolApp(
            self.controller,
            show_boot=True,
            boot_duration=0.01,
            discover_connections=False,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.screen.id, "_default")
            self.assertIsNotNone(app.query_one("#prompt", PromptArea))

    async def test_searchable_box_picker_keyboard_selection_and_draft_preservation(self):
        from tests.test_pane_switcher import PaneSwitcherTests
        fixture = PaneSwitcherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(85, 28)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("an unsent planning draft")
            await pilot.press("alt+b")
            await self.wait_for_ui(pilot, lambda: isinstance(app.screen, BoxSwitcherScreen), "box picker")
            before = len(fixture.controller.store.load()["messages"])
            await pilot.press("ctrl+enter")
            await pilot.pause(.05)
            self.assertEqual(len(fixture.controller.store.load()["messages"]), before)
            await pilot.press("escape")
            await self.wait_for_ui(pilot, lambda: len(app.screen_stack) == 1, "picker cancelled")
            self.assertEqual(prompt.text, "an unsent planning draft")
            await pilot.press("alt+b")
            await self.wait_for_ui(pilot, lambda: isinstance(app.screen, BoxSwitcherScreen), "box picker reopened")
            target = app.screen.snapshot["rows"][1]
            field = app.screen.query_one("#box-picker-input", Input)
            field.value = target["label"]
            await pilot.press("space")
            self.assertIsInstance(app.screen, BoxSwitcherScreen)
            self.assertTrue(field.value.endswith(" "))
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: app.selected == target["box_id"], "selected exact box")
            self.assertEqual(prompt.text, "an unsent planning draft")
            self.assertIsNone(fixture.controller.session["approved_digest"])
            await pilot.press("alt+b")
            await self.wait_for_ui(pilot, lambda: isinstance(app.screen, BoxSwitcherScreen), "return picker")
            app.screen.query_one("#box-picker-input", Input).value = "ORCHESTRATOR"
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: app.selected == "orchestrator", "orchestrator selected")

    async def test_pin_and_group_from_composer_reorder_shortcut_and_search(self):
        import json
        from tests.test_pane_switcher import PaneSwitcherTests
        fixture = PaneSwitcherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        target = fixture.document["agents"][-1]["id"]
        path = fixture.controller.store.project_dir / "pane-organization.json"
        app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/pin " + target)
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: path.exists() and not fixture.controller._command_lock.locked(), "pin persisted")
            await app._refresh_fleet()
            self.assertEqual(app.boxes[0]["box_id"], target)
            self.assertIn("★", str(app.query_one("#fleet").render()))
            prompt.load_text('/group ' + target + ' "[red] API build"')
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: not fixture.controller._command_lock.locked()
                and target in json.loads(path.read_text())["groups"], "group persisted")
            prompt.load_text("unsent draft")
            await pilot.press("alt+b")
            await self.wait_for_ui(pilot, lambda: isinstance(app.screen, BoxSwitcherScreen), "group picker")
            app.screen.query_one("#box-picker-input", Input).value = "API build"
            await pilot.pause()
            options = app.screen.query_one("#box-picker-options", OptionList)
            self.assertEqual(options.option_count, 1)
            self.assertIn("[red] API build", str(options.get_option_at_index(0).prompt))
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: app.in_box and app.selected == target, "group selection")
            self.assertEqual(prompt.text, "unsent draft")
            self.assertIsNone(fixture.controller.session["approved_digest"])
            fixture.controller.converse_fn.assert_not_called()
            path.write_bytes(b"invalid preference record")
            await app._refresh_fleet()
            self.assertIn("preferences unavailable", str(app.query_one("#fleet").render()))
            self.assertEqual(app.selected, target)

    async def test_box_picker_paginates_large_fleet_and_survives_narrow_resize(self):
        from camol.overview import planned_state
        from camol.pane_switcher import switch_snapshot
        from tests.test_pane_switcher import PaneSwitcherTests
        fixture = PaneSwitcherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = planned_state(fixture.document)
        prototype = next(iter(state["agents"].values()))
        state["agents"] = {"worker-{:03d}".format(i): dict(prototype) for i in range(77)}
        snapshot = switch_snapshot(fixture.controller.session, state, "plan_only")
        snapshot["query"] = ""
        app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(85, 28)) as pilot:
            app.selected = "worker-076"
            app.in_box = True
            app._apply_response(CommandResponse(switcher=snapshot))
            await pilot.pause()
            self.assertEqual(app.screen.page_offset, 50)
            await pilot.press("pageup")
            self.assertEqual(app.screen.query_one("#box-picker-options", OptionList).option_count, 50)
            await pilot.press("pagedown")
            self.assertEqual(app.screen.page_offset, 50)
            self.assertEqual(app.screen.query_one("#box-picker-options", OptionList).option_count, 28)
            await pilot.resize_terminal(45, 18)
            field = app.screen.query_one("#box-picker-input", Input)
            field.value = "worker-076 dormant"
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#box-picker-options", OptionList).option_count, 1)
            field.value = "no matching box"
            await pilot.press("enter")
            self.assertIsInstance(app.screen, BoxSwitcherScreen)
            await pilot.press("ctrl+c")
        self.assertFalse(app.is_running)

    async def test_threaded_login_picker_and_command_denial_are_visible_from_box(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(90, 28)) as pilot:
            app._render_box("worker", "events", "read-only fixture")
            app.query_one("#prompt", PromptArea).load_text("/box missing")
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: app.selected == "orchestrator", "denial visible in orchestrator")
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("unknown box", rendered)
            app.query_one("#prompt", PromptArea).load_text("/login")
            await pilot.press("enter")
            await self.wait_for_ui(pilot, lambda: isinstance(app.screen, LoginProviderScreen), "threaded native login chooser")
            await pilot.press("escape")
            await self.wait_for_ui(pilot, lambda: len(app.screen_stack) == 1, "login chooser cancelled")

    async def test_worker_named_orchestrator_is_not_the_orchestrator_pane(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(90, 28)) as pilot:
            app._render_box("orchestrator", "events", "worker with a colliding display name")
            app._render_dependency_rail()
            self.assertTrue(app.in_box)
            self.assertIn("BOX orchestrator", str(app.query_one("#context").render()))
            app.boxes = [dict(box_id="orchestrator")]
            app.action_cycle(-1)
            self.assertFalse(app.in_box)
            self.assertIn("ORCHESTRATOR —", str(app.query_one("#context").render()))

    async def test_composer_orders_multiline_typing_before_send_shortcuts(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.press("a", "ctrl+j", "b", "ctrl+enter")
            await self.wait_for_ui(pilot, lambda: bool(app.history), "ordered multiline submit")
            self.assertEqual(app.history[-1], "a\nb")
            self.assertEqual(app.query_one("#prompt", PromptArea).text, "")

    async def test_overview_command_runs_in_composer_without_starting_work(self):
        import json
        from camol.schema import canonical_digest
        document = json.loads((Path(__file__).resolve().parents[1] / "examples/local-n-box-runbook.json").read_text())
        plan = dict(schema="camol.product_plan", schema_version=2, proposal=None,
            run_id=document["run"]["id"], runbook=document, execution_status="ready",
            execution_limitation="fixture", source=dict(workspace=str(self.workspace), revision="a" * 40))
        self.controller.session = self.controller.store.update(self.controller.session,
            plan=plan, plan_digest=canonical_digest(plan), run_id=plan["run_id"])
        self.controller.spawn_fn = Mock(side_effect=AssertionError("must not spawn"))
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("/overview")
            await pilot.press("enter")
            def rendered():
                return "\n".join(line.text for line in app.query_one("#transcript").lines)
            await self.wait_for_ui(pilot, lambda: "CAMOL OVERVIEW" in rendered(), "overview rendered")
            self.assertIn("plan_only", rendered())
            self.assertIn("DEPENDENCIES", rendered())
            self.assertIn("ORCH[Alt+0]", str(app.query_one("#fleet").render()))
            self.assertIsNone(self.controller.session["approved_digest"])
        self.controller.spawn_fn.assert_not_called()

    async def test_revision_review_and_handoff_return_focus_from_box_to_orchestrator(self):
        from tests.test_revision_commands import RevisionCommandTests
        from camol.revision_ui import RevisionUI
        fixture = RevisionCommandTests()
        fixture.setUp()
        try:
            app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
            async with app.run_test(size=(110, 32)) as pilot:
                app._render_box("builder", "events", "old box view")
                app.query_one("#prompt", PromptArea).load_text(fixture.command)
                await pilot.press("enter")
                await self.wait_for_ui(pilot, lambda: app.selected == "orchestrator", "visible revision review")
                await app.workers.wait_for_complete()
                review = RevisionUI(fixture.controller.store).inspect(fixture.controller.session)
                rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
                self.assertIn("STOPPED-OWNER REVISION", rendered)
                app._render_box("builder", "events", "old box view")
                app.query_one("#prompt", PromptArea).load_text("/revise apply " + review["review_digest"])
                await pilot.press("enter")
                await self.wait_for_ui(pilot, lambda: app.selected == "orchestrator", "revision handoff focus")
                await app.workers.wait_for_complete()
                self.assertEqual(fixture.controller.session["run_id"], "successor")
                self.assertTrue(app.query_one("#transcript").display)
                self.assertFalse(app.query_one("#box-transcript").display)
                fixture.controller.spawn_fn.assert_not_called()
        finally:
            fixture.doCleanups()

    async def test_seed_proposal_from_composer_is_visible_unapproved_and_needs_exact_digest(self):
        from tests.test_proposal import ProposalUITests
        fixture = ProposalUITests()
        fixture.setUp()
        try:
            app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                prompt = app.query_one("#prompt", PromptArea)
                prompt.load_text(fixture.command)
                app.action_submit()
                for _ in range(30):
                    await pilot.pause(.1)
                    if fixture.controller.session["status"] == "plan_ready":
                        break
                self.assertEqual(fixture.controller.session["status"], "plan_ready")
                await self.wait_for_ui(pilot, lambda: "MODEL CANDIDATE" in "\n".join(line.text for line in app.query_one("#transcript").lines), "rendered proposal")
                rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
                self.assertIn("MODEL CANDIDATE", rendered)
                self.assertIsNone(fixture.controller.session["approved_digest"])
                prompt.load_text("/approve " + fixture.controller.session["plan_digest"])
                app.action_submit()
                for _ in range(30):
                    await pilot.pause(.1)
                    if fixture.controller.session["status"] == "approved":
                        break
                self.assertEqual(fixture.controller.session["status"], "approved",
                                 "\n".join(line.text for line in app.query_one("#transcript").lines))
                self.assertFalse(Path(fixture.controller.session["state_dir"]).exists())
                fixture.provider.assert_called_once()
        finally:
            fixture.tearDown()

    async def test_detached_fleet_target_is_not_a_background_refresh_failure(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.query_one("#fleet").remove()
            await app._refresh_fleet()

    async def test_cancelled_refresh_on_detach_or_supersession_is_not_an_app_failure(self):
        from textual.worker import WorkerCancelled
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            cancelled = Mock(wait=AsyncMock(side_effect=WorkerCancelled("fixture detach")))
            with patch.object(app, "run_worker", return_value=cancelled):
                await app._refresh_fleet()
            app.selected = "fixture-box"
            app.in_box = True
            with patch.object(app, "run_worker", side_effect=[Mock(wait=AsyncMock(return_value=[])), cancelled]):
                await app._refresh_fleet()

    async def test_refresh_parent_cancellation_survives_real_worker_conversion(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            original = app.run_worker
            for mode in ("fleet", "box", "monitor"):
                entered = asyncio.Event()
                async def blocked():
                    entered.set()
                    await asyncio.Event().wait()
                def controlled(work, **kwargs):
                    if mode == "box" and kwargs["group"] == "fleet":
                        return Mock(wait=AsyncMock(return_value=[]))
                    return original(blocked, group="cancel-regression", exclusive=True)
                app.in_box = mode == "box"
                app.selected = "fixture-box" if app.in_box else "orchestrator"
                callback = app._refresh_monitor if mode == "monitor" else app._refresh_fleet
                with patch.object(app, "run_worker", side_effect=controlled):
                    refresh = asyncio.create_task(callback())
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        # The same callback used by the refresh timer, with a
                        # real Worker.wait. Old behavior returns normally here,
                        # allowing the timer loop to continue during shutdown.
                        refresh.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await refresh
                    finally:
                        if not refresh.done():
                            refresh.cancel()
                            await asyncio.gather(refresh, return_exceptions=True)
                        app.workers.cancel_group(app, "cancel-regression")
            app.in_box = False

    async def test_parent_cancel_is_not_converted_to_harmless_supersession(self):
        from camol.tui import _await_view_worker
        from textual.worker import WorkerCancelled
        entered = asyncio.Event()
        async def converting_wait():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise WorkerCancelled("converted by worker wait")
        task = asyncio.create_task(_await_view_worker(Mock(wait=converting_wait)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_natural_goal_creation_questions_review_and_approval_in_composer(self):
        import json
        from camol.conversation import ConversationReply
        from camol.schema import canonical_digest
        from tests.test_draft_creation import DraftCreationTests, model_candidate
        fixture = DraftCreationTests()
        fixture.setUp()
        try:
            app = CamolApp(fixture.controller, show_boot=False, discover_connections=False)
            async with app.run_test(size=(110, 32)) as pilot:
                async def submit(text):
                    app.query_one("#prompt", PromptArea).load_text(text)
                    app.action_submit()
                    await app.workers.wait_for_complete()
                    await pilot.pause(.02)

                for text in (
                    "/grill --draft Build two independently verified files",
                    "left.txt contains good; right.txt contains good", "No deployment or upload",
                    "README stays baseline", "Unknown newline requirement; ask before defining the oracle",
                    "process --trusted python3 {workspace}/worker.py {packet} {result}",
                    "boxes=2 concurrency=2 turns=4 tokens=20000 cost_cents=0 timeout=30 tasks=4 attempts=2",
                ):
                    await submit(text)
                fixture.provider.assert_not_called()
                state = fixture.controller.session["grill"]
                self.assertEqual(state["phase"], "review")
                await submit("/draft confirm " + canonical_digest(state["envelope"]))
                fixture.provider.return_value = ConversationReply('{"questions":["Must both files end with a newline?"]}', "claude", "fable", None, 2, 3)
                await submit("/propose")
                self.assertEqual(fixture.controller.session["grill"]["phase"], "clarifying")
                await submit("Yes, one newline in each.")
                self.assertEqual(fixture.provider.call_count, 1)
                state = fixture.controller.session["grill"]
                await submit("/draft confirm " + canonical_digest(state["envelope"]))
                fixture.provider.return_value = ConversationReply(json.dumps(model_candidate(state["envelope"])), "claude", "fable", None, 5, 8)
                await submit("/propose")
                self.assertEqual(fixture.controller.session["status"], "plan_ready")
                digest = fixture.controller.session["plan_digest"]
                await submit("/approve " + digest)
                self.assertIsNone(fixture.controller.session["approved_digest"])
                await submit("/review " + digest)
                await submit("/approve " + digest)
                self.assertEqual(fixture.controller.session["status"], "approved")
                self.assertFalse(Path(fixture.controller.session["state_dir"]).exists())
                self.assertEqual(fixture.provider.call_count, 2)
                self.assertTrue(fixture.provider.call_args.kwargs["no_tools"])
                rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
                self.assertIn("DRAFT RISK", rendered)
                self.assertIn("command/scope/oracle review", rendered)
                self.assertIn("Approved", rendered)
        finally:
            fixture.tearDown()

    async def test_n_box_fleet_and_keyboard_navigation(self):
        for command in (
            "/model claude:fable",
            "/grill build",
            "done",
            "no deploy",
            "preserve secrets",
            "a | first\nb | second | after=a",
            "python3 -m unittest",
            "boxes=2 turns=3 tokens=10000 cost_cents=100 turn_timeout_seconds=600",
        ):
            self.controller.handle(command)
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause(0.2)
            fleet = str(app.query_one("#fleet").render())
            self.assertIn("builder", fleet)
            self.assertIn("builder-2", fleet)
            app.action_box(2)
            await self.wait_for_ui(pilot, lambda: app.selected == "builder-2", "selected box builder-2")
            self.assertEqual(app.selected, "builder-2")
            self.assertIn("BOX builder-2", str(app.query_one("#context").render()))
            self.assertTrue(app.query_one("#box-transcript").display)
            self.assertFalse(app.query_one("#transcript").display)
            app.action_orchestrator()
            self.assertTrue(app.query_one("#transcript").display)

    async def test_detach_waits_for_actual_controller_persistence_not_cancelled_future(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        path = self.workspace / "owned-client-finished"
        def delayed_handle(raw, **kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            path.write_text("completed persistence\n")
            finished.set()
            return CommandResponse(messages=("finished",))
        self.controller.handle = delayed_handle
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("fixture request")
            await self.wait_for_ui(pilot, started.is_set, "running controller request")
            asyncio.get_running_loop().call_later(.05, release.set)
            app.action_detach()
        self.assertTrue(finished.is_set(), "run_test returned while controller persistence was still running")
        self.assertTrue(app._client_work.idle.is_set())
        self.assertEqual(path.read_text(), "completed persistence\n")

    def test_shutdown_rejects_queued_tickets_and_detached_controller_requests(self):
        ownership = _OwnedClientWork()
        queued, running = ownership.reserve(), ownership.reserve()
        self.assertTrue(ownership.start(running))
        ownership.close()
        self.assertFalse(ownership.start(queued))
        self.assertFalse(ownership.idle.is_set())
        self.assertIsNone(ownership.reserve())
        ownership.finish(running)
        self.assertTrue(ownership.idle.is_set())
        before = self.controller.store.history([], 20)
        self.controller.close_client()
        self.assertIn("detached", self.controller.handle("this must never persist").messages[0])
        self.assertEqual(self.controller.store.history([], 20), before)

    async def test_two_connection_probes_release_serialization_before_detach_waits(self):
        release, first_started = threading.Event(), threading.Event()
        calls = []
        def probe(provider, **kwargs):
            calls.append(provider)
            if len(calls) == 1:
                first_started.set()
                self.assertTrue(release.wait(2))
            return []
        self.controller.connections.refresh = probe
        app = CamolApp(self.controller, show_boot=False)
        with patch.object(app, "call_from_thread", side_effect=AssertionError("probe waits for UI callback")):
            async with app.run_test(size=(100, 30)) as pilot:
                app._probe_connections(login_provider="claude", login_returncode=0)
                await self.wait_for_ui(pilot, first_started.is_set, "first blocked probe")
                app._probe_connections(login_provider="codex", login_returncode=0)
                await self.wait_for_ui(pilot, lambda: list(app._client_work.tickets.values()).count("running") == 2, "second serialized probe")
                asyncio.get_running_loop().call_later(.05, release.set)
                app.action_detach()
        self.assertEqual(calls, ["claude", "codex"])
        self.assertTrue(app._client_work.idle.is_set())
        self.assertFalse(app._connection_probe_lock.locked())

    async def test_stream_waits_for_split_secret_then_redacts_before_display(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await asyncio.to_thread(app._stream_chunk, "response sk-live-abc")
            self.assertNotIn("sk-live-abc", str(app.query_one("#stream").render()))
            await asyncio.to_thread(app._stream_chunk, "defghijklmnopqrstuvwxyz123456 ")
            rendered = str(app.query_one("#stream").render())
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
            self.assertIn("[REDACTED]", rendered)

    async def test_startup_and_repeated_rail_render_are_cached_only_and_never_green(self):
        records = [
            _record(
                "claude-cli",
                "anthropic",
                "cli",
                status="ready",
                runtime="claude",
                detail="Authenticated Claude CLI",
            )
        ]
        self.controller.connections.save(records)
        self.controller.connections.probe_all = Mock(side_effect=AssertionError("startup probe"))
        self.controller.connections.refresh = Mock(side_effect=AssertionError("startup refresh"))
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            with patch("camol.tui.shutil.which", return_value="/installed-only"):
                app._render_dependency_rail()
            rail = str(app.query_one("#dependency-rail").render())
            self.assertIn("□ claude auth-observed", rail)
            self.assertIn("□ docker installed; daemon?", rail)
            self.assertIn("task unverified", rail)
            self.assertNotIn("■", rail)
            self.assertNotIn("↻", rail)
            self.controller.connections.probe_all.assert_not_called()
            self.controller.connections.refresh.assert_not_called()

    async def test_login_opens_keyboard_picker_and_enter_selects_provider(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._apply_response(self.controller.handle("/login"))
            await pilot.pause()
            self.assertIsInstance(app.screen, LoginProviderScreen)
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/login codex")

    async def test_typing_slash_opens_command_palette_and_enter_runs_safe_command(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, SlashCommandScreen)
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/skills")

    async def test_palette_filters_as_the_user_types_a_command(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            command_input = app.screen.query_one("#command-input", Input)
            await pilot.press("s", "k", "i", "l", "l", "s")
            self.assertEqual(command_input.value, "/skills")
            self.assertEqual(app.screen.query_one("#command-options", OptionList).option_count, 1)
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/skills")

    async def test_space_stays_in_typed_command_instead_of_scrolling_the_palette(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause(0.2)
            command_input = app.screen.query_one("#command-input", Input)
            await pilot.press("g", "r", "i", "l", "l", "space", "b", "u", "i", "l", "d")
            self.assertEqual(command_input.value, "/grill build")
            self.assertIsInstance(app.screen, SlashCommandScreen)
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/grill build")

    async def test_enter_sends_and_shift_enter_adds_a_line(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptArea)
            prompt.load_text("first")
            prompt.move_cursor((0, len(prompt.text)))
            await pilot.press("shift+enter")
            await pilot.press("s", "e", "c", "o", "n", "d")
            self.assertEqual(prompt.text, "first\nsecond")
            await pilot.press("enter")
            await pilot.pause()
            app._submit.assert_called_once_with("first\nsecond")

    async def test_reopen_shows_a_reattach_boundary_without_replaying_old_output(self):
        self.controller.handle("/btw retained context")
        reopened = InteractiveController(self.workspace, state_root=Path(self.temporary.name) / "state")
        app = CamolApp(reopened, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("REATTACHED", rendered)
            self.assertIn("durable transcript entries retained", rendered)
            self.assertNotIn("retained context", rendered)

    async def test_confirmed_login_activates_model_and_populates_orchestrator(self):
        record = _record(
            "claude-cli",
            "anthropic",
            "cli",
            status="ready",
            runtime="claude",
            detail="Authenticated Claude CLI",
        )
        self.controller.connections.save([record])
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._finish_connection_probe("claude", 0)
            await pilot.pause()
            self.assertEqual(self.controller.session["model"], "claude:fable")
            self.assertIn("auth-observed", str(app.query_one("#context").render()))
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("Claude connection confirmed", rendered)
            self.assertIn("model set to claude:fable", rendered)

    async def test_failed_login_refresh_does_not_promote_cached_authentication(self):
        self.controller.connections.save([_record("claude-cli", "anthropic", "cli", status="ready", runtime="claude")])
        app = CamolApp(self.controller, show_boot=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._finish_connection_probe("claude", 0, False)
            self.assertEqual(self.controller.session["model"], "manual")
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("not a new confirmation", " ".join(rendered.split()))

    async def test_picker_reconnects_even_when_discovery_already_reports_connected(self):
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
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        app._submit = Mock()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._apply_response(self.controller.handle("/login"))
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            app._submit.assert_called_once_with("/login codex")

    async def test_control_c_during_native_login_detaches_without_a_traceback(self):
        app = CamolApp(self.controller, show_boot=False, discover_connections=False)
        async with app.run_test(size=(100, 30)):
            with (
                patch.object(app, "suspend", return_value=nullcontext()),
                patch("camol.tui.subprocess.run", side_effect=KeyboardInterrupt),
            ):
                app._apply_response(
                    CommandResponse(
                        login_argv=("claude", "auth", "login"),
                        login_provider="claude",
                    )
                )
            rendered = "\n".join(line.text for line in app.query_one("#transcript").lines)
            self.assertIn("Provider login cancelled; client detached safely", rendered)


if __name__ == "__main__":
    unittest.main()
