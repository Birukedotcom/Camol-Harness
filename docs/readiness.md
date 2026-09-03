# Readiness contracts and `camol doctor`

Status: tested deterministic slice (unit and CLI fixtures in `tests/test_doctor.py`:
clean local Git checkout, process adapter, no hosted model). Its maturity is still
`SPECULATIVE` under SPEC §2, because `BACKED` requires replayable evidence of a
real run with a controlled failure and recovery, which only M7 produces. Everything
a probe cannot observe safely is reported as a typed `unknown`, never assumed.

This document describes what the code in `camol/readiness.py`, `camol/probes.py`,
and `camol/doctor.py` actually enforces. Where the product specification asks
for more, the gap is named here rather than implied.

## 1. The lease subject and its binding

One lease subject is the tuple `(run_id, task_id, box_id, worker_id, target_id)`.
It is fixed by a `BoxBinding`, which also names the frozen plan digest and the
exact workspace (`workspace_id` plus the digest of its `WorkspaceReceipt`).

Every proof record carries the full subject or the binding's digest:

| Record | Carries | Bound by digest to |
|---|---|---|
| `BoxBinding` | subject, plan digest, workspace id + digest | — |
| `WorkspaceReceipt` | repository, base revision, branch, path, dirty digest, filesystem policy, cleanup owner | referenced from `BoxBinding.workspace_digest` |
| `AuthorityPolicy` | run, task, required capabilities (non-empty), filesystem paths, network destinations, credential refs, trust tier | — |
| `ProbePolicy` | run, task, exact required probe set (`ProbeRequirement`: id, kind, target-bound, **definition digest** of the exact probe implementation/version/config) | — |
| `ProbeResult` | probe id/kind, optional target, status, method, redacted argv, tool version, observed/expiry, summary, missing requirements, evidence digest, typed reason + wake condition, definition digest | — |
| `ReadinessReceipt` | subject, plan, transport, runtime, probes, status, observed/expiry, **clock provenance** (`system` or `synthetic`), requested model, credential scope fingerprints, optional reservation id | box binding, workspace, evaluator, authority policy, probe policy |
| `CapabilityGrant` | subject, capabilities, constraints, trust tier, grantor, granted/expiry | authority policy |
| `CapacityReservation` | subject, slots, tokens, optional spend, status, reserved/expiry | — |
| `LeaseFence` | subject, epoch, issued/expiry | plan, box binding, evaluator, workspace, authority, probe policy, readiness receipt, grant, reservation |

Pre-M2 box identity rule: the current process adapter has no box records, so
`camol doctor` uses the agent id as both `box_id` and `worker_id`, the agent's
`box` path as the workspace path, and describes that workspace honestly as
`filesystem_policy: shared_checkout_write`, `cleanup_owner: adopted`. M2
replaces this with isolated worktrees; the workspace digest changes and every
receipt bound to it must be re-probed.

## 2. Temporal validity

Every time-bound record is valid on the closed/open interval
`start <= now < expires_at`:

| Record | start |
|---|---|
| `ProbeResult` | `observed_at` |
| `ReadinessReceipt` | `observed_at`, and every probe must itself be fresh |
| `CapabilityGrant` | `granted_at` |
| `CapacityReservation` | `reserved_at` |
| `LeaseFence` | `issued_at` |

A future-dated record is never fresh. A green receipt cannot be constructed
unless every probe is green, has an expiry, was observed no later than the
receipt, and expires no earlier than the receipt. A red receipt must contain at
least one non-green probe. Any target-bound probe must name the receipt's
target.

## 3. `READY_TO_LEASE`

`assess_ready_to_lease` is a pure function over the records above. It is not yet
called by the scheduler (M3); `tests/test_readiness.py` keeps the loudly named
`test_CURRENT_UNSAFE_*` cases that document the scheduler's present behavior.

Every failed conjunct yields a typed `WaitingReason` bound to the task and box.
The mapping is:

| Condition | Reason |
|---|---|
| plan not approved / binding names another plan | `APPROVAL_REQUIRED` / `POLICY_DENIED` |
| control plane not ready | `OPERATOR_ATTENTION` |
| dependencies not succeeded | `WAITING_DEPENDENCY` |
| no or mismatched workspace receipt | `WORKSPACE_CONFLICT` |
| no receipt, red receipt, outside validity, stale probe, missing/mis-kinded required probe, pre-reservation receipt, receipt names another reservation | `READINESS_STALE` |
| any record names another run, task, box, worker, target, binding, authority policy, or probe policy; a probe the policy never listed or produced by a different probe definition; grant carries authority the policy did not freeze; receipt produced with a synthetic clock | `POLICY_DENIED` |
| evaluator not ready or receipt names another evaluator | `EVALUATOR_NOT_READY` |
| no authority policy, no grant, grant lacks required capabilities, grant outside validity | `APPROVAL_REQUIRED` |
| no reservation or reservation not active | `CAPACITY_EXHAUSTED` |

