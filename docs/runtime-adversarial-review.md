# Runtime adversarial review — 2026-09-07

Reviewed the product V0 runtime from `d6cbfae` in an isolated worktree. The checks
used temporary Git repositories and deterministic process workers; they used no
provider inference, production credentials, deployments, or user repository edits.

## Reproduced defects and corrections

| Defect | Reproduction | Correction |
| --- | --- | --- |
| A long turn outlives its 90-second fence; both result ingestion and retry raise `the active lease fence is expired`, leaving a running task that cannot resume. | `tests/test_runtime_resilience.py` advances the clock during a running turn, then retries recovery. | A watchdog obtains fresh read-only admission proof before expiry and records `LEASE_AUTHORIZATION_REFRESHED`. The original packet/fence identity remains stable. Failed reproof cancels the process, retains a salvage receipt, and produces a typed waiting state. |
| A provider-capability receipt can expire before the generic readiness TTL. | Prepare hosted readiness one second before the fake provider capability expires. | Provider probes and candidate doctor/admission receipts are clipped to the weakest capability expiry. No paid preflight is run automatically. |
| A new runner cannot restore an in-flight verification because its in-memory workspace handle map is empty. | Submit a candidate, construct a new runner, and resume verification. | Verification reloads its persisted worktree handle. Verification receipts also bind the lease and fence, preventing an older attempt's passing result from completing a later attempt. |
| The scheduler waits for an entire batch of workers even after an independent slot becomes free. | A fast task enables a child while an unrelated worker sleeps five seconds. | The scheduler admits newly ready DAG nodes after each completion; the child starts before the unrelated slow task finishes. |
| Writing a large prompt to a child that does not consume stdin bypasses the subprocess deadline; cancellation leaves a live process and active invocation record. | Supply two MiB to a sleeping child with a 0.2-second deadline, then repeat with cancellation. | The deadline and cancellation cleanup cover stdin backpressure, process completion, and output capture. Both paths reap the process group and persist a terminal invocation record. |
| An unexpected supervisor driver exception leaves an apparently running daemon with no scheduler. | A driver error propagates out of `run_until_terminal`. | The daemon remains inspectable in `operator_attention`, with a redacted `last_error`, and can retry through explicit resume. |
| Read-only Git status/diff or worktree checkout can execute repository-configured clean/smudge/process filters. | `tests/test_git_safety.py` uses a trap script, tracked attributes, same-size/mtime mutation, and a conditional worktree include. | Central Git plumbing enumerates callback names without executing them and disables all effective filter/diff drivers. New worktrees are created without checkout, then materialized under their own inspected config. Malformed driver names and masked index flags fail closed. |
| A benchmark reconciliation or cleanup can race the still-running trial owner and poison replay or remove a live environment. | `tests/test_campaign.py` holds a plugin trial open while a second controller attempts owner actions. | A database/campaign-scoped process lock covers execution, recovery, cleanup and reconciliation. Every appended transition is projected before insertion. Known overspend is retained as a failed, promotion-ineligible result. |
| An embedded harness can acquire two different state-directory locks for one external database. | Open one database from two state directories. | The embedded API, like the daemon, requires its database inside the exact leader-locked state directory. |
| An isolated Python venv launches through intermediate symlinks and a non-system standard library that the sandbox cannot read. | Run the destructive sandbox fixture using a UV-managed Python3.12 venv. | Explicit runtime read roots include the bounded executable symlink chain and adjacent runtime libraries/config. The source remains unwritable; neither `/` nor the user's entire home becomes a read root. |
| Worker and evaluator policies granted writes to the full task packet directory, including trusted accounting and process records. | An actual sandboxed process attempts both overwrite and unlink of packets, provider result caches, charge journals and invocation logs. | Process workers write only a dedicated untrusted output directory plus their worktree; stdout provider workers and evaluators receive no packet-directory write grant. Validated results are copied into a separate control-owned cache. All ten trap operations are denied while legitimate result output and controller logging succeed. |
| Pre/post evaluator hashes miss an oracle changed temporarily and restored before process exit. | A verifier registers an exit handler restoring an oracle, changes the expected value, and reads the changed value. | Hardened verification uses sandbox policy v2: its entire candidate source is OS-read-only, with a separate writable scratch directory exposed through TMPDIR. Explicit read-only exclusions also protect ancestor directory entries against replacement/rename. The trusted/unenforced tier is labeled `hash_checks_only_unenforced`, not presented as equivalent isolation. |
| Git inside the worker sandbox cannot inspect a linked worktree without its external common metadata, while granting that metadata would expose credentialed config and other boxes' objects. | Run real sandboxed `git status` and `git diff`, then try reading included source config and modifying shared/private Git metadata. | Admission builds a pinned private shallow inspection view: exactly the approved commit plus reachable tree/blob objects, sanitized config and an independent index. No remote, shared store, alternate, earlier commit or unreachable object is copied. A separate worker scratch grant supplies TMPDIR. Real status/diff succeeds; shared config access and inspection-view mutations are denied. |
| Integration receipts hash raw checks, but invariant gates hash redacted persisted evidence. Additional sandbox-policy metadata exposes the mismatch and blocks successful V5/V6 runs. | Execute capacity/revision fixtures with complete verifier policy evidence. | Receipt and counterexample digests now use the same normalized check data as persisted command evidence. Exact replay and gate binding remain strict; redaction is not bypassed. |
| Git replacement refs preserve an apparent approved commit ID while substituting another tree/code during materialization or pinned-source inspection. | Create `refs/replace` mapping the approved commit to a different tree; the old runtime materializes substituted bytes under the approved receipt ID. | Every centralized Git command and sanitized Git environment disables replacement objects. Guarded-probe, worktree and private-inspection regressions all read the original approved objects, not the replacement. |

