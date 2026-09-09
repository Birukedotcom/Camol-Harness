# Offline SWE-bench Verified adapter

`camol.swebench.SWEBenchVerifiedSuite` implements the campaign suite protocol for
an explicitly pinned offline cohort. It is an executable adapter, not a bundled
dataset, downloaded Docker image, preconfigured paid agent, or published score.
The tests run small protocol doubles, including a real Python subprocess. **No
official Docker task or paid model campaign has been validated by those tests.**

## Supported boundary

The first supported official harness revision is SWE-bench **v5.0.1**, commit
`87ab1f6ced28f75ba73ca899dc759b019310944a`. The operator supplies a clean local
checkout and a Python interpreter with its dependencies installed. The adapter
checks the checkout again immediately before launching each private subprocess.
Git callbacks are disabled through the shared safe-plumbing layer. Every tracked
`swebench` package file is independently hashed against its committed Git blob;
index/stat-cache claims do not prove its bytes. Imports run from an exclusive
copy of those verified bytes, without ignored files or stale `__pycache__` data.
It does not install dependencies, clone repositories, pull images, build images,
submit results, or acquire accounts.

Local JSON/JSONL ingestion accepts the original Verified row fields. Separate,
reviewed prepared rows must retain the original row unchanged and include the
official v5 evaluator fields (`image`, `eval_script`, `log_parser`, `eval_type`).
Nonempty binary/image assets and splits other than `test` are unsupported. This
keeps the first adapter limited to offline text/Python repository tasks. An older
raw dataset alone does not supply the v5 prepared evaluator or image.

