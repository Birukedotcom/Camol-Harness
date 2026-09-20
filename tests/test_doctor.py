"""``camol doctor`` end to end: exit codes, report shape, read-only guarantees, redaction."""

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from camol.cli import main
from camol.doctor import EXIT_NOT_READY, EXIT_PROBE_FAILURE, EXIT_READY, DoctorOptions, run_doctor
from camol.probes import Probe, ProbeExecutionError, ProbeRegistry, default_registry
from camol.readiness import BoxBinding, ProbePolicy, ReadinessReceipt, WaitingReason, WorkspaceReceipt, AuthorityPolicy
from camol.runbook import migrate_runbook_v1_to_v2
from camol.schema import SchemaError

from tests.test_probes import HOSTILE_ENV, HOSTILE_STRINGS, git, make_repo

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-03T10:00:00Z"


def snapshot(root: Path):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if ".git" not in path.parts)


class DoctorFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = make_repo(self.root)
        self.state = self.root / "state"
        self.state.mkdir()
        self.runbook = self.repo / "examples/three-agent-runbook.json"

    def tearDown(self):
        self.temporary.cleanup()

    def doctor(self, registry=None, env=None, **overrides):
        options = dict(runbook=self.runbook, workspace=self.repo, state_dir=self.state, json_output=True, now=NOW, target_id="local:test")
        options.update(overrides)
        out = io.StringIO()
        report = run_doctor(DoctorOptions(**options), registry=registry, env=env if env is not None else HOSTILE_ENV, stdout=out)
        return report, out.getvalue()


