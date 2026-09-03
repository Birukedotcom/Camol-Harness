# Camol v0 build plan

Status: implementation-ready plan. The current code is a deterministic kernel proof;
this plan defines the shortest path to a readiness-safe, isolated, real-agent dogfood
loop.

Target date and vendor details in this document are observations as of 2026-09-03.
Provider facts are adapter compatibility data, not permanent kernel constants.

## 1. Outcome and non-negotiable boundary

The first product-iterable release is one complete local run in which Camol:

1. freezes a human-approved plan and evaluator bundle;
2. proves that the control plane and a task-specific box are ready;
3. reserves capacity and creates an isolated workspace;
4. leases one bounded task to one real agent;
5. records the requested and resolved model, commands, tool calls, outputs, changes,
   checkpoints, resource use, and evidence;
6. runs verification outside the builder's authority;
7. returns failed evidence to refinement instead of calling the task complete; and
8. survives interruption without losing accepted work or repeating an unsafe effect.

Camol must not start work because a worker is registered, connected, or idle. The
launch invariant is:

```text
READY_TO_LEASE =
  PLAN_FROZEN
  AND CONTROL_PLANE_READY
  AND TASK_DEPENDENCIES_GREEN
  AND BOX_READINESS_FRESH
  AND EVALUATOR_READY
  AND AUTHORITY_GRANTED
  AND CAPACITY_RESERVED
```

The task lease binds the hashes of the plan, evaluator bundle, workspace receipt,
readiness receipt, capability grant, and reservation. The runner rechecks them
immediately before process launch. A missing, red, expired, or changed input prevents
launch and creates a visible typed waiting state.

## 2. What isolation means

"Isolated" is not one boolean. Camol records and enforces independent isolation
layers:

| Layer | v0 requirement |
|---|---|
| Source | Every write-capable task receives a dedicated Git worktree and task branch. The user's checkout and `main` are never the worker cwd. |
| Process | A real worker launches through a sandbox backend with an explicit filesystem, process, and network policy. Unsandboxed subprocess execution is labeled `developer_trusted` and cannot receive production credentials or public-safety claims. |
| Credentials | The worktree contains no credentials. The box gets only a lease-scoped reference or supported provider login required for its role. |
| State | Run databases, packets, logs, artifacts, and worktrees live in an explicit state directory outside the source checkout. |
| Integration | Workers never merge into the user's checkout. A separate integration worktree applies candidates and reruns the frozen evaluator. |
| Evaluation | Builder-written tests may add evidence, but cannot replace, edit, or approve the frozen evaluator bundle. |

For v0, `--state-dir` is required and must resolve outside the source repository. A
dirty source checkout is rejected by default. Supporting a human-approved snapshot of
uncommitted work is a later policy; Camol must never silently copy it.

Git worktrees protect repository state, not the rest of the host. A local process
running as the user remains able to access whatever that OS account can access. This
distinction must stay visible in `camol doctor`, readiness receipts, and maturity
claims.

## 3. First real model-backed box

The first hosted model profile is a generic Claude CLI worker requesting Claude
Fable 5.1. Fable is a remote model; the Camol box contains the worktree, tools,
adapter, sandbox, and evidence collector, while inference occurs through the
supported provider connection.

```text
Camol daemon
  -> readiness gate and lease
  -> isolated box runtime
       -> Claude CLI adapter
       -> requested model: claude-fable-5-1
       -> tools operate only inside the lease policy
  <- events, checkpoint, diff, usage, and resolved-model receipt
```

The profile is data, not a special scheduler branch:

```yaml
adapter: claude_cli
requested_model: claude-fable-5-1
roles: [orchestrate, implement, investigate, review]
minimum_runtime_version: 2.1.255
connection: user_owned_supported_login
trust_tier: developer_sandboxed
```

The developer machine currently exposes Claude Code `2.1.259`, which clears the
currently documented Fable 5.1 minimum of `2.1.255`. This is only a binary-version
observation; authentication, entitlement, quota, model resolution, and task-specific
readiness remain unproven until the adapter emits fresh receipts.

Camol records both `requested_model` and `resolved_model`. A provider refusal,
safeguard reroute, or fallback invalidates any capability assumption that required
Fable specifically and is handled according to the frozen fallback policy. The model
that produced a candidate cannot be its sole consequential approver.