Authority sufficiency: `AUTHORITY_GRANTED` means the grant is bound to the frozen
`AuthorityPolicy` by digest **and** equals it exactly. A grant with fewer
capabilities is insufficient (`APPROVAL_REQUIRED`); a grant with more
capabilities, paths, destinations, credentials, or a different trust tier is
unauthorized (`POLICY_DENIED`). An empty authority policy cannot exist, so an
empty grant can never be sufficient.

## 4. `camol doctor`

```bash
camol doctor RUNBOOK --workspace REPO --state-dir DIR
camol doctor RUNBOOK --workspace REPO --state-dir DIR --json
```

Exit codes: `0` every task has at least one coherent green candidate receipt;
`2` a readiness requirement is red, unknown, stale, or needs preparation or
human action; `3` a probe could not be performed or interpreted.

The doctor is read-only. It never creates the state directory or the artifact
sink, never writes into the repository, never launches an adapter, never
downloads or installs anything, and never sends a model request. Missing
preparation is reported with the exact action a human must take.

Options: `--now` fixes the observation instant for deterministic fixtures. A
receipt produced that way carries `clock: synthetic`, the report records
`synthetic_clock: true` and `usable_evidence: false`, and `READY_TO_LEASE`
rejects the receipt with `POLICY_DENIED`; only `clock: system` receipts are
evidence. `--receipt-ttl-seconds` applies to schema v1 runbooks
only (default 300); a v2 runbook freezes the TTL in `run.readiness_policy` and a
conflicting flag is an error. `--require-service HOST:PORT` adds a read-only TCP
reachability probe. `--min-free-bytes` sets the disk headroom requirement.

### Probes in the default registry