class DoctorGreenPathTests(DoctorFixture):
    def test_documented_invocation_is_green_deterministic_and_read_only(self):
        before = snapshot(self.root)
        report, text = self.doctor()
        self.assertEqual(report.exit_code, EXIT_READY, text)
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "ready")
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["synthetic_clock"])
        self.assertEqual(payload["receipt_ttl_seconds"], 300)
        self.assertEqual(snapshot(self.root), before, "doctor must write nothing")
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=str(self.repo), stdout=subprocess.PIPE).stdout, b"")
        # Determinism: a second run with the same clock yields identical digests.
        second, text2 = self.doctor()
        first_digests = [(c["receipt_digest"], c["box_binding_digest"], c["probe_policy_digest"], c["authority_digest"]) for t in payload["tasks"] for c in t["candidates"]]
        second_digests = [(c["receipt_digest"], c["box_binding_digest"], c["probe_policy_digest"], c["authority_digest"]) for t in json.loads(text2)["tasks"] for c in t["candidates"]]
        self.assertEqual(first_digests, second_digests)
        self.assertTrue(all(digest[0] for digest in first_digests))

    def test_every_task_has_a_coherent_candidate_whose_records_validate(self):
        report, text = self.doctor()
        payload = json.loads(text)
        self.assertEqual({task["task_id"] for task in payload["tasks"]}, {"frame", "inventory", "challenge", "integrate"})
        for task in payload["tasks"]:
            self.assertTrue(task["ready"], task["task_id"])
            green = [c for c in task["candidates"] if c["coherent"]]
            self.assertTrue(green, task["task_id"])
            for candidate in green:
                receipt = ReadinessReceipt.from_dict(candidate["receipt"])
                binding = BoxBinding.from_dict(candidate["box_binding"])
                policy = ProbePolicy.from_dict(candidate["probe_policy"])
                workspace = WorkspaceReceipt.from_dict(candidate["workspace"])
                authority = AuthorityPolicy.from_dict(candidate["authority_policy"])
                self.assertEqual(receipt.digest(), candidate["receipt_digest"])
                self.assertEqual(receipt.box_binding_digest, binding.digest())
                self.assertEqual(receipt.probe_policy_digest, policy.digest())
                self.assertEqual(receipt.authority_digest, authority.digest())
                self.assertEqual(binding.workspace_digest, workspace.digest())
                self.assertEqual(receipt.status, "green")
                self.assertIsNone(receipt.reservation_id, "doctor receipts are pre-reservation")
                self.assertEqual(policy.coverage_errors(receipt), [])
                self.assertTrue(receipt.is_fresh("2026-09-03T10:04:59+00:00"))
                self.assertFalse(receipt.is_fresh("2026-09-03T10:05:00+00:00"))
                self.assertEqual(workspace.filesystem_policy, "shared_checkout_write")
                self.assertTrue(receipt.runtime_id.startswith("python3@"))
                # A green doctor is not a lease: grant and reservation are still missing,
                # and because this fixture uses --now the receipt is a synthetic fixture.
                still = {reason["code"] for reason in candidate["lease_preview"]["reasons"]}
                self.assertIn("APPROVAL_REQUIRED", still)
                self.assertIn("CAPACITY_EXHAUSTED", still)
                self.assertIn("POLICY_DENIED", still)
                self.assertNotIn("READINESS_STALE", still)
                self.assertEqual(candidate["clock"], "synthetic")
                self.assertFalse(candidate["evidence"])
                self.assertEqual(receipt.clock, "synthetic")
                self.assertFalse(receipt.is_evidence())

    def test_system_clock_run_yields_evidence_and_no_synthetic_denial(self):
        report, text = self.doctor(now=None)
        self.assertEqual(report.exit_code, EXIT_READY, text)
        payload = json.loads(text)
        self.assertEqual(payload["clock"], "system")
        self.assertTrue(payload["usable_evidence"])
        self.assertFalse(payload["synthetic_clock"])
        for task in payload["tasks"]:
            for candidate in task["candidates"]:
                if candidate["coherent"]:
                    self.assertTrue(candidate["evidence"])
                    receipt = ReadinessReceipt.from_dict(candidate["receipt"])
                    self.assertTrue(receipt.is_evidence())
                    still = {reason["code"] for reason in candidate["lease_preview"]["reasons"]}
                    self.assertNotIn("POLICY_DENIED", still)
                    self.assertEqual(still - {"APPROVAL_REQUIRED", "CAPACITY_EXHAUSTED", "WAITING_DEPENDENCY"}, set())

    def test_probe_policy_pins_definition_digests(self):
        _, text = self.doctor()
        payload = json.loads(text)
        by_id = {probe["probe_id"]: probe for probe in payload["probes"]}
        for task in payload["tasks"]:
            for candidate in task["candidates"]:
                for requirement in candidate["probe_policy"]["required_probes"]:
                    self.assertTrue(requirement["definition_digest"].startswith("sha256:"))
                    produced = by_id.get(requirement["probe_id"]) or next(p for p in candidate["agent_probes"] if p["probe_id"] == requirement["probe_id"])
                    self.assertEqual(produced["definition_digest"], requirement["definition_digest"], requirement["probe_id"])

    def test_informational_probes_are_flagged_and_excluded_from_the_policy(self):
        _, text = self.doctor()
        payload = json.loads(text)
        informational = {probe["probe_id"] for probe in payload["probes"] if probe["informational"]}
        self.assertEqual(informational, {"provider.connection", "network.policy"})
        for probe in payload["probes"]:
            if probe["informational"]:
                self.assertEqual(probe["status"], "unknown")
                self.assertFalse(probe["required"])
        for task in payload["tasks"]:
            for candidate in task["candidates"]:
                listed = {item["probe_id"] for item in candidate["probe_policy"]["required_probes"]}
                self.assertFalse(listed & informational)
        self.assertIn("proves nothing about hosted-model availability", payload["note"])

    def test_every_probe_entry_exposes_the_required_fields(self):
        _, text = self.doctor()
        payload = json.loads(text)
        entries = list(payload["probes"]) + [probe for task in payload["tasks"] for c in task["candidates"] for probe in c["agent_probes"]]
        for entry in entries:
            for field in ("probe_id", "kind", "target_id", "method", "command", "tool_version", "observed_at", "expires_at", "status", "reason", "missing_requirements", "summary", "evidence_digest", "result_digest", "required"):
                self.assertIn(field, entry, (entry["probe_id"], field))
            if entry["method"] == "process":
                self.assertTrue(entry["command"])
            if entry["status"] != "green":
                self.assertTrue(entry["reason"]["code"])
                self.assertTrue(entry["missing_requirements"])

    def test_text_output_shows_methods_missing_requirements_and_lease_gap(self):
        report, text = self.doctor(json_output=False)
        self.assertEqual(report.exit_code, EXIT_READY)
        self.assertIn("process: ", text)
        self.assertIn("[filesystem]", text)
        self.assertIn("informational; excluded from the probe policy", text)
        self.assertIn("missing: ", text)
        self.assertIn("lease predicate still requires: APPROVAL_REQUIRED, CAPACITY_EXHAUSTED", text)
        self.assertIn("verdict: ready (exit 0)", text)

    def test_v2_runbook_uses_the_frozen_ttl_and_rejects_a_conflicting_flag(self):
        raw = json.loads(self.runbook.read_text())
        v2 = migrate_runbook_v1_to_v2(raw, readiness_policy={"receipt_ttl_seconds": 120}, trust_tiers={a["id"]: "developer_trusted" for a in raw["agents"]})
        path = self.root / "v2.json"
        path.write_text(json.dumps(v2))
        report, text = self.doctor(runbook=path)
        self.assertEqual(report.exit_code, EXIT_READY, text)
        payload = json.loads(text)
        self.assertEqual(payload["receipt_ttl_seconds"], 120)
        self.assertEqual(payload["expires_at"], "2026-09-03T10:02:00.000000+00:00")
        with self.assertRaisesRegex(SchemaError, "conflicts with the frozen run.readiness_policy"):
            self.doctor(runbook=path, receipt_ttl_seconds=300)
        report, text = self.doctor(runbook=path, receipt_ttl_seconds=120)
        self.assertEqual(report.exit_code, EXIT_READY)

    def test_cli_entry_point_matches_the_build_plan_invocation(self):
        out = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(out):
            code = main(["doctor", str(self.runbook), "--workspace", str(self.repo), "--state-dir", str(self.state), "--now", NOW, "--target-id", "local:test"])
        self.assertEqual(code, EXIT_READY)
        self.assertIn("verdict: ready", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["doctor", str(self.runbook), "--workspace", str(self.repo), "--state-dir", str(self.state), "--json", "--now", NOW]), EXIT_READY)


