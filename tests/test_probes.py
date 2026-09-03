"""Read-only probe registry: redaction, the execution guard, and individual probes."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from camol.probes import (
    REDACTED,
    AdapterBinaryProbe,
    ArtifactSinkProbe,
    CapacityProbe,
    CommandOutcome,
    DiskHeadroomProbe,
    EvaluatorBundleProbe,
    GitBinaryProbe,
    GuardedRunner,
    Probe,
    ProbeContext,
    ProbeExecutionError,
    ProbeRegistry,
    ProviderConnectionProbe,
    Redactor,
    RepositoryProbe,
    ServiceEndpointProbe,
    StateDirProbe,
    ToolsProbe,
    default_registry,
    run_command,
    runbook_commands,
    sanitize_identifier,
)
from camol.readiness import ProbeResult
from camol.runbook import load_runbook
from camol.schema import SchemaError


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)

HOSTILE_ENV = {
    "GITHUB_TOKEN": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "MY_PASSWORD": "hunter2-hunter2",
    "SOME_SECRET": "s3cr3t-value-here",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "HARMLESS": "public-value",
    "PATH": os.environ.get("PATH", ""),
}
HOSTILE_STRINGS = [
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "https://user:p4ssw0rd@example.com/repo.git",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA-abcdefghijklmnopqrstuvwxyz0123456",
    "xoxb-1234567890-abcdefghijklmnop",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "glpat-abcdefghijklmnopqrstuvwxyz",
    "api_key=sk-live-1234567890abcdefghijklmnop",
    "password: correct-horse-battery",
    "Cookie: session=abcdef0123456789; other=1",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "examples").mkdir(parents=True)
    shutil.copy(ROOT / "examples/three-agent-runbook.json", repo / "examples/three-agent-runbook.json")
    shutil.copy(ROOT / "examples/fake_agent.py", repo / "examples/fake_agent.py")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def make_context(repo: Path, state_dir: Path, runbook=None, **overrides) -> ProbeContext:
    values = dict(
        runbook=runbook or load_runbook(ROOT / "examples/three-agent-runbook.json"),
        workspace=repo,
        state_dir=state_dir,
        now=NOW,
        ttl_seconds=300,
        target_id="local:test",
        redactor=Redactor(HOSTILE_ENV),
    )
    values.update(overrides)
    return ProbeContext.guarded(**values)


class RedactorTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(HOSTILE_ENV)

    def test_secret_named_environment_values_are_removed_wherever_they_appear(self):
        for name, value in HOSTILE_ENV.items():
            if name in ("HARMLESS", "PATH"):
                continue
            for carrier in ("stdout {}", "argv --flag {}", "summary contains {} inside", "error: {}", '{{"facts": "{}"}}'):
                text = self.redactor.text(carrier.format(value))
                self.assertNotIn(value, text, (name, carrier))
                self.assertIn(REDACTED, text)
        self.assertIn("public-value", self.redactor.text("HARMLESS=public-value"))

    def test_known_token_shapes_and_headers_are_removed_even_without_env(self):
        clean = Redactor({})
        for hostile in HOSTILE_STRINGS:
            text = clean.text(hostile)
            self.assertIn(REDACTED, text, hostile)
        self.assertEqual(clean.text("https://user:p4ssw0rd@example.com/repo.git"), "https://[REDACTED]@example.com/repo.git")
        self.assertNotIn("p4ssw0rd", clean.text("https://user:p4ssw0rd@example.com/repo.git"))
        self.assertNotIn("dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", clean.text(HOSTILE_STRINGS[0]))
        self.assertNotIn("MIIEow", clean.text(HOSTILE_STRINGS[-1]))
        self.assertNotIn("abcdef0123456789", clean.text("Cookie: session=abcdef0123456789"))

    def test_argv_secret_flags_are_masked(self):
        argv = ["tool", "--token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "--api-key=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", "--password", "x", "--name", "ok"]
        redacted = self.redactor.argv(argv)
        self.assertEqual(redacted, ["tool", "--token", REDACTED, "--api-key=" + REDACTED, "--password", REDACTED, "--name", "ok"])

    def test_nested_facts_are_redacted(self):
        facts = {"remote": "https://a:b@h/x", "list": ["sk-abcdefghijklmnopqrstuvwxyz", {"deep": HOSTILE_ENV["MY_PASSWORD"]}], "n": 3}
        redacted = self.redactor.mapping(facts)
        blob = json.dumps(redacted)
        self.assertNotIn("a:b@", blob)
        self.assertNotIn("sk-abcdef", blob)
        self.assertNotIn("hunter2", blob)
        self.assertEqual(redacted["n"], 3)


class GuardedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = make_repo(root)
        self.state = root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _script(self, path: Path) -> Path:
        path.write_text("#!/bin/sh\necho LAUNCHED > \"$(dirname \"$0\")/LAUNCHED.sentinel\"\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_workspace_content_is_never_executed(self):
        script = self._script(self.repo / "python3")
        runner = GuardedRunner(run_command, [self.repo, self.state], [])
        for argv in ([str(script), "--version"], ["./python3", "--version"]):
            with self.assertRaisesRegex(ProbeExecutionError, "guarded root"):
                runner(argv, self.repo, 5)
        self.assertFalse((self.repo / "LAUNCHED.sentinel").exists())

    def test_state_dir_content_is_never_executed_even_through_a_symlink(self):
        script = self._script(self.state / "git")
        link = Path(self.temporary.name) / "elsewhere"
        link.symlink_to(self.state)
        runner = GuardedRunner(run_command, [self.repo, self.state], [])
        with self.assertRaisesRegex(ProbeExecutionError, "guarded root"):
            runner([str(link / "git"), "--version"], None, 5)
        self.assertFalse((self.state / "LAUNCHED.sentinel").exists())
        self.assertTrue(script.exists())

    def test_runbook_commands_are_never_executed(self):
        runbook = load_runbook(ROOT / "examples/three-agent-runbook.json")
        forbidden = runbook_commands(runbook, self.repo)
        runner = GuardedRunner(run_command, [self.repo, self.state], forbidden)
        adapter_argv = [item.replace("{workspace}", str(self.repo)) for item in runbook["agents"][0]["adapter"]["argv"]]
        with self.assertRaisesRegex(ProbeExecutionError, "runbook task command"):
            runner(adapter_argv, self.repo, 5)
        verification = runbook["tasks"][0]["verification"][0]["argv"]
        with self.assertRaisesRegex(ProbeExecutionError, "runbook task command"):
            runner(verification, self.repo, 5)

    def test_allowlisted_path_binary_runs(self):
        runner = GuardedRunner(run_command, [self.repo, self.state], [])
        outcome = runner([shutil.which("git"), "--version"], None, 10)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("git version", outcome.stdout)

    def test_missing_binary_is_a_probe_failure_not_a_readiness_fact(self):
        runner = GuardedRunner(run_command, [self.repo, self.state], [])
        with self.assertRaisesRegex(ProbeExecutionError, "not found on PATH"):
            runner(["definitely-not-a-binary-xyz", "--version"], None, 5)


class ProbeBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = make_repo(self.root)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "artifacts").mkdir()
        self.context = make_context(self.repo, self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_registry_is_all_green_on_a_clean_fixture_and_never_writes(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if ".git" not in path.parts)
        registry = default_registry()
        results = {probe.probe_id: probe.observe(self.context).result for probe in registry.shared_probes(self.context)}
        for agent in self.context.runbook["agents"]:
            for probe in registry.agent_probes(agent):
                results[probe.probe_id] = probe.observe(self.context).result
        required = {probe.probe_id for probe in registry.shared_probes(self.context) if probe.required}
        for probe_id in required:
            self.assertEqual(results[probe_id].status, "green", probe_id)
        self.assertEqual(results["provider.connection"].status, "unknown")
        self.assertEqual(results["network.policy"].status, "unknown")
        for result in results.values():
            ProbeResult.from_dict(json.loads(json.dumps(result.to_dict())))
            if result.method == "process":
                self.assertTrue(result.command)
            else:
                self.assertEqual(result.command, ())
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if ".git" not in path.parts)
        self.assertEqual(before, after, "probes must not create or remove files")
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=str(self.repo), stdout=subprocess.PIPE).stdout, b"")

    def test_adapter_probe_does_not_execute_workspace_scripts_named_like_interpreters(self):
        script = self.repo / "python3"
        script.write_text("#!/bin/sh\necho LAUNCHED > \"$(dirname \"$0\")/LAUNCHED.sentinel\"\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "trap", cwd=self.repo)
        agent = dict(self.context.runbook["agents"][0], adapter={"kind": "process", "argv": ["./python3", "{packet}", "{result}"], "timeout_seconds": 10})
        result = AdapterBinaryProbe(agent).observe(self.context).result
        self.assertFalse((self.repo / "LAUNCHED.sentinel").exists())
        self.assertNotEqual(result.status, "green")
        self.assertEqual(result.reason_code, "POLICY_DENIED")
        self.assertEqual(result.method, "filesystem")
        absolute = dict(agent, adapter={"kind": "process", "argv": ["{workspace}/python3", "{packet}", "{result}"], "timeout_seconds": 10})
        result = AdapterBinaryProbe(absolute).observe(self.context).result
        self.assertFalse((self.repo / "LAUNCHED.sentinel").exists())
        self.assertNotEqual(result.status, "green")

    def test_adapter_probe_reports_missing_script_and_ignores_non_path_arguments(self):
        agent = dict(self.context.runbook["agents"][0], adapter={"kind": "process", "argv": ["python3", "{workspace}/missing.py", "--model=org/name", "{packet}"], "timeout_seconds": 10})
        result = AdapterBinaryProbe(agent).observe(self.context).result
        self.assertEqual(result.status, "red")
        self.assertEqual(result.reason_code, "NEEDS_DOWNLOAD")
        self.assertEqual(len(result.missing_requirements), 1)
        self.assertTrue(result.missing_requirements[0].endswith("missing.py"))

    def test_adapter_probe_with_missing_interpreter_is_red_via_injected_which(self):
        context = make_context(self.repo, self.state, which=lambda name: None)
        result = AdapterBinaryProbe(self.context.runbook["agents"][0]).observe(context).result
        self.assertEqual(result.status, "red")
        self.assertIn("python3", result.missing_requirements)

    def test_git_missing_is_typed_not_a_probe_failure(self):
        context = make_context(self.repo, self.state, which=lambda name: None)
        for probe in (GitBinaryProbe(), RepositoryProbe()):
            result = probe.observe(context).result
            self.assertEqual(result.status, "red", probe.probe_id)
            self.assertEqual(result.reason_code, "NEEDS_DOWNLOAD")
            self.assertEqual(result.missing_requirements, ("git",))

    def test_dirty_checkout_is_workspace_conflict(self):
        (self.repo / "scratch.txt").write_text("dirty")
        result = RepositoryProbe().observe(self.context).result
        self.assertEqual(result.status, "red")
        self.assertEqual(result.reason_code, "WORKSPACE_CONFLICT")
        self.assertEqual(result.method, "process")
        self.assertIn("status", result.command)

    def test_non_git_and_subdirectory_workspaces_are_red(self):
        plain = self.root / "plain"
        plain.mkdir()
        result = RepositoryProbe().observe(make_context(plain, self.state)).result
        self.assertEqual((result.status, result.reason_code), ("red", "WORKSPACE_CONFLICT"))
        result = RepositoryProbe().observe(make_context(self.repo / "examples", self.state)).result
        self.assertEqual((result.status, result.reason_code), ("red", "WORKSPACE_CONFLICT"))
        self.assertIn("repository root", result.summary)

    def test_repository_probe_redacts_remote_credentials(self):
        git("remote", "add", "origin", "https://user:p4ssw0rd@example.com/repo.git", cwd=self.repo)
        outcome = RepositoryProbe().observe(self.context)
        self.assertNotIn("p4ssw0rd", json.dumps(outcome.result.to_dict()))
        self.assertNotIn("p4ssw0rd", json.dumps(outcome.facts))
        self.assertEqual(outcome.facts["repository_id"], "https://[REDACTED]@example.com/repo.git")

    def test_state_dir_nesting_is_rejected_lexically_and_resolved(self):
        inside = self.repo / "state"
        result = StateDirProbe().observe(make_context(self.repo, inside)).result
        self.assertEqual((result.status, result.reason_code), ("red", "POLICY_DENIED"))
        # Symlink inside the repo pointing outside: lexical path is inside -> rejected.
        outside = self.root / "outside"
        outside.mkdir()
        link = self.repo / "statelink"
        link.symlink_to(outside)
        result = StateDirProbe().observe(make_context(self.repo, link)).result
        self.assertEqual((result.status, result.reason_code), ("red", "POLICY_DENIED"))
        # Symlink outside pointing inside: resolved path is inside -> rejected.
        link2 = self.root / "looks-outside"
        link2.symlink_to(self.repo / ".camol-state")
        result = StateDirProbe().observe(make_context(self.repo, link2)).result
        self.assertEqual((result.status, result.reason_code), ("red", "POLICY_DENIED"))
        # Workspace inside state dir -> rejected.
        result = StateDirProbe().observe(make_context(self.repo, self.root)).result
        self.assertEqual((result.status, result.reason_code), ("red", "POLICY_DENIED"))

    def test_state_dir_in_git_common_dir_of_a_worktree_is_rejected(self):
        worktree = self.root / "wt"
        git("worktree", "add", "-q", str(worktree), "-b", "wt-branch", cwd=self.repo)
        common_state = self.repo / ".git" / "camol-state"
        result = StateDirProbe().observe(make_context(worktree, common_state)).result
        self.assertEqual((result.status, result.reason_code), ("red", "POLICY_DENIED"))

    def test_missing_state_dir_is_operator_attention_and_not_created(self):
        missing = self.root / "nope"
        result = StateDirProbe().observe(make_context(self.repo, missing)).result
        self.assertEqual((result.status, result.reason_code), ("red", "OPERATOR_ATTENTION"))
        self.assertFalse(missing.exists())
        self.assertTrue(result.missing_requirements)

    def test_artifact_sink_semantics(self):
        self.assertEqual(ArtifactSinkProbe().observe(self.context).result.status, "green")
        shutil.rmtree(self.state / "artifacts")
        absent = ArtifactSinkProbe().observe(self.context).result
        self.assertEqual(absent.status, "green")
        self.assertIn("absent", absent.summary)
        self.assertFalse((self.state / "artifacts").exists(), "doctor must not create the sink")
        (self.state / "artifacts").write_text("not a directory")
        broken = ArtifactSinkProbe().observe(self.context).result
        self.assertEqual((broken.status, broken.reason_code), ("red", "OPERATOR_ATTENTION"))

    def test_evaluator_bundle_digest_is_deterministic_and_launchability_is_not_execution(self):
        first = EvaluatorBundleProbe().observe(self.context)
        second = EvaluatorBundleProbe().observe(self.context)
        self.assertEqual(first.facts["evaluator_digest"], second.facts["evaluator_digest"])
        self.assertEqual(first.result.status, "green")
        self.assertEqual(first.result.method, "filesystem")
        context = make_context(self.repo, self.state, which=lambda name: None)
        red = EvaluatorBundleProbe().observe(context).result
        self.assertEqual((red.status, red.reason_code), ("red", "EVALUATOR_NOT_READY"))

    def test_tools_probe_names_missing_binaries(self):
        context = make_context(self.repo, self.state, which=lambda name: None)
        result = ToolsProbe().observe(context).result
        self.assertEqual((result.status, result.reason_code), ("red", "NEEDS_DOWNLOAD"))
        self.assertIn("python3", result.missing_requirements)

    def test_disk_and_capacity_probes(self):
        self.assertEqual(DiskHeadroomProbe().observe(self.context).result.status, "green")
        tight = make_context(self.repo, self.state, min_free_bytes=1 << 62)
        result = DiskHeadroomProbe().observe(tight).result
        self.assertEqual((result.status, result.reason_code), ("red", "CAPACITY_EXHAUSTED"))
        self.assertEqual(CapacityProbe().observe(self.context).result.status, "green")

    def test_provider_probe_is_unknown_and_informational_never_green(self):
        probe = ProviderConnectionProbe()
        self.assertFalse(probe.required)
        result = probe.observe(self.context).result
        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.wake_condition)
        self.assertTrue(result.missing_requirements)

    def test_service_probe_connect_and_close_only(self):
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            green = ServiceEndpointProbe("127.0.0.1:{}".format(port)).observe(self.context).result
            self.assertEqual((green.status, green.method), ("green", "socket"))
        finally:
            server.close()
        red = ServiceEndpointProbe("127.0.0.1:{}".format(port)).observe(self.context).result
        self.assertEqual((red.status, red.reason_code), ("red", "TARGET_UNREACHABLE"))
        with self.assertRaisesRegex(SchemaError, "HOST:PORT"):
            ServiceEndpointProbe("nonsense")

    def test_git_probe_failure_when_output_is_unparseable(self):
        def broken_runner(argv, cwd, timeout):
            return CommandOutcome(tuple(argv), 0, "", "")

        context = make_context(self.repo, self.state, runner=broken_runner)
        with self.assertRaisesRegex(ProbeExecutionError, "no parseable output"):
            GitBinaryProbe().observe(context)

    def test_hostile_values_never_reach_results_or_facts(self):
        class LeakyProbe(Probe):
            probe_id = "leaky"
            kind = "test"

            def observe(self, context):
                secrets = " ".join(HOSTILE_STRINGS + [HOSTILE_ENV["GITHUB_TOKEN"], HOSTILE_ENV["MY_PASSWORD"]])
                return self.red(context, "summary " + secrets, reason="AUTH_REQUIRED", wake="wake " + secrets, missing=["missing " + secrets], command=["tool", "--token", HOSTILE_ENV["GITHUB_TOKEN"], secrets], facts={"out": secrets, "nested": {"x": [secrets]}})

        outcome = LeakyProbe().observe(self.context)
        blob = json.dumps(outcome.result.to_dict()) + json.dumps(outcome.facts)
        for value in (HOSTILE_ENV["GITHUB_TOKEN"], HOSTILE_ENV["MY_PASSWORD"], "p4ssw0rd", "AKIAIOSFODNN7EXAMPLE", "MIIEow", "correct-horse-battery"):
            self.assertNotIn(value, blob, value)
        self.assertIn(REDACTED, blob)

    def test_registry_rejects_duplicate_ids_and_sanitizes_identifiers(self):
        registry = ProbeRegistry()
        registry.register(CapacityProbe())
        with self.assertRaisesRegex(SchemaError, "duplicate probe id"):
            registry.register(CapacityProbe())
        self.assertEqual(sanitize_identifier("my host.local"), "my-host.local")
        self.assertEqual(sanitize_identifier("***", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
