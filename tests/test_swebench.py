"""SWE-bench adapter/protocol fixtures, never public-suite or model scores."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from camol.benchmark import BenchmarkError
from camol.campaign import BenchmarkCampaign, CampaignStore
from camol.schema import canonical_digest
from camol.swebench import (ArmCapabilities, HARNESS_COMMIT, OfflineVerifiedDataset, OfficialSWEBenchGrader,
                           PUBLIC_FIELDS, SUITE, SWEBenchVerifiedSuite, _grade, bytes_digest, freeze_offline_lock, verify_harness_checkout)
from camol.swebench_bridge import GuardedDockerClient, cleanup, run_official
from tests.test_campaign import manifest


def fixture_files(root):
    row = dict(repo="fixture/repo", instance_id="fixture__repo-1", base_commit="a" * 40, patch="gold fix",
               test_patch="protected regression", problem_statement="Fix the addition regression", hints_text="excluded hint",
               created_at="2026-01-01", version="one", FAIL_TO_PASS=json.dumps(["test_bug"]), PASS_TO_PASS=json.dumps(["test_existing"]),
               environment_setup_commit="b" * 40, difficulty="under 15 minutes")
    prepared = dict(row, image="fixture:mutable", eval_script="protected evaluator", log_parser="fixture", eval_type="pass_and_fail")
    raw_path, prepared_path = root / "verified.json", root / "prepared.jsonl"
    raw_path.write_text(json.dumps([row]))
    prepared_path.write_text(json.dumps(prepared) + "\n")
    images = {row["instance_id"]: dict(image_ref="fixture/image@sha256:" + "d" * 64, image_id="sha256:" + "c" * 64,
                                    public_tests_digest=canonical_digest("baseline tests"))}
    environment = dict(architecture="amd64", network="none", memory_bytes=1024**3, cpu_millis=1000, pids_limit=128)
    lock = freeze_offline_lock(raw_path, prepared_path, dataset_revision="e" * 40, images=images, environment=environment)
    return raw_path, prepared_path, lock


def official_report(request, accepted=None):
    if accepted is None:
        accepted = request["patch"] == "gold fix" and request["purpose"] != "noop"
    return {request["task"]["task_id"]: dict(resolved=accepted, patch_successfully_applied=True,
        patch_is_None=False, patch_exists=True, infra_failure=False,
        tests_status={"FAIL_TO_PASS": dict(success=["test_bug"] if accepted else [], failure=[] if accepted else ["test_bug"]),
                      "PASS_TO_PASS": dict(success=["test_existing"], failure=[])})}


def grade_receipt(request):
    report = official_report(request)
    return dict(schema="camol.swebench_grade", schema_version=1, request_digest=canonical_digest(request),
                report=report, report_digest=canonical_digest(report), test_output_digest=bytes_digest(b"real log fixture"),
                environment_digest=request["task"]["environment_digest"], candidate_digest=request["candidate_digest"],
                invocation_id="fixture-" + canonical_digest(request)[7:30], elapsed_ms=1, cleanup_complete=True)


class FixtureGrader:
    protected_paths = ()
    def __init__(self):
        self.calls = []
        self.mutate = None

    async def grade(self, request, *, timeout_seconds):
        self.calls.append(copy.deepcopy(request))
        receipt = grade_receipt(request)
        if self.mutate:
            self.mutate(receipt)
        return receipt

    async def teardown(self, trial_id):
        pass


class FixtureExecutor:
    capabilities = ArmCapabilities("protocol-fixture", canonical_digest("trusted fixture"), "provider_proxy", "kernel", True, True)
    def __init__(self, arm):
        self.arm, self.calls = arm, []

    async def preflight(self, public, plan, protected_paths):
        assert set(public) == set(PUBLIC_FIELDS)
        task = next(item for item in plan["tasks"] if item["task_id"] == public["instance_id"])
        return dict(arm=self.arm, task_digest=task["task_digest"], manifest_digest=canonical_digest(plan),
                    environment_digest=task["environment_digest"], public_tests_digest=task["public_tests_digest"],
                    executor_digest=self.capabilities.implementation_digest, budget_digest=canonical_digest(plan["budgets"]), ready=True)

    async def execute(self, public, arm, seed, plan, request_digest):
        assert set(public) == set(PUBLIC_FIELDS)
        assert "protected regression" not in json.dumps(public)
        self.calls.append((public, arm, seed))
        return dict(request_digest=request_digest, patch="gold fix", tokens=5, cost_usd_micros=0, retries=0,
                    human_interventions=0, tool_calls=1, invariant_violations=0, recovery_success=True,
                    usage_observed=True, evidence_complete=True, artifact_manifest_digest=canonical_digest("host artifacts"))

    async def teardown(self, trial_id):
        pass


class SWEBenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw, self.prepared, self.lock = fixture_files(self.root)
        self.dataset = OfflineVerifiedDataset(self.raw, self.prepared, self.lock)
        self.task = self.dataset.task("fixture__repo-1")
        self.plan = manifest()
        self.plan.update(suite=SUITE, suite_version=self.lock["dataset_revision"], dataset_digest=self.lock["dataset_digest"], tasks=[self.task])
        self.grader = FixtureGrader()
        self.executors = {arm: FixtureExecutor(arm) for arm in self.plan["arms"]}
        self.suite = SWEBenchVerifiedSuite(self.dataset, self.grader, self.executors)

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_lock_and_worker_view_do_not_expose_oracle(self):
        self.assertEqual(self.lock["harness_commit"], HARNESS_COMMIT)
        public = self.dataset.public_task(self.task["task_id"])
        self.assertEqual(set(public), set(PUBLIC_FIELDS))
        self.assertNotIn("gold fix", str(public))
        self.assertNotIn("protected regression", str(public))
        public["problem_statement"] = "changed copy"
        self.assertNotEqual(public, self.dataset.public_task(self.task["task_id"]))
        changed = copy.deepcopy(self.lock)
        changed["instances"][0]["image_ref"] = "fixture:latest"
        with self.assertRaises(BenchmarkError):
            OfflineVerifiedDataset(self.raw, self.prepared, changed)
        self.raw.write_text(self.raw.read_text().replace("gold fix", "changed fix"))
        with self.assertRaisesRegex(BenchmarkError, "changed"):
            OfflineVerifiedDataset(self.raw, self.prepared, self.lock)

    def test_duplicate_rows_json_keys_symlinks_and_prepared_drift_rejected(self):
        self.raw.write_text('[{"instance_id":"fixture__repo-1","instance_id":"other"}]')
        with self.assertRaises(BenchmarkError):
            OfflineVerifiedDataset(self.raw, self.prepared, self.lock)
        self.raw.unlink()
        self.raw.symlink_to(self.prepared)
        with self.assertRaises(BenchmarkError):
            OfflineVerifiedDataset(self.raw, self.prepared, self.lock)

    async def test_complete_three_arm_campaign_is_protocol_evidence_not_public_score(self):
        store = CampaignStore(self.root / "campaign.sqlite3")
        try:
            campaign = BenchmarkCampaign.create(store, self.plan, approved_by="owner", manifest_digest=canonical_digest(self.plan))
            state = await campaign.run(self.suite)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(self.grader.calls), 8)  # actual adapter calls: gold + no-op + six candidate grades
            self.assertEqual([item["purpose"] for item in self.grader.calls[:2]], ["gold", "noop"])
            self.assertFalse(campaign.report()["statistical_claim"])
            self.assertNotIn("gold fix", str(store.events(self.plan["campaign_id"])))
            self.assertNotIn("protected regression", str(store.events(self.plan["campaign_id"])))
        finally:
            store.close()

    async def test_cli_only_cannot_claim_hard_budget_or_oracle_isolation(self):
        self.executors["claude_direct"].capabilities = ArmCapabilities("cli-only", canonical_digest("cli"), "observed_cli", "none", True, True)
        with self.assertRaisesRegex(BenchmarkError, "CLI-only"):
            await self.suite.validate_gold(self.task, self.plan)
        self.assertEqual(self.grader.calls, [])
        self.assertTrue(all(not executor.calls for executor in self.executors.values()))

    async def test_false_or_missing_noop_execution_quarantines_without_model_spend(self):
        def fake_noop(receipt):
            receipt["report"][self.task["task_id"]]["infra_failure"] = True
            receipt["report_digest"] = canonical_digest(receipt["report"])
        self.grader.mutate = fake_noop
        with self.assertRaisesRegex(BenchmarkError, "infrastructure"):
            await self.suite.validate_gold(self.task, self.plan)
        self.assertTrue(all(not executor.calls for executor in self.executors.values()))
        with self.assertRaisesRegex(BenchmarkError, "preflight"):
            await self.suite.run_trial(self.task, "camol_one", 42, self.plan)

    def test_candidate_environment_report_and_test_set_tampering_fail_closed(self):
        request = self.dataset.grade_request(self.task["task_id"], "gold fix", "candidate", canonical_digest(self.plan))
        for mutate in (lambda r: r.update(candidate_digest=canonical_digest("other")),
                       lambda r: r.update(environment_digest=canonical_digest("other")),
                       lambda r: r.update(cleanup_complete=False),
                       lambda r: r["report"][self.task["task_id"]].update(resolved=False),
                       lambda r: r["report"][self.task["task_id"]]["tests_status"]["FAIL_TO_PASS"].update(success=[])):
            receipt = grade_receipt(request)
            mutate(receipt)
            receipt["report_digest"] = canonical_digest(receipt["report"])
            with self.assertRaises(BenchmarkError):
                _grade(receipt, request)

    async def test_real_subprocess_official_interface_fixture_runs_empty_noop(self):
        # This runs the bridge function through a real interpreter, substituting
        # only Docker/official dependencies with tiny explicit protocol doubles.
        request = self.dataset.grade_request(self.task["task_id"], "", "noop", canonical_digest(self.plan))
        packet = dict(request=request, harness_checkout="unused-fixture", invocation_id="grade-protocol-noop", timeout_seconds=2)
        path = self.root / "request.json"
        path.write_text(json.dumps(packet))
        code = "import sys; sys.path.insert(0, sys.argv[1]); from tests.test_swebench import bridge_fixture; bridge_fixture(sys.argv[2])"
        completed = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code,
                    str(Path(__file__).resolve().parents[1]), str(path), cwd=self.root,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await completed.communicate()
        self.assertEqual(completed.returncode, 0, stderr.decode())
        receipt = json.loads(stdout)
        self.assertFalse(_grade(receipt, request)["accepted"])
        self.assertEqual(receipt["request_digest"], canonical_digest(request))
        self.assertTrue((self.root / "logs/evaluation/grade-protocol-noop/camol-candidate/fixture__repo-1/test_output.txt").is_file())

    async def test_subprocess_cancellation_still_awaits_exact_cleanup(self):
        class WaitingGrader(OfficialSWEBenchGrader):
            def _verify_pin(self):
                pass  # explicit boundary double; no official/Docker execution

            async def _invoke(self, directory, mode, timeout):
                self.calls.append(mode)
                if mode == "grade":
                    self.started.set()
                    await asyncio.Event().wait()

        grader = WaitingGrader(python=sys.executable, harness_checkout=self.root, evidence_root=self.root / "private")
        grader.calls, grader.started = [], asyncio.Event()
        request = self.dataset.grade_request(self.task["task_id"], "", "noop", canonical_digest(self.plan))
        running = asyncio.create_task(grader.grade(request, timeout_seconds=1))
        await grader.started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(grader.calls, ["grade", "cleanup"])

    def test_docker_image_pin_and_privilege_readback_are_enforced(self):
        request = self.dataset.grade_request(self.task["task_id"], "gold fix", "gold", canonical_digest(self.plan))
        packet = dict(request=request, harness_checkout="unused", invocation_id="grade-docker-policy", timeout_seconds=2)
        client = FakeDocker()
        proxy = GuardedDockerClient(client, packet)
        client.image = SimpleNamespace(id=request["image_id"], attrs=dict(Architecture="amd64", Os="linux"))
        self.assertEqual(proxy.images.get(request["prepared"]["image"]), client.image)
        client.image.id = "sha256:" + "1" * 64
        with self.assertRaises(BenchmarkError):
            proxy.images.get(request["prepared"]["image"])
        with self.assertRaises(BenchmarkError):
            proxy.images.pull("anything")
        name = "sweb.eval.fixture__repo-1.grade-docker-policy"
        container = proxy.containers.create(image=request["prepared"]["image"], name=name, user="root", detach=True,
                                            command="tail -f /dev/null", cap_add=["SYS_ADMIN"])
        self.assertEqual(container.attrs["HostConfig"]["NetworkMode"], "none")
        self.assertEqual(client.created["cap_add"], [])
        client.tamper = True
        with self.assertRaisesRegex(BenchmarkError, "readback"):
            proxy.containers.create(image=request["prepared"]["image"], name=name)
        cleanup(client, packet)
        self.assertFalse(client.items)

    def test_official_pin_never_executes_clean_filter_or_trusts_assume_unchanged(self):
        repo = self.root / "official-fixture"
        source = repo / "swebench" / "harness" / "run_evaluation.py"
        source.parent.mkdir(parents=True)
        source.write_text("ANSWER = 1\n")
        (repo / ".gitattributes").write_text("*.py filter=trap\n")
        (repo / ".gitignore").write_text("__pycache__/\n")
        def git(*args):
            return subprocess.run(["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "protocol fixture")
        revision = git("rev-parse", "HEAD")
        marker = self.root / "filter-was-executed"
        git("config", "filter.trap.clean", "touch '" + str(marker) + "'; cat")
        git("config", "filter.trap.required", "true")
        original = source.stat()
        snapshot = self.root / "clean-snapshot"
        cache = source.parent / "__pycache__"
        cache.mkdir()
        (cache / "run_evaluation.cpython-312.pyc").write_bytes(b"ignored poisoned bytecode")
        verify_harness_checkout(repo, snapshot=snapshot, expected_commit=revision)
        self.assertFalse(marker.exists())
        self.assertFalse((snapshot / "swebench/harness/__pycache__").exists())
        self.assertEqual((snapshot / "swebench/harness/run_evaluation.py").read_text(), "ANSWER = 1\n")
        git("-c", "filter.trap.clean=", "-c", "filter.trap.required=false", "update-index", "--assume-unchanged", "swebench/harness/run_evaluation.py")
        source.write_text("ANSWER = 9\n")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(BenchmarkError):
            verify_harness_checkout(repo, expected_commit=revision)
        self.assertFalse(marker.exists())
        git("-c", "filter.trap.clean=", "-c", "filter.trap.required=false", "update-index", "--no-assume-unchanged", "swebench/harness/run_evaluation.py")
        # Even if Git claims stat-cache cleanliness, direct blob comparison
        # rejects a same-sized, restored-mtime tracked source mutation.
        with self.assertRaises(BenchmarkError):
            verify_harness_checkout(repo, expected_commit=revision)
        self.assertFalse(marker.exists())


class FakeDocker:
    def __init__(self):
        self.containers, self.images = self, self
        self.items, self.created, self.tamper = [], {}, False
        self.image = None

    def get(self, name):
        return self.image

    def list(self, all=False, filters=None):
        return list(self.items)

    def create(self, **kwargs):
        self.created = kwargs
        host = dict(NetworkMode="bridge" if self.tamper else "none", Memory=kwargs["mem_limit"], NanoCpus=kwargs["nano_cpus"],
                    PidsLimit=kwargs["pids_limit"], Privileged=False, Binds=[], CapAdd=kwargs["cap_add"], CapDrop=kwargs["cap_drop"],
                    SecurityOpt=kwargs["security_opt"])
        item = SimpleNamespace(attrs=dict(Image="sha256:" + "c" * 64, Config=dict(Labels=kwargs["labels"]), HostConfig=host), reload=lambda: None)
        item.remove = lambda force: self.items.remove(item)
        self.items.append(item)
        return item


def bridge_fixture(path):
    packet = json.loads(Path(path).read_text())
    request = packet["request"]
    client = FakeDocker()
    def execute(spec, prediction, guarded_client, run_id, *, timeout, rewrite_reports, skip_patch, task_repo):
        assert skip_patch is True and prediction["model_patch"] == "" and not rewrite_reports
        actual = subprocess.run([sys.executable, "-I", "-c", "assert 1 + 1 == 3"], capture_output=True)
        assert actual.returncode != 0
        report = official_report(request, accepted=False)
        output = Path.cwd() / "logs" / "evaluation" / run_id / "camol-candidate" / request["task"]["task_id"]
        output.mkdir(parents=True)
        (output / "report.json").write_text(json.dumps(report))
        (output / "test_output.txt").write_bytes(actual.stderr)
        return request["task"]["task_id"], report
    official = SimpleNamespace(make_test_spec=lambda prepared: prepared, run_instance=execute)
    receipt = run_official(packet, Path.cwd(), client, official)
    print(json.dumps(receipt))