| Probe | Kind | Method | Green means | Non-green reason |
|---|---|---|---|---|
| `control-plane.state-dir` | control_plane | filesystem | exists, writable, outside the repository (lexical and resolved paths, both directions, including the git common dir) | `POLICY_DENIED` nested, `OPERATOR_ATTENTION` missing/unwritable |
| `control-plane.artifact-sink` | control_plane | filesystem | `<state-dir>/artifacts` is a real (non-symlink) writable directory inside the state dir, or absent under a writable state dir; a symlink, or a sink/state dir resolving into the workspace or git common dir, is rejected | `POLICY_DENIED`, `OPERATOR_ATTENTION` |
| `source.git` | runtime | process (`git --version`) | git on PATH with a parseable version | `NEEDS_DOWNLOAD` |
| `source.repository` | source | process (`git rev-parse`, `git status --porcelain`, all with `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `core.untrackedCache=false`, `GIT_OPTIONAL_LOCKS=0`, no global/system config, sanitized env) | workspace is a clean repository root at a committed HEAD; remote URL recorded with userinfo redacted | `WORKSPACE_CONFLICT`, `TARGET_UNREACHABLE` |
| `runtime.python` | runtime | in_process | control-plane interpreter version | — |
| `adapter.<agent>` | adapter | process or filesystem | argv[0] is a bare allowlisted interpreter on PATH with a parsed version, and every workspace-relative script argument is a regular file (resolved against the workspace, never the process cwd) | `NEEDS_DOWNLOAD`; `POLICY_DENIED` (unknown) for any other binary, which the doctor refuses to execute and whose identity is unproven |
| `tools.commands` | tool | filesystem | every task step binary resolves: bare names via PATH, path forms as regular executable files under the workspace | `NEEDS_DOWNLOAD` |
| `evaluator.bundle` | evaluator | filesystem | verification bundle digest recorded and every verifier binary resolves under the same rules (nothing is run) | `EVALUATOR_NOT_READY` |
| `resources.disk` | resource | filesystem | free space at the state dir meets the requirement (whole-GiB granularity) | `CAPACITY_EXHAUSTED` |
| `capacity.local` | capacity | in_process | CPU count observed; records that no reservation ledger exists yet | `OPERATOR_ATTENTION` |
| `network.policy` | network | in_process | never green today; informational for a verified local interpreter, required for any other adapter | `OPERATOR_ATTENTION` |
| `provider.connection` | provider | in_process | never green today; informational for a verified local interpreter, required for any other adapter | `AUTH_REQUIRED` |
| `service.<host>-<port>` | service | socket | TCP connect then close | `TARGET_UNREACHABLE` |

Requiredness derives from the adapter, not from a fixed list. `adapter_policy`
classifies an adapter as a *verifiable local interpreter* only when it is a
`process` adapter whose argv[0] is a bare, allowlisted binary name (`python3`,
`node`, ...) found on PATH; the adapter probe then verifies it by parsing its
version. For every other adapter (a hosted CLI such as `claude`, an unknown
binary, a path-form interpreter, an unversioned tool) the provider-connection
and network-policy probes become **required**. Because no provider probe
adapter exists yet, those probes are `unknown`, so such a candidate is red and
the doctor exits 2. **A green doctor is therefore only possible for a verified
local process adapter, and even then it proves nothing about hosted-model
availability or network egress**; the informational probes remain visible with
`informational: true` and excluded from that candidate's probe policy.

Every `ProbeRequirement` carries the definition digest of the exact probe
implementation, version, and configuration; a result with the same id from a
different definition is `POLICY_DENIED`.

### Execution guard and Git side-effect safety

Every subprocess a probe starts goes through `GuardedRunner`. It refuses any
argv whose program resolves inside the workspace or the state directory
(checked lexically and after symlink resolution, relative to the command's
working directory), and any argv equal to a runbook adapter, step, or
verification command, raw or with `{workspace}` substituted. Version queries are
made only for PATH-resolved binaries on a small allowlist. A committed
executable named `python3` in the repository is therefore reported, not run;
`tests/test_doctor.py` proves the sentinel it would write never appears.

Subprocesses run with a sanitized environment (only `PATH`, `HOME`, `TMPDIR`,
a C locale; every `GIT_*` variable dropped). Git commands add
`GIT_OPTIONAL_LOCKS=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_TERMINAL_PROMPT=0`, and command-line overrides for `core.fsmonitor`,
`core.untrackedCache`, `core.hooksPath`, `core.sshCommand`, `core.pager`, and
`protocol.allow`, so a repository that configures an fsmonitor hook or hooks
path cannot run anything and the index is never rewritten while the doctor
reads it. `tests/test_probes.py` commits such a trap, proves unguarded git fires
it, and proves the probes do not.

### Redaction

All summaries, argv, facts, wake conditions, and errors pass through
`Redactor` before they reach a `ProbeResult`, the terminal, or JSON. It removes
values of secret-named environment variables, URL userinfo, `Bearer`/`Basic`
authorization, cookie headers, PEM private-key blocks, secret-looking
`key=value` pairs and flags, and well-known token shapes (OpenAI-style `sk-`,
GitHub `ghp_`/`github_pat_`, Slack `xox*`, AWS `AKIA`, Google `AIza`, GitLab
`glpat-`, npm, JWT). `tests/test_probes.py` and `tests/test_doctor.py` push
hostile fixtures through every path.

### What the doctor emits per candidate

For every task and every statically eligible worker: the `WorkspaceReceipt`,
`BoxBinding`, `AuthorityPolicy` (the process adapter's factual authority:
`execute, read, write`, the box path, and the agent's trust tier, defaulting to
`developer_trusted` for schema v1 because that is what an unsandboxed local
process is), `ProbePolicy`, and a candidate `ReadinessReceipt` with all digests.
The receipt is pre-reservation (`reservation_id: null`). The report also shows
the full `READY_TO_LEASE` assessment for that candidate, which on a green doctor
still reads `APPROVAL_REQUIRED` (no grant) and `CAPACITY_EXHAUSTED` (no
reservation), so a green doctor is visibly not a lease.

## 5. Runtime enforcement (M2 and M3)

The runtime admission path is stricter than the read-only doctor path:

- `WorkspaceManager` creates a dedicated task worktree and emits an
  `isolated_worktree_write` receipt. The source checkout and integration
  worktree remain protected roots.
- `AdmissionController` reserves capacity, issues exact authority, freezes the
  sandbox and probe policies, runs the required probes, and records one
  reservation-bound admission bundle.
- The scheduler calls the same `READY_TO_LEASE` predicate, then atomically emits
  a monotonic `LeaseFence` bound to the plan, box binding, evaluator, workspace,
  authority, probe policy, readiness receipt, grant, and reservation.
- The runner re-observes the workspace immediately before process launch. A
  changed or expired input emits a typed wait and releases capacity without
  calling the adapter.
- Admission and lease appends use optimistic compare-and-append transactions,
  so concurrent schedulers cannot overbook capacity or issue two fences for one
  task. Cancellation, revocation, retry, blocking, and success release the
  reservation atomically with their state transition.

M5 still needs to register provider probe adapters so
`provider.connection` can become green without a billable request.