This follows the official [dataset field contract](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/blob/main/README.md)
and pinned [v5 test specification](https://github.com/SWE-bench/SWE-bench/blob/87ab1f6ced28f75ba73ca899dc759b019310944a/swebench/types.py).

## Freeze an offline cohort

`freeze_offline_lock` is read-only. Its return value is a proposal to review and
persist, not an approval or an assertion that this host independently verified
download provenance. The full upstream dataset revision is owner-supplied; local
bytes, original and prepared rows, image identity, and policy are hashed.

```python
from camol.swebench import freeze_offline_lock, OfflineVerifiedDataset

lock = freeze_offline_lock(
    raw_dataset_path, prepared_dataset_path,
    dataset_revision=pinned_dataset_commit,
    images={
        instance_id: {
            "image_ref": immutable_registry_reference,  # repository@sha256:...
            "image_id": observed_local_image_id,       # sha256:... (Docker config ID)
            "public_tests_digest": pinned_baseline_tests_digest,
        },
    },
    environment={
        "architecture": "amd64", "network": "none",
        "memory_bytes": 8 * 1024**3, "cpu_millis": 2000,
        "pids_limit": 512,
    },
)
dataset = OfflineVerifiedDataset(raw_dataset_path, prepared_dataset_path, lock)
task_manifest = dataset.task(instance_id)
worker_input = dataset.public_task(instance_id)
```

The campaign uses `suite="swe-bench-verified"`, `suite_version=dataset_revision`,
the lock's dataset digest, and exact `dataset.task(...)` descriptors. Model,
version, effort, arm revisions, budgets, environment, seeds and selection are
still frozen by the standard campaign manifest. Every arm receives the same
baseline public-test digest and environment. The operator must review the
prepared evaluator; a hash guarantees identity, not evaluator correctness.

The worker view contains only `instance_id`, `repo`, `base_commit`, issue text and
repository version. It does not contain gold patches, test patches, reference
test identifiers, prepared evaluator scripts, evaluator paths or hints. All
these remain visible to the human in the separate protected host storage.

## Trusted arm execution

Register three trusted host executors, one for each campaign arm. An executor is
not an agent-generated Python plugin. It must independently materialize the exact
source/environment, isolate its workers from the oracle, other arms and host
secrets, and observe all work and provider usage. Workers return a candidate patch,
not a grading decision or a capability assertion.

`ArmCapabilities` requires kernel isolation, exact model-version and source/image
proof, and `budget_enforcement="provider_proxy"`: pre-request token and USD limits
must be enforced by the host provider boundary across **all** boxes/refinement.
Plain Claude/Codex CLI invocation and after-the-fact usage observation cannot
satisfy that declaration. There is currently **no bundled ready three-arm executor**
with those guarantees. The adapter fails preflight instead of pretending a
CLI-only arm supplies hard budgets or oracle isolation.

The registered host's `preflight(public_task, manifest, protected_paths)` must
return the exact arm/task/manifest/environment/public-test/executor/budget binding
with `ready=True`. `execute(public_task, arm, seed, manifest, request_digest)`
returns the patch and independently observed usage/recovery/tool evidence bound to
that digest. `teardown(trial_id)` drains only that trial's resources. These methods
are a trusted-host boundary, not proof that arbitrary Python code is honest.

```python
from camol.swebench import OfficialSWEBenchGrader, SWEBenchVerifiedSuite

grader = OfficialSWEBenchGrader(
    python=grader_python,
    harness_checkout=pinned_official_checkout,
    evidence_root=owner_only_evidence_directory,
)
suite = SWEBenchVerifiedSuite(dataset, grader, registered_host_executors)
# campaign is created through the normal reviewed, owner-approved manifest flow.
result = await campaign.run(suite)
```

## Grading and negative-control details

The private subprocess uses the pinned official `make_test_spec` and
`run_instance`, not a reimplementation of SWE-bench test parsing. Each grade has
an exclusive directory and an ID containing a candidate/request hash plus a fresh
nonce. This matters because official result caching does not include patch content;
a previous green report must not be reused for a changed candidate. See the
[official cache warning](https://www.swebench.com/SWE-bench/guides/evaluation/).

Gold is graded first, then an actual no-op is executed with `skip_patch=True`.
Using an empty public CLI predictions file is not equivalent: that path filters
empty patches without executing their tests. The pinned [official implementation](https://github.com/SWE-bench/SWE-bench/blob/87ab1f6ced28f75ba73ca899dc759b019310944a/swebench/harness/run_evaluation.py)
provides the direct negative-control interface. Preflight requires gold resolved,
at least one issue test failing on no-op, and no baseline regression on no-op.

The adapter verifies the protected on-disk report against the official returned
report, candidate/environment/request hashes, nonempty test output, and complete
nonoverlapping expected FAIL_TO_PASS/PASS_TO_PASS sets. `resolved` must agree with
those sets. Missing/malformed/infra evidence quarantines readiness or leaves a
trial requiring attention; it is not reported as a clean agent failure or a score.
The official [grading contract](https://github.com/SWE-bench/SWE-bench/blob/87ab1f6ced28f75ba73ca899dc759b019310944a/swebench/harness/grading.py)
remains the source of the report.

## Isolation, cleanup and limits

The concrete grader currently supports the local Unix Docker endpoint only. The
exact preinstalled image reference/config ID/Linux architecture is checked before
container start. No image pull is permitted. A narrow wrapper removes upstream
`SYS_ADMIN`, disables container networking, drops capabilities, enables
no-new-privileges and fixes CPU/memory/PID ceilings. Actual Docker readback must
match. This restricted environment is part of the task's environment digest;
tasks that need more privileges/network fail gold preflight rather than silently
receiving broader authority. Docker is not a VM security guarantee.

Official parsing/patch/test logic is unchanged. Cleanup is adapted to the Docker
API with exact per-invocation labels, avoiding upstream's unpinned `docker`
executable lookup. Only those labeled containers can be removed; never shared
images or a global prune. Python process cancellation is followed by separately
awaited container cleanup because killing the grader process alone does not stop
Docker work. Failure to drain is an explicit failure, with the private request and
logs retained for reconciliation.

Raw gold/no-op/test logs stay under the owner-only evidence root, never the worker
checkout or general campaign event body. Campaign records receive only grade and
artifact digests and normalized metrics. Source JSON and evaluator roots must be
denied by each arm's actual worker sandbox. File permissions by themselves do not
isolate processes running under the same account.

Before using public scores, run a small real pinned cohort on the actual isolated
benchmark host, verify gold/no-op and cleanup there, validate the registered
executors' budget/oracle enforcement, then run the frozen three-arm campaign.
The present fixture tests prove adapter behavior, not that any SWE-bench task was
solved, any score improved, or a statistical promotion is justified.