Public Camol remains bring-your-own connection. It never distributes the developer's
account, password, cookies, tokens, or provider quota. See Anthropic's current
[Fable 5.1 model documentation](https://platform.claude.com/docs/en/models/fable-5-1/overview)
and [plan access documentation](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)
for adapter compatibility inputs.

## 4. Dependency order

```text
M0 contracts and characterization
  -> M1 readiness receipts and doctor
      -> M2 isolated workspace and sandbox boundary
          -> M3 reservations, fenced leases, and typed waits
              -> M4 complete evidence and artifact capture
                  -> M5 Claude CLI / Fable profile
                      -> M6 daemon recovery and reconciliation
                          -> M7 evaluator gate and dogfood proof
```

Do not parallelize milestones across an unstable shared schema. Within a milestone,
tests, documentation, and independent implementation files may proceed in parallel
when their ownership does not overlap.

## 5. Pull-request-sized milestones

### M0 — Freeze contracts and characterize the current boundary

Goal: make the unsafe current behavior explicit before changing it.

Status (branches `camol/m0-readiness-contracts`, `camol/readiness-foundation`):
contracts, canonical hashing, runbook v2 + migration, typed waiting reasons, and
the three readiness ledger events are implemented and tested. A `BoxBinding`
fixes the subject `(run, task, box, worker, target)` and its workspace; receipts,
grants, reservations, and fences bind to it, to the frozen `AuthorityPolicy`, and
to the frozen `ProbePolicy` by digest. Validity is `start <= now < expires_at`
everywhere; a green receipt cannot outlive or predate its weakest probe; hashed
collections are immutable tuples; no plan field can disable readiness proof; and
a grant must equal its authority policy exactly. See `docs/readiness.md`. The
original characterization cases in `tests/test_readiness.py` asserted the
unsafe boundary. M3 inverted them: missing proof creates a typed wait, and the
scheduler now calls `assess_ready_to_lease` before issuing an atomic fenced
lease. M0 itself remains the pure-contract foundation.

Work:

- Write failing characterization cases showing that the current scheduler would
  lease an idle capability match without runtime, workspace, provider, or evaluator
  readiness.
- Define versioned schemas for `ProbeResult`, `ReadinessReceipt`, `WorkspaceReceipt`,
  `CapacityReservation`, `CapabilityGrant`, and `LeaseFence`.
- Keep runbook schema v1 readable; introduce schema v2 through explicit normalization
  and migration tests rather than changing the meaning of existing fields.
- Define a canonical JSON hashing function used by plans, receipts, packets, and
  evaluator bundles.
- Freeze the typed non-runnable states:

```text
WAITING_DEPENDENCY  AUTH_REQUIRED       NEEDS_DOWNLOAD
TARGET_UNREACHABLE  WORKSPACE_CONFLICT  CAPACITY_EXHAUSTED
EVALUATOR_NOT_READY APPROVAL_REQUIRED   READINESS_STALE
POLICY_DENIED       EFFECT_UNKNOWN      OPERATOR_ATTENTION
```

Likely files:

```text
camol/schema.py
camol/readiness.py
camol/events.py
camol/state.py
camol/runbook.py
tests/test_schema.py
tests/test_readiness.py
```

Exit gate: schema round trips and migrations are deterministic; no real process is
launched.

### M1 — Readiness receipts and `camol doctor`

Goal: prove readiness without beginning task work.

Status (branch `camol/readiness-foundation`): implemented in `camol/probes.py`
and `camol/doctor.py` with exit codes 0/2/3, `--json`, a guarded runner that
refuses to execute workspace or state-dir content and runbook commands, a
sanitized Git environment (fsmonitor/hooks/optional locks disabled), and
hostile-fixture redaction tests. What is proven: the deterministic local
process-adapter fixture in `tests/test_doctor.py` exits 0 with system-clock
evidence and 2/3 in the failure matrix. What is not: any hosted or unverified
adapter (provider and network probes are required for them and no probe adapter
exists, so they exit 2), any remote target, and the maturity ladder itself, which
stays `SPECULATIVE` until M7 produces replayable evidence with a controlled
failure and recovery. The exit gate "all-green synthetic receipt is reproducible"
is met only in the sense that a synthetic-clock receipt is deterministic; such a
receipt is a fixture, not evidence, and `READY_TO_LEASE` rejects it.

Work:

- Implement a probe registry with pure/read-only probes separated from authorized
  preparation actions.
- Probe control-plane storage, artifact sink, source repository, Git, runtime,
  adapter, provider connection, model availability, tools, services, evaluator
  launchability, disk, network policy, and capacity.
- Scope every receipt to run, plan, task, box, worker, target, runtime, workspace,
  evaluator, and policy digests.
- Record probe command, redacted result, tool version, observation time, expiry,
  status, and exact missing requirements.
- Add `camol doctor RUNBOOK --workspace REPO --state-dir DIR` and `--json` output.
- Use stable exit codes: `0` ready, `2` known-not-ready, `3` probe failure.
- Ensure `doctor` never performs downloads, creates paid resources, edits the repo,
  or calls a billable model unless a separate preparation action was explicitly
  approved.

Exit gate: an all-green synthetic receipt is reproducible; expiration, missing auth,
wrong revision, unavailable verifier, and unreachable provider produce the correct
typed state. The scheduler still cannot launch a real adapter.

### M2 — Isolated workspace and sandbox boundary

Goal: make the user's checkout an input, never a worker workspace.

Work:

- Add a workspace manager that resolves the source repository and exact starting
  commit, rejects a dirty checkout by default, and creates one branch/worktree per
  write-capable task under the external state directory.
- Produce a `WorkspaceReceipt` containing repository identity, base revision, branch,
  path, dirty digest, filesystem policy, and cleanup ownership.
- Add an integration worktree controlled only by the orchestrator.
- Define a sandbox-runner interface with allowlisted mounts, environment variables,
  subprocesses, network destinations, and credential handles.
- Keep the existing process adapter only as a visibly `developer_trusted` backend.
- Verify that path traversal, symlink escape, branch collision, nested source state,
  and attempted writes to the user's checkout are rejected.
- Add salvage-before-cleanup; Camol-created worktrees may be removed only after a
  content-addressed diff and artifact receipt exists. Adopted workspaces are never
  destroyed automatically.

Likely files:

```text
camol/workspace.py
camol/sandbox.py
camol/adapter.py
tests/test_workspace.py
tests/test_sandbox.py
```

Exit gate: a destructive fixture can damage only its disposable worktree. The source
checkout and integration worktree remain unchanged.

### M3 — Reservations, fenced leases, and typed waits

Goal: make it impossible to lease or launch work without fresh proof.

Work:

- Split task dependency readiness from box/task execution readiness.
- Introduce the lifecycle:

```text
DISCOVERED -> PROBING -> READY_FOR(task) -> RESERVED -> LEASED -> RUNNING
                   \-> WAITING(reason) / QUARANTINED
```

- Bind readiness, workspace, evaluator, authority, and capacity hashes into
  `TASK_LEASED`.
- Add monotonic fencing epochs, lease expiry, heartbeat/renewal, revocation, and
  reservation release.
- Recheck the receipt immediately before `TASK_STARTED`; stale proof returns the task
  to a typed wait without invoking the adapter.
- Replace `scheduler_deadlock` for recoverable conditions with waiting states and
  wake conditions. Reserve terminal blocking for conditions with no authorized
  recovery path.
- Test delayed workers, duplicate starts, stale fences, capacity races, expired
  receipts, cancellation, and reassignment.

Exit gate: a spy adapter proves it receives zero calls for every red or stale gate;
an eligible synthetic worker launches exactly once.

### M4 — Complete evidence and artifact capture

Goal: make every consequential claim replayable and inspectable.

Work:

- Add a content-addressed artifact store outside the repository.
- Replace stdout/stderr hash-only records with redacted content references plus
  hashes, byte counts, truncation policy, timestamps, and producer identity.
- Record every command and tool request/result, cwd, sanitized environment, model
  request/resolution, effort, token and price receipt, file diff, checkpoint,
  verifier result, approval, and external-effect identity.
- Validate envelopes at ingestion and treat worker output as untrusted data.
- Add export and replay tests, secret-redaction fixtures, artifact corruption checks,
  and bounded-stream backpressure tests.

Exit gate: a run can be reconstructed from the ledger and artifact store without its
terminal scrollback, and seeded secrets do not appear in exported evidence.

### M5 — Claude CLI adapter and Fable 5.1 profile

Goal: execute the first real bounded agent turn without teaching the kernel about a
specific vendor.

Work:

- Implement the versioned Claude CLI adapter on top of the sandbox and packet/result
  contracts.
- Use the provider's supported existing login for private development; never pass
  raw credentials in argv, packets, logs, or fixtures.
- Probe CLI compatibility, connection state, requested-model availability, quota,
  policy, and budget before readiness can become green.
- Capture runtime version and requested/resolved model for every turn. Treat unknown
  model resolution as reduced evidence, not an invented Fable claim.
- Set explicit per-turn, per-task, and per-run cost/token ceilings. Fable is preferred
  for long-horizon planning, difficult implementation, investigation, and
  adjudication; cheaper or local profiles may handle bounded tasks when backed evals
  justify them.
- Provide a fake provider and recorded response fixtures for ordinary tests. Keep
  live tests opt-in, redacted, spend-capped, and excluded from release determinism.

Likely files:

```text
camol/adapters/base.py
camol/adapters/claude_cli.py
camol/providers.py
profiles/models/claude-fable-5-1.yaml
tests/test_claude_adapter.py
tests/live/test_claude_fable.py
```

Exit gate: one opt-in, low-impact task runs in an isolated developer-sandboxed box; the
ledger proves the exact request, resolved model when available, tool activity,
changes, usage, and result. Revoked auth or exhausted budget prevents launch.

### M6 — Supervised daemon, recovery, and remote-effect reconciliation

Goal: make the run independent of a terminal window and safe across interruption.

Work:

- Split the deterministic daemon from CLI/TUI clients; retain one authoritative
  leader per local store.
- Add authenticated local IPC first. Preserve the protocol boundary needed for later
  authenticated remote workers.
- Resume leases from durable state, consume an already-written packet-bound result,
  and fence an old worker after reassignment.
- Add graceful drain, forced interruption, checkpoint salvage, and orphan detection.
- Represent external mutation as `EFFECT_REQUESTED`, `EFFECT_CONFIRMED`,
  `EFFECT_REJECTED`, or `EFFECT_UNKNOWN`; require provider readback before retrying
  an unknown effect.

Exit gate: closing the client changes nothing; killing the daemon at every event
boundary either resumes safely or exposes a typed operator decision, with no duplicate
fixture effect.

### M7 — Frozen evaluator gate and first dogfood proof

Goal: earn `BACKED` for the local-repository/Fable adapter slice.

Work:

- Compile the human-approved invariants and evaluator commands before builder launch.
- Store the evaluator bundle outside the builder worktree and bind its digest to the
  readiness receipt and lease.
- Run evaluation in a separate verifier context against the candidate revision.
- Permit builder-added tests as evidence but never as replacements for frozen tests.
- Apply a passing candidate to the integration worktree and rerun the full frozen
  bundle there.
- Dogfood one small Camol change through the complete loop.
- Inject at least: stale readiness, wrong source revision, verifier failure, daemon
  death after result write, provider unavailability, and an attempted source-checkout
  write.
- Compare direct Claude CLI versus the one-box Camol path using the matched protocol
  in `evaluation-program.md`; report quality, retries, tokens, cost, time, recovery,
  and human intervention without claiming significance from one trial.

Exit gate: the evidence bundle demonstrates approval, isolation, readiness, one real
agent turn, rejection/refinement, independent verification, integration, restart
recovery, and final human acceptance. Only this scoped profile becomes `BACKED`.

## 6. Development workflow for every milestone

Each milestone uses a separate branch and worktree created from the latest accepted
base. One owner is assigned per file set; overlapping changes wait or are rebased
before further work.

```bash
git fetch origin
git worktree add ../Camol-Harness-M1 -b camol/m1-readiness origin/main
cd ../Camol-Harness-M1
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -v
```

Before merge:

```text
all unit and integration tests green
schema and replay compatibility green
no credentials or private worklogs in the diff
diff reviewed against the milestone exit gate
branch pushed; CI green
human explicitly approves merge
```

Do not run Camol as its own builder until M7 prerequisites are present. Before that,
ordinary development tools build Camol and the deterministic fake adapter exercises
its kernel.

## 7. Release ladder

| Release | Meaning | Promotion evidence |
|---|---|---|
| `v0.0` | Current kernel simulator | Existing deterministic tests pass. |
| `v0.1` | Readiness-safe isolated local runner | M0-M4 gates pass; no real model is required. |
| `v0.2` | First real-agent dogfood loop | M5-M7 gates pass for one Claude/Fable profile. |
| `v0.3` | Adaptive N-box local/remote pool | Authenticated workers, connected-capacity inventory, backpressure, integration pressure, and fleet UI are backed. |
| `v0.4` | Public alpha | Bring-your-own connections, signed packaging, migrations, retention/deletion, security review, and recovery matrix are backed. |

## 8. Explicitly deferred until after the vertical slice

- dynamic VM or paid-resource provisioning;
- multiple hosted-provider adapters;
- automatic local-model download and GPU placement;
- cmux migration compatibility;
- GCP and voice-agent reference-profile execution;
- repository graph rendering and the spatial build visualizer;
- benchmark-scale N-box campaigns; and
- Homebrew public distribution.

These remain valid product targets. Implementing them before M7 would multiply
unproven execution paths around a scheduler that cannot yet prove readiness.

## 9. Remaining decisions, with v0 defaults

| Decision | v0 default | Revisit trigger |
|---|---|---|
| Claude connection | Existing supported Claude CLI login owned by the developer | Public alpha or provider policy change |
| Fable role | Preferred for difficult planning/build/review; never sole final approver | Matched eval evidence supports different routing |
| Source dirty state | Reject | Snapshot semantics are designed and tested |
| State directory | Explicit path outside repository, required | Cross-platform packaging selects platform defaults |
| Process trust | Sandboxed for backed claims; unsandboxed is `developer_trusted` only | A stronger local/VM backend becomes default |
| New paid capacity | Off | Human approves provider, region, count, spend, lifetime, and teardown envelope |
| First concurrency | One real box | The one-box recovery and integration gate is backed |
| Database | SQLite, supervised single writer | Measured writer contention or availability requirement |
| Merge authority | Human | A later plan explicitly authorizes a bounded protected-branch workflow |

The plan is buildable with these defaults. None of the remaining choices blocks M0.