Trusted observer evidence, including paid usage arriving after expiry, can still be
recorded against the exact current lease. This preserves accounting without accepting
worker claims or permitting an expired task to advance its verification/completion
gate. A new lease epoch cannot reuse the previous authorization.

Schema5 manual candidate/integration gates keep a task visibly verifying while
other runnable boxes continue. Two 301-second simulated waits are covered by a
restart regression: fresh verification-only authorization resumes the exact
candidate without another worker turn, and the integration commit reviewed by the
owner is the same one later accepted. Changed integration bases invalidate that
approval rather than reusing it for a different artifact.

Provider execution additionally pins file-backed model/tool/budget profiles to
the original admission probe digest. Claude and Codex use immutable prelaunch
invocation journals; cancelled, malformed and uncertain calls retain accounting,
and unknown charges stop further paid launches. See [Codex workers](codex-workers.md)
for the weaker Codex capability tier and explicit unsupported guarantees.

Private Git inspection is bounded to 100,000 objects and 64 MiB uncompressed object
bytes per task baseline. It supports status, diff and one-commit shallow inspection,
not shared branch mutation, commits, remotes or full history. Objects from other
tasks and older commits are excluded even when present in the common repository.
Existing admissions without the pinned inspection capability are denied before
launch; they require an explicit owner-approved migration. This does not turn
`developer_trusted` into an OS-isolated tier. The controlled inspection view is
read-only only in an enforcing sandbox.

## Repeatable runtime soak

Run from the repository:

```bash
python3 scripts/run_runtime_soak.py --iterations 3 --boxes 4
```

Each trial creates its own source repository, executes a nine-task DAG across four
workers, checks dependent source outputs in the final integration, verifies the source
checkout and HEAD remain unchanged, and exports and replays the full ledger. Every
other trial interrupts at verification, closes/reopens SQLite, and constructs fresh
orchestrator and runner instances before recovery. The script emits JSON and deletes
its temporary repositories after verification.

The initial run passed all 27 tasks. Trial durations were 24.588, 20.937, and 25.209
seconds. Event counts were 258, 257, and 258; artifact counts were 29, 28, and 29. All
three had exact exported-state replay equality and an unchanged source checkout.
These timings characterize deterministic local fixtures, not hosted-agent speed or
model capability.

Focused regression commands:

```bash
python3 -m unittest tests.test_runtime_resilience tests.test_sandbox tests.test_hosted_admission -v
python3 -m unittest tests.test_admission_scheduler tests.test_runner tests.test_evaluation tests.test_supervisor -v
python3 -m unittest tests.test_git_view tests.test_git_safety tests.test_capacity_runtime tests.test_revisions -v
```

## Boundaries still requiring separate evidence

Paid provider/model runs, real revoked-account behavior, distributed workers, remote
effects, and a real overnight workload are not established by this fixture soak.
The unsandboxed process backend remains `developer_trusted`; Git worktrees do not
isolate host filesystem access. OS process-group termination covers normal child
processes, not a hostile process deliberately escaping into another session. Strict
containment of arbitrary daemonizing workloads requires an enforcing container or
OS job boundary in addition to the current local process-group lifecycle.

`tests.test_process_descendants` preserves a bounded macOS characterization: a
worker can fork, enter a new session, close its inherited pipes, and continue
writing its old task checkout after the parent's invocation is recorded complete.
The child self-terminates within 2.5 seconds. Seatbelt rejects its writes to sibling
verifier/integration generations and frozen controller evaluator data; independently
materialized, hash-addressed candidates remain unchanged. This demonstrates no
accepted-candidate or frozen-oracle bypass in that fixture, not complete descendant
containment. A later retry reusing the old task checkout could still be disturbed
by such a survivor. Per-invocation writable generations and an OS-owned descendant
boundary are needed before claiming full lease-time process revocation. A process
group or best-effort process-tree scan alone does not establish that stronger claim.
