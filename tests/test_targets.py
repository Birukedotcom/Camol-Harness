import copy
import asyncio
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

from camol.api import Harness
from camol.artifacts import ArtifactStore, RunArchive
from camol.cli import main
from camol.events import new_event
from camol.schema import canonical_digest
from camol.readiness import WaitingReason
from camol.state import apply_event, project
from camol.store import ConcurrentAppendError
from camol.targets import TargetError, TargetRegistry, descriptor, snapshot
from tests import test_admission_scheduler as fixture_module
from tests import test_supervisor as supervisor_fixture
from camol.supervisor import Supervisor, SupervisorError, send_control, send_control_v2


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.AdmissionSchedulerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.orch, self.run = self.fixture.orchestrator, self.fixture.run_id
        self.registry = TargetRegistry(self.orch, self.run)
        self.target = dict(schema="camol.execution_target", schema_version=1, target_id="target-build",
            generation="generation-1", control_plane_id="control-1", label="voice build",
            ownership="adopted", provider=dict(kind="gcp", account="account-1", project="project-1",
                location="zone-a", resource_id="instance-123", resource_name="voice-dev"),
            transport=dict(kind="worker_tls", profile_digest="sha256:" + "a" * 64))

    def proposal(self, target=None):
        return self.registry.propose(target or self.target, by="test-owner", expires_at="2026-09-03T12:05:00Z")

    def adopt(self, value=None):
        value = value or self.proposal()
        return self.registry.adopt(value, by="test-owner", approval_digest=value["digest"])

    def retire(self, value):
        return self.registry.retire(value["descriptor"]["generation"], by="test-owner", adoption_digest=value["digest"], reason="operator detached target")

    def test_adopt_retire_replay_archive_without_readiness_or_execution(self):
        before = self.orch.state(self.run)
        proposal = self.proposal()
        self.assertEqual(self.orch.state(self.run), before)
        record = self.adopt(proposal)
        self.assertEqual(record["readiness"], "unproven")
        self.assertFalse(record["execution_authority"])
        self.assertFalse(record["deletion_authority"])
        adopted = self.orch.state(self.run)
        self.assertEqual({k: v for k, v in adopted.items() if k not in {"last_seq", "execution_targets"}},
                         {k: v for k, v in before.items() if k != "last_seq"})
        self.assertEqual(self.adopt(proposal), record)
        self.assertEqual(self.orch.state(self.run), adopted)
        retired = self.retire(proposal)
        self.assertEqual(retired["status"], "retired")
        self.assertEqual(self.retire(proposal), retired)
        self.assertEqual(self.adopt(proposal), retired)  # Receipt lookup, not revival.
        events = self.fixture.store.read(self.run)
        self.assertEqual(project(events), self.orch.state(self.run))
        archive = self.fixture.state_dir.parent / "target-export"
        RunArchive.export(self.run, events, ArtifactStore(self.fixture.state_dir), archive)
        self.assertEqual(RunArchive.replay(archive), self.orch.state(self.run))

    def test_duplicate_resource_target_and_generation_require_retirement(self):
        original = self.proposal()
        self.adopt(original)
        for mutation in (dict(generation="generation-2"), dict(generation="generation-2", target_id="renamed-target")):
            target = dict(self.target, **mutation)
            target["provider"] = dict(target["provider"], resource_name="renamed-vm")
            with self.assertRaises(TargetError):
                self.adopt(self.proposal(target))
        changed = self.proposal(dict(self.target, label="new label"))
        with self.assertRaises(TargetError):
            self.adopt(changed)
        self.retire(original)
        self.assertEqual(self.adopt(self.proposal(dict(self.target, generation="generation-2")))["status"], "adopted")
        self.assertEqual(self.registry.inspect()["count"], 2)
        with self.assertRaises(TargetError):
            self.adopt(self.proposal(dict(self.target, generation="generation-3", control_plane_id="other-control")))

    def test_exact_owner_plan_digest_expiry_and_replay_forgery(self):
        proposal = self.proposal()
        for by, digest in (("strategist", proposal["digest"]), ("test-owner", "sha256:" + "f" * 64)):
            with self.assertRaises(TargetError):
                self.registry.adopt(proposal, by=by, approval_digest=digest)
        for key, value in (("run_id", "other"), ("plan_digest", "sha256:" + "b" * 64), ("schema_version", True)):
            changed = dict(proposal, **{key: value})
            changed["digest"] = canonical_digest({k: v for k, v in changed.items() if k != "digest"})
            with self.assertRaises(TargetError):
                self.adopt(changed)
        state = self.orch.state(self.run)
        event = new_event(self.run, "TARGET_ADOPTED", "strategist", dict(proposal=proposal, approval_digest=proposal["digest"]), occurred_at=self.orch._now())
        with self.assertRaises(TargetError):
            apply_event(state, dict(event, seq=state["last_seq"] + 1))
        self.fixture.clock.advance(300)
        with self.assertRaises(TargetError):
            self.adopt(proposal)
        self.assertNotIn("execution_targets", self.orch.state(self.run))

    def test_gcp_account_label_cannot_duplicate_same_machine(self):
        self.adopt()
        changed = copy.deepcopy(self.target)
        changed.update(target_id="renamed-target", generation="generation-2")
        changed["provider"].update(account="another-login-label", resource_name="another-display-name")
        before = self.orch.state(self.run)
        with self.assertRaises(TargetError):
            self.adopt(self.proposal(changed))
        self.assertEqual(self.orch.state(self.run), before)
        proposed = self.proposal(changed)
        forged = new_event(self.run, "TARGET_ADOPTED", "test-owner", dict(proposal=proposed,
            approval_digest=proposed["digest"]), occurred_at=self.orch._now())
        events = self.fixture.store.read(self.run)
        with self.assertRaises(TargetError):
            project(events + [dict(forged, seq=before["last_seq"] + 1)])
        self.assertEqual(self.fixture.store.read(self.run), events)

    def test_gcp_account_change_after_retirement_keeps_exact_new_review(self):
        first = self.proposal()
        self.adopt(first)
        self.retire(first)
        changed = copy.deepcopy(self.target)
        changed.update(target_id="renamed-target", generation="generation-2")
        changed["provider"]["account"] = "another-login-label"
        proposed = self.proposal(changed)
        with self.assertRaises(TargetError):
            self.registry.adopt(proposed, by="test-owner", approval_digest=first["digest"])
        record = self.adopt(proposed)
        self.assertEqual(record["proposal"]["descriptor"]["provider"]["account"], "another-login-label")
        self.assertEqual(project(self.fixture.store.read(self.run)), self.orch.state(self.run))

    def test_provider_specific_account_and_project_namespaces_stay_distinct(self):
        self.target["provider"]["kind"] = "manual"
        self.adopt()
        changed = copy.deepcopy(self.target)
        changed.update(target_id="second-target", generation="generation-2")
        changed["provider"]["account"] = "different-resource-account"
        self.adopt(self.proposal(changed))
        for index, provider in enumerate((dict(self.target["provider"], kind="gcp"),
                dict(self.target["provider"], kind="gcp", project="project-2"),
                dict(self.target["provider"], kind="gcp", location="zone-b")), start=3):
            target = dict(self.target, target_id="target-" + str(index), generation="generation-" + str(index), provider=provider)
            self.adopt(self.proposal(target))
        self.assertEqual(self.registry.inspect()["count"], 5)

    def test_retirement_denies_lease_before_process_launch(self):
        bundle, _ = self.fixture.admit()
        proposal = self.proposal(dict(self.target, target_id=bundle.binding.target_id))
        self.adopt(proposal)
        assignment = self.fixture.frame_assignment()
        self.assertEqual(self.orch.state(self.run)["tasks"][assignment["task_id"]]["status"], "leased")
        for elapsed in (0, 60):
            self.fixture.clock.advance(elapsed)
            before = self.orch.state(self.run)
            with self.assertRaises(TargetError):
                self.retire(proposal)
            self.assertEqual(self.orch.state(self.run), before)
            forged = new_event(self.run, "TARGET_RETIRED", "test-owner", dict(generation=proposal["descriptor"]["generation"],
                adoption_digest=proposal["digest"], reason="premature retirement"), occurred_at=self.orch._now())
            with self.assertRaises(TargetError):
                apply_event(copy.deepcopy(before), dict(forged, seq=before["last_seq"] + 1))
        self.orch.revoke_lease(self.run, assignment, WaitingReason(code="READINESS_STALE",
            detail="fixture prelaunch lease revoked", wake_condition="fresh admission",
            task_id=assignment["task_id"], box_id=assignment["agent_id"]))
        self.assertEqual(self.retire(proposal)["status"], "retired")
        self.assertEqual(project(self.fixture.store.read(self.run)), self.orch.state(self.run))

    def test_contract_unknowns_wrong_types_and_created_claim_are_rejected(self):
        changes = [((), "unknown", 1), ((), "schema_version", True), ((), "ownership", "created"),
                   (("provider",), "optional_api_field", "ignored?"), (("transport",), "kind", []),
                   (("transport",), "profile_digest", "not-a-digest"), ((), "label", "escape\x1b[0m")]
        for path, key, value in changes:
            candidate = copy.deepcopy(self.target)
            node = candidate
            for part in path:
                node = node[part]
            node[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                descriptor(candidate)

    def test_append_failure_and_concurrent_change_never_partially_adopt(self):
        proposal = self.proposal()
        state = self.orch.state(self.run)
        for error in (OSError("fixture disk failure"), ConcurrentAppendError("fixture concurrent update")):
            with patch.object(self.fixture.store, "append", side_effect=error):
                with self.assertRaises(type(error)):
                    self.adopt(proposal)
            self.assertEqual(self.orch.state(self.run), state)
        self.adopt(proposal)
        original = self.fixture.store.append
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("fixture response lost")
        with patch.object(self.fixture.store, "append", side_effect=lost_response):
            with self.assertRaises(OSError):
                self.retire(proposal)
        self.assertEqual(self.retire(proposal)["status"], "retired")

    def test_retirement_denies_unreconciled_lease_even_after_expiry(self):
        bundle, _ = self.fixture.admit()
        proposal = self.proposal(dict(self.target, target_id=bundle.binding.target_id))
        self.adopt(proposal)
        assignment = self.fixture.frame_assignment()
        self.orch.start_task(self.run, assignment)
        for elapsed in (0, 60):
            self.fixture.clock.advance(elapsed)
            with self.assertRaises(TargetError):
                self.retire(proposal)
        self.assertEqual(self.registry.inspect()["targets"][0]["status"], "adopted")

    def test_protected_material_new_redaction_and_bounded_snapshot(self):
        token = "target-private-credential-1234567890"
        value = dict(self.target, label=token)
        with patch.dict("os.environ", {"CAMOL_TARGET_TEST_TOKEN": token}):
            with self.assertRaises(TargetError):
                self.proposal(value)
        self.adopt(self.proposal(value))
        events = self.fixture.store.read(self.run)
        with patch.dict("os.environ", {"CAMOL_TARGET_TEST_TOKEN": token}):
            self.assertNotIn(token, json.dumps(self.registry.inspect()))
            self.assertEqual(project(events), self.orch.state(self.run))
        for offset, limit in ((True, 1), (0, True), (-1, 5), (0, 101)):
            with self.assertRaises(TargetError):
                self.registry.inspect(offset=offset, limit=limit)
        self.assertEqual(self.registry.inspect(offset=1)["targets"], [])

    def test_history_ceiling_terminal_run_and_concurrent_event_fence(self):
        proposal = self.proposal()
        with patch("camol.targets.MAX_TARGETS", 0):
            with self.assertRaises(TargetError):
                self.adopt(proposal)
        state = self.orch.state(self.run)
        event = new_event(self.run, "TARGET_ADOPTED", "test-owner", dict(proposal=proposal, approval_digest=proposal["digest"]), occurred_at=self.orch._now())
        with self.assertRaises(TargetError):
            apply_event(dict(state, terminal={"status": "cancelled"}), dict(event, seq=state["last_seq"] + 1))
        append = self.fixture.store.append
        other = self.proposal(dict(self.target, generation="generation-2"))
        def concurrent(*args, **kwargs):
            with patch.object(self.fixture.store, "append", append):
                self.adopt(other)
            return append(*args, **kwargs)
        with patch.object(self.fixture.store, "append", side_effect=concurrent):
            with self.assertRaises(ConcurrentAppendError):
                self.adopt(proposal)
        self.assertEqual(list(self.orch.state(self.run)["execution_targets"]), ["generation-2"])

    def test_public_embedding_and_offline_cli_inspection(self):
        with Harness(self.fixture.source, self.fixture.state_dir, database=self.fixture.store.path) as harness:
            self.assertIsInstance(harness.targets, TargetRegistry)
            value = harness.targets.propose(self.target, by="test-owner", expires_at="2099-01-01T00:00:00Z")
            harness.targets.adopt(value, by="test-owner", approval_digest=value["digest"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["target", "inspect", "--state-dir", str(self.fixture.state_dir), "--db", str(self.fixture.store.path), "--run-id", self.run])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["targets"][0]["proposal"], value)
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["target", "inspect", "--state-dir", str(self.fixture.state_dir), "--db", str(self.fixture.store.path),
                         "--run-id", self.run, "--plan-digest", "sha256:" + "f" * 64])
        self.assertEqual(code, 2)


class TargetSupervisorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = supervisor_fixture.SupervisorTests.asyncSetUp
    asyncTearDown = supervisor_fixture.SupervisorTests.asyncTearDown
    _wait_for = supervisor_fixture.SupervisorTests._wait_for

    async def test_live_cli_review_adopt_inspect_retire_and_control_denials(self):
        supervisor = Supervisor(supervisor_fixture.ROOT / "examples/three-agent-runbook.json", self.source, self.state)
        serving = asyncio.create_task(supervisor.serve())
        try:
            await self._wait_for(supervisor.paths.socket)
            await send_control(self.state, "drain")
            await send_control(self.state, "approve", requested_by="human-owner")
            state = supervisor.orchestrator.state(supervisor.run_id)
            target = dict(schema="camol.execution_target", schema_version=1, target_id="remote-1",
                generation="generation-1", control_plane_id="controller-1", ownership="adopted", label="Remote build host",
                provider=dict(kind="manual", account="owner", project="fixture", location="fixture", resource_id="host-1", resource_name="build"),
                transport=dict(kind="ssh", profile_digest="sha256:" + "a" * 64))
            descriptor_path, proposal_path = self.source / "target.json", self.source / "proposal.json"
            descriptor_path.write_text(json.dumps(target))

            async def cli(action, *args):
                arguments = [sys.executable, "-m", "camol", "target", action, "--live",
                    "--state-dir", str(self.state), "--db", str(supervisor.paths.database), "--run-id", state["run_id"],
                    "--plan-digest", state["plan_digest"]]
                if action != "inspect":
                    arguments += ["--workspace", str(self.source), "--by", "human-owner"]
                child = await asyncio.create_subprocess_exec(*arguments, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(child.communicate(), 10)
                    self.assertEqual(child.returncode, 0, err.decode()[:500])
                    return json.loads(out)
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()

            proposal = await cli("propose", "--descriptor", str(descriptor_path), "--expires-at", "2099-01-01T00:00:00Z")
            self.assertEqual((await cli("inspect"))["count"], 0)
            proposal_path.write_text(json.dumps(proposal))
            adopted = await cli("adopt", "--proposal", str(proposal_path), "--approval-digest", proposal["digest"])
            self.assertFalse(adopted["execution_authority"])
            self.assertEqual((await cli("inspect"))["count"], 1)
            for params, digest in ((dict(offset=0, limit=50, expected_database="/wrong"), state["plan_digest"]),
                                   (dict(offset=0, limit=50), "sha256:" + "f" * 64),
                                   (dict(offset=True, limit=50), state["plan_digest"])):
                with self.assertRaises(SupervisorError):
                    await send_control_v2(self.state, "target-inspect", requested_by="human-owner", params=params,
                        expected_run_id=state["run_id"], expected_plan_digest=digest)
            with self.assertRaises(SupervisorError):
                await send_control_v2(self.state, "target-retire", requested_by="strategist",
                    params=dict(generation="generation-1", adoption_digest=proposal["digest"], reason="deny", approved_by="human-owner"),
                    expected_run_id=state["run_id"], expected_plan_digest=state["plan_digest"])
            retired = await cli("retire", "--generation", "generation-1", "--adoption-digest", proposal["digest"], "--reason", "detach fixture")
            self.assertEqual(retired["status"], "retired")
            self.assertEqual(supervisor.orchestrator.state(state["run_id"])["total_tokens"], 0)
        finally:
            if not serving.done():
                await send_control(self.state, "stop", requested_by="human-owner")
            await asyncio.wait_for(serving, 5)