class DoctorNotReadyTests(DoctorFixture):
    def assertTaskWait(self, payload, code):
        codes = {reason["code"] for task in payload["tasks"] for c in task["candidates"] for reason in c.get("waiting", [])}
        codes |= {reason["code"] for task in payload["tasks"] for reason in task.get("waiting", [])}
        self.assertIn(code, codes)

    def test_dirty_checkout_is_exit_2_workspace_conflict(self):
        (self.repo / "scratch.txt").write_text("dirty")
        report, text = self.doctor()
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "not_ready")
        self.assertTaskWait(payload, "WORKSPACE_CONFLICT")
        for task in payload["tasks"]:
            for candidate in task["candidates"]:
                self.assertIsNone(candidate["receipt_digest"])
                self.assertEqual(candidate["receipt"]["status"], "red")

    def test_state_dir_inside_repo_is_exit_2_policy_denied(self):
        report, text = self.doctor(state_dir=self.repo / ".camol")
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        self.assertTaskWait(json.loads(text), "POLICY_DENIED")

    def test_committed_symlink_state_dir_inside_repo_is_still_denied(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / "statelink").symlink_to(outside)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "link", cwd=self.repo)
        report, text = self.doctor(state_dir=self.repo / "statelink")
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        self.assertTaskWait(json.loads(text), "POLICY_DENIED")

    def test_missing_state_dir_is_exit_2_and_not_created(self):
        missing = self.root / "missing-state"
        report, text = self.doctor(state_dir=missing)
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        self.assertFalse(missing.exists())
        self.assertTaskWait(json.loads(text), "OPERATOR_ATTENTION")

    def test_workspace_trap_binary_is_never_launched_and_task_is_not_ready(self):
        trap = self.repo / "python3"
        trap.write_text("#!/bin/sh\necho LAUNCHED > \"$(dirname \"$0\")/LAUNCHED.sentinel\"\n")
        trap.chmod(trap.stat().st_mode | stat.S_IXUSR)
        raw = json.loads(self.runbook.read_text())
        for agent in raw["agents"]:
            agent["adapter"]["argv"] = ["./python3", "{packet}", "{result}"]
        self.runbook.write_text(json.dumps(raw))
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "trap", cwd=self.repo)
        report, text = self.doctor()
        self.assertFalse((self.repo / "LAUNCHED.sentinel").exists())
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        payload = json.loads(text)
        self.assertTaskWait(payload, "POLICY_DENIED")
        for task in payload["tasks"]:
            self.assertFalse(task["ready"])

    def test_unverified_hosted_adapter_cannot_be_ready_while_provider_and_network_are_unknown(self):
        # Reproduced round-3 case: adapter argv[0] "claude" on PATH but unverifiable -> must not exit 0.
        fake_claude = self.root / "bin" / "claude"
        fake_claude.parent.mkdir()
        fake_claude.write_text("#!/bin/sh\necho launched > \"{}\"\n".format(self.root / "CLAUDE.sentinel"))
        fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
        raw = json.loads(self.runbook.read_text())
        for agent in raw["agents"]:
            agent["adapter"]["argv"] = ["claude", "--print", "{packet}", "{result}"]
        self.runbook.write_text(json.dumps(raw))
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "hosted", cwd=self.repo)
        real_path = os.environ["PATH"]
        os.environ["PATH"] = str(fake_claude.parent) + os.pathsep + real_path
        try:
            report, text = self.doctor(now=None)
        finally:
            os.environ["PATH"] = real_path
        self.assertFalse((self.root / "CLAUDE.sentinel").exists())
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        payload = json.loads(text)
        for task in payload["tasks"]:
            self.assertFalse(task["ready"])
            for candidate in task["candidates"]:
                self.assertFalse(candidate["adapter_policy"]["verifiable_local_interpreter"])
                self.assertTrue(candidate["adapter_policy"]["requires_provider_proof"])
                required = {item["probe_id"] for item in candidate["probe_policy"]["required_probes"]}
                self.assertIn("provider.connection", required)
                self.assertIn("network.policy", required)
                codes = {reason["code"] for reason in candidate["waiting"]}
                self.assertIn("AUTH_REQUIRED", codes)
                self.assertIsNone(candidate["receipt_digest"])

    def test_task_without_eligible_worker_is_capacity_exhausted(self):
        raw = json.loads(self.runbook.read_text())
        raw["tasks"][0]["capabilities"] = ["quantum"]
        self.runbook.write_text(json.dumps(raw))
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "caps", cwd=self.repo)
        report, text = self.doctor()
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        payload = json.loads(text)
        frame = next(task for task in payload["tasks"] if task["task_id"] == "frame")
        self.assertEqual(frame["candidates"], [])
        self.assertEqual(frame["waiting"][0]["code"], "CAPACITY_EXHAUSTED")
        WaitingReason.from_dict(frame["waiting"][0])

    def test_git_missing_is_typed_exit_2_not_exit_3(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        python = shutil.which("python3")
        os.symlink(python, bin_dir / "python3")
        env = dict(HOSTILE_ENV, PATH=str(bin_dir))
        real_path = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir)
        try:
            report, text = self.doctor(env=env)
        finally:
            os.environ["PATH"] = real_path
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        self.assertTaskWait(json.loads(text), "NEEDS_DOWNLOAD")

    def test_unreachable_required_service_is_exit_2(self):
        report, text = self.doctor(services=("127.0.0.1:1",))
        self.assertEqual(report.exit_code, EXIT_NOT_READY)
        self.assertTaskWait(json.loads(text), "TARGET_UNREACHABLE")


class DoctorProbeFailureTests(DoctorFixture):
    def test_probe_failure_is_exit_3_with_a_report(self):
        class Exploding(Probe):
            probe_id = "exploding"
            kind = "test"

            def observe(self, context):
                raise ProbeExecutionError("boom " + HOSTILE_ENV["GITHUB_TOKEN"])

        registry = default_registry().register(Exploding())
        report, text = self.doctor(registry=registry)
        self.assertEqual(report.exit_code, EXIT_PROBE_FAILURE)
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "probe_failure")
        self.assertTrue(payload["errors"])
        self.assertNotIn(HOSTILE_ENV["GITHUB_TOKEN"], text)

    def test_all_shared_probes_failing_still_yields_exit_3_report(self):
        class Exploding(Probe):
            def __init__(self, probe_id):
                self.probe_id = probe_id
                self.kind = "test"

            def observe(self, context):
                raise ProbeExecutionError("boom")

        registry = ProbeRegistry()
        for name in ("a", "b"):
            registry.register(Exploding(name))
        report, text = self.doctor(registry=registry)
        self.assertEqual(report.exit_code, EXIT_PROBE_FAILURE)
        self.assertIn("errors", json.loads(text))


class DoctorRedactionTests(DoctorFixture):
    def test_hostile_environment_and_remote_never_reach_output(self):
        git("remote", "add", "origin", "https://user:p4ssw0rd@example.com/repo.git", cwd=self.repo)

        class Leaky(Probe):
            probe_id = "leaky"
            kind = "test"

            def observe(self, context):
                blob = " ".join(HOSTILE_STRINGS + list(HOSTILE_ENV.values()))
                return self.red(context, "leak " + blob, reason="AUTH_REQUIRED", wake="wake " + blob, missing=["m " + blob], facts={"x": blob})

        registry = default_registry().register(Leaky())
        for json_output in (True, False):
            report, text = self.doctor(registry=registry, json_output=json_output)
            for name, value in HOSTILE_ENV.items():
                if name in ("HARMLESS", "PATH"):
                    continue
                self.assertNotIn(value, text, name)
            for hostile in ("p4ssw0rd", "AKIAIOSFODNN7EXAMPLE", "correct-horse-battery", "MIIEow", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"):
                self.assertNotIn(hostile, text, hostile)
            self.assertIn("[REDACTED]", text)


if __name__ == "__main__":
    unittest.main()
