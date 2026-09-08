# Camol

Camol is a terminal-native, local-first control plane for human-guided agent work.
You develop and challenge a plan with an orchestration agent, approve its exact
digest, and let the deterministic harness distribute bounded tasks across as many
isolated boxes as the plan and connected capacity justify.

Camol is the harness—not an agent, model, terminal multiplexer, VM, or provider
account. Orchestration agents, worker agents, models, transports, execution targets,
and evaluators are replaceable components. The kernel owns durable truth about
plans, state, readiness, authority, leases, evidence, budgets, retries, integration,
and completion.

> Development status: **0.3.0a1 is an alpha, not completion of the whole spec.**
> This branch adds an embeddable Python API, explicit invariant gates and final
> human acceptance, immutable plan revisions, a debugger, usage accounting, safe
> repository graphs, shared multi-run capacity, approved model downloads and durable
> observation/benchmark protocols. The next-wave work adds human-reviewed goal-to-plan
> creation, seed-assisted proposals, source-bound daemon launch, explicit local-model
> hosting/inference and authenticated SSH control attachment. These are narrow
> implementations, not a claim of unrestricted planning, distributed workers or
> live-model readiness.
> Local runtime
> verification includes a 170-task/eight-box soak with five verification restarts.
> Claude and Codex/local worker integrations still need account-specific live
> validation; fixture tests do not prove model availability, cloud deployment or
> production security. See the [acceptance ledger](docs/full-implementation-progress.md)
> and [exact checkpoint checks](docs/next-wave-verification.md) for implemented
> slices, limitations and remaining work. Linux CI passes; macOS portability and
> the larger-run performance investigation still require their named gates.

<table>
  <tr>
    <td width="48%">
      <pre>
      ___           ___           ___           ___
     /\__\         /\  \         /\  \         /\  \
    /:/  /        /::\  \       |::\  \       /::\  \
   /:/  /        /:/\:\  \      |:|:\  \     /:/\:\  \
  /:/  /  ___   /:/ /::\  \   __|:|\:\  \   /:/  \:\  \   ___
 /:/__/  /\__\ /:/_/:/\:\__\ /::::|_\:\__\ /:/__/ \:\__\ /\  \
 \:\  \ /:/  / \:\/:/  \/__/ \:\~~\  \/__/ \:\  \ /:/  / \:\  \
  \:\  /:/  /   \::/__/       \:\  \        \:\  /:/  /   \:\  \
   \:\/:/  /     \:\  \        \:\  \        \:\/:/  /     \:\/:/
    \::/  /       \:\__\        \:\__\        \::/  /       \::/
     \/__/         \/__/         \/__/         \/__/         \/__/
      </pre>
    </td>
    <td width="52%" align="right">
      <img src="assets/boot/camol-camel-ascii.png" alt="Camol camel ASCII boot artwork" width="620" />
    </td>
  </tr>
</table>

The wordmark and supplied PNG are the canonical boot assets. The CLI now renders the
wordmark from the upper-left and a plaintext camel derived from that artwork,
right-adjusted at wide and medium terminal sizes with compact fallbacks.

## Install and open Product V0

Camol requires Python 3.9+ and a Git repository. Textual is an optional dependency;
without it, bare `camol` opens the same command engine in line mode.

```bash
git clone --branch codex/v0-next --single-branch \
  https://github.com/Birukedotcom/Camol-Harness.git Camol-Harness-v0
cd Camol-Harness-v0
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[tui,graph]'
source .venv/bin/activate
camol
```

For an isolated command available outside the clone, use pipx after the branch is
published. This is optional and assumes `pipx` is already installed. Launch Camol
from the root of the real Git project you want it to inspect; do not paste a
placeholder directory literally.

```bash
pipx install 'camol-harness[tui,graph] @ git+https://github.com/Birukedotcom/Camol-Harness.git@codex/v0-next'
cd "$(git rev-parse --show-toplevel)"  # run while already somewhere inside your project
camol
```

A development-only [Homebrew formula](Formula/camol.rb) includes the full terminal
dependencies with checked source hashes. It is not yet a published stable tap or a
tested Homebrew installation. The verified installation path is a Python virtual
environment; see [packaging and release gates](docs/packaging.md).

## Inspect and embed the harness

```text
camol --version
camol repo crawl --workspace .
camol repo impact camol/runner.py --workspace .
```

Run-ledger tools take the real database path shown by `/status`: `camol usage`,
`camol debug`, `camol watchers`, `camol profile`, `camol logs`, `camol events`, and `camol export`. No provider
request is needed to inspect existing evidence. `/usage run` and `/debug list`
are available in the terminal; `/box` has context, transcript, tools, diff, eval
and evidence views. `/import` accepts an explicit runbook without inferring its
invariant-to-evaluator mapping from prose. Schema V5 adds per-task human gates
and `/accept` for the exact completed outcome. Schema V6 adds explicit shared
capacity/placement contracts; connected accounts do not imply known quota.

`camol models` separates download planning, exact approval, transfer and verified
artifact inspection. It never implicitly downloads, loads or claims inference
readiness. `camol watch` supports approved durable polling of a normalized local
event journal, including daemon restart; cloud source adapters need their own proof.

The next wave adds `camol model-host` for a separately approved, one-shot owned
llama.cpp process lifecycle. Its private authenticated endpoint is not yet handed
to the planner/worker adapters; loaded does not mean inference-ready. See
[local hosting](docs/local-model-hosting.md). `camol model-inference` separately
fingerprints and approves one exact prompt to that owned load, records metadata
and reported/unknown usage, and never silently retries an uncertain result. Response
text is shown only with `--show-response` and is not retained in its ledger. See
[owned inference](docs/local-model-inference.md).
[`camol remote`](docs/ssh-control.md) attaches to an already
installed, explicitly pinned SSH bridge and running supervisor; it does not provision
a machine or register a distributed worker. Neither feature runs on startup.

`/propose --from REVIEWED_SEED.json GOAL` makes one explicit, no-tools planning
request to a supported selected provider. It returns questions or a complete
unapproved V5/V6 candidate preserving the reviewed seed's authority. It requires
exact-digest human approval, not a bare “yes.” See
[seed-assisted proposals](docs/seed-assisted-proposals.md) for source binding,
provider limitations and usage coverage; this mode still requires a reviewed seed.

For a goal without a seed, `/grill --draft GOAL` guides six decisions about outcomes,
invariants, oracles, worker runtime and limits. `/draft confirm DIGEST` confirms
planning boundaries; an explicit `/propose` requests a candidate. New command and
oracle authority needs `/review DIGEST`, then `/approve DIGEST`, before `/run` can
perform readiness checks. Every proposed state gate and the integrated outcome
remain human-gated. See the [goal creation workflow](docs/goal-creation-wizard.md).

`/history [COUNT]` reads a bounded recent transcript view instead of loading the
entire lifetime archive. It does not delete old records or imply a retention policy.
The [approved-source handoff contract](docs/approved-source-handoff.md) explains
the durable binding and its difference from legacy unbound plans.

Python applications use [`Harness`](docs/embedding.md) without a terminal. It owns
the same execution lock, isolated workspaces and durable kernel as the CLI, with
methods for approval, execution, observation, debugging, usage, revisions and
portable lineage exports. Trusted plugins are host code; they are not capabilities
that worker agents may grant themselves.

Inside Camol, a first live-capable plan looks like this:

This manual `/grill` path currently produces a Claude-worker V4 plan. Choosing
Codex for planning does not silently turn that worker plan into Codex execution.
For a Codex or Codex-local worker, import a reviewed V5/V6 runbook with embedded
provider profiles; `/run` then presents the exact provider-policy acknowledgement.
See [Codex worker contracts](docs/codex-workers.md). Neither path starts work merely
because a login succeeds.

```text
/connections
/login                        # for the manual grill path, choose Claude with arrows + Enter
                              # successful verification selects its planning model
/effort max
/grill Build the feature and prove it without deployment
                               # answer each visible question; use structured limits such as:
                               # boxes=3 turns=6 tokens=48000 cost_cents=100 turn_timeout_seconds=1800
/plan                          # inspect invariants, DAG, evaluator argv, and digests
/approve yes                   # approval freezes only the exact visible digest
/run --accept-spend --worker-cents 100 --preflight-cents 10
/status
/boxes
/box 1
/events
/quit                          # detach; does not stop the supervisor
```

The composer follows chat-CLI conventions: Enter sends, Shift+Enter (or Ctrl+J)
adds a line, and typing `/` opens a typeable command-and-skill palette. Keep typing
to filter it, use ↑/↓ to change the highlighted choice, Tab to complete, or Enter
to accept. The palette's input owns ordinary characters, so Space remains text in
commands such as `/grill build this` instead of scrolling the result list. `/skills`
shows the small set of durable Camol protocols—grill, debugger, evidence,
refinement, and readiness—rather than pretending every prompt fragment is a skill.
The interface uses a deep matrix-green, charcoal, gray, and white terminal palette.
On reattach, Camol shows a fresh session summary instead of replaying old output;
`/history` retrieves retained entries and `/clear` clears only the current terminal
view. Ctrl+C safely detaches the terminal client; it does not stop an active
supervisor or discard run state.

The exact plan embeds the effective provider effort, token limits, timeout, tool/network
policy, and total worker cost ceiling. `/run` requires the human to repeat that worker
ceiling exactly, refuses a dirty source checkout, and runs one separately capped,
no-tools provider preflight before it starts a Claude supervisor. The supervisor then prepares isolated
worktrees and will not issue a lease until its task-specific readiness predicate is
green. Use `/drain`, `/resume`, or `/stop` for lifecycle control. Interactive state is
kept outside the repository under the platform state directory; `/status` prints its
exact path.

## The intended terminal experience

Camol opens on the orchestrator. `/grill` turns a goal into an explicit plan by
questioning assumptions, exclusions, invariants, risk, resources, evaluators, and
completion criteria. No task begins until a human confirms the frozen plan and Camol
proves that its exact execution path is ready.

```text
┌ CAMOL / ORCHESTRATOR ───────── DOCKER ■  GIT ■  GCP □  MODELS ■↓ ┐
│ plan: frozen @ 91d3…       state: WAITING_READINESS       ! 2    │
│ goal: ship payment transport without exposing card data          │
├ PLAN / GATES ───────────────────┬ BOXES / LIVE WORK ──────────────┤
│ ✓ human goal                    │ ■ api-builder    EXECUTING      │
│ ✓ invariants                    │ ■ adversary      EVALUATING     │
│ ✓ evaluator bundle             │ □ deploy         AUTH_REQUIRED  │
│ ! GCP deploy authority          │ … 5 dormant; 1 needs attention  │
├ SELECTED EVIDENCE ──────────────┴─────────────────────────────────┤
│ task api-transport · verifier red · response body leaked PAN      │
│ next: return candidate to refinement; deployment remains blocked  │
├───────────────────────────────────────────────────────────────────┤
│ camol> /box api-builder                                          │
└───────────────────────────────────────────────────────────────────┘
── WORKSPACES ──────────────────────────────────────────────────────
[0 ORCH] [BOXES 2/8] [!2] ▸[1 ■ API] [2 ■ ADVERSARY] [3 □ DEPLOY]
```

Glyphs are deliberately contextual. In the top rail, `■` means an account/runtime is
connected or a dependency is installed—it is **not** task readiness. In the bottom
fleet, `■` means an isolated box workspace exists, `□` means dormant/unprepared, and
`!` needs attention. The kernel reports task readiness separately. Selection is a
separate cursor. The bottom switcher traverses
the orchestrator and a window over an arbitrary N-box fleet; box numbers are visible
shortcuts, not permanent roles or a fixed worker count.

Provider connections are scanned in the background at startup. `↻` means the
installed CLI is still being inspected; it changes to `■` only after the provider's
own status command confirms authentication. Camol preserves the non-secret local
identity variables those CLIs require while filtering unrelated environment values,
and it refreshes the rail automatically after `/login`. The V0 picker intentionally
contains only Claude Code and Codex CLI. Choosing either entry always hands the
terminal to that provider's native browser-login flow—even if discovery is already
green—so reconnecting or switching accounts predictably exposes the provider-owned
URL and prompt. A newly verified login lights the provider glyph, records the result
in the orchestrator transcript, and selects `claude:fable` or `codex` when no plan is
frozen or running. Cancelling the provider flow never promotes an older green record
into a new login confirmation.

Selecting a box opens a read-only peer view of its terminal stream, bounded context,
tool calls, diff, evaluations, events, and evidence. Sending input or taking over is
a distinct, approval-aware, logged transition.

## The operating model

```text
human + orchestration agent
  -> /grill goal, constraints, invariants, evaluators, and resources
  -> human approves exact plan digest
  -> kernel discovers eligible connected capacity
  -> readiness probes prove each task/box combination
  -> scheduler reserves capacity and leases isolated work
  -> boxes emit checkpoints, commands, tools, diffs, usage, and claims
  -> frozen independent evaluators return green or counterexamples
  -> orchestrator integrates accepted candidates or proposes an amendment
  -> human confirms consequential transitions and final acceptance
```

A box may implement a unique task, independently verify another box, explore a
competing implementation, observe a long-running system, or remain dormant. Camol
activates zero to N boxes from useful task-graph width and a human-approved resource
envelope; it never launches boxes merely to fill a target count.

All authoritative box-to-box information passes through the orchestrator and durable
ledger. The orchestrator receives compact checkpoints and typed deltas rather than N
ever-growing raw transcripts.

## Readiness before work

Registered, installed, connected, authenticated, idle, and task-ready are distinct
states. Camol's launch invariant is:

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

A readiness receipt binds the run, plan, task, box, worker, execution target,
transport, runtime, workspace revision, evaluator, model/provider capability,
credential scope, resource reservation, probe results, and expiration. The runner
rechecks the receipt immediately before launch. Red or stale proof produces a typed
wait such as `AUTH_REQUIRED`, `WORKSPACE_CONFLICT`, `EVALUATOR_NOT_READY`,
`TARGET_UNREACHABLE`, or `READINESS_STALE`; it does not start the agent or collapse
everything into a scheduler deadlock.

## Isolation is layered

Every write-capable task gets a dedicated branch and Git worktree outside the user's
checkout. Run databases, packets, evidence, artifacts, and worktrees live in an
explicit external state directory. A separate orchestrator-owned integration
worktree applies candidates and reruns the frozen evaluator; workers never merge
directly into `main`.

Repository isolation is not process security. Real workers must also launch through
a sandbox with explicit filesystem, process, environment, network, and credential
grants. An unsandboxed local process is labeled `developer_trusted` and cannot earn a
public or production-safe maturity claim. The canonical evaluator definition and
asset blobs remain outside builder write authority; changed workspace copies are
rejected before evaluator execution and before readmission.

## Models and execution targets

Hosted and downloaded models use the same capability-oriented selection path.
Possible execution targets include the local host, containers, VMs, pods, remote
development environments, or provider jobs. A terminal is only a detachable client;
closing it must not stop the supervised Camol daemon.

The first real hosted-model proof is a generic Claude CLI box requesting Claude
Fable 5.1:

```text
Camol daemon
  -> readiness gate + fenced lease
  -> sandboxed box + isolated worktree + Claude CLI adapter
  -> remote Fable 5.1 inference
  <- requested/resolved model, tools, usage, diff, checkpoint, evidence
```

Fable is the first profile, not a kernel dependency. Camol records both the requested
and resolved model, so a provider fallback cannot silently inherit Fable-specific
capability claims. Development may use the owner's supported CLI login; public Camol
uses bring-your-own provider connections or local models and never embeds the
developer's credentials.

## Evaluation, debugging, and hill climbing

States are gated by human-confirmed invariants and visible evidence—not trust in an
existing implementation. `/grill` proposes candidate invariants from the goal,
policy, traces, past behavior, counterexamples, and alternate implementations; a
human confirms the normative set before it becomes a gate.

Every evaluator, generator, threshold, command, result, exception, and approval is
visible. Builder-authored tests can strengthen evidence but cannot replace frozen
tests. Failed evidence returns the task to a bounded refinement loop.

The debugger makes the comparison explicit:

```text
current observed behavior
  -> target behavior
  -> smallest reproducible divergence
  -> bounded experiment
  -> commands, tools, transcripts, environment, diff, and result evidence
  -> independent verification
  -> permanent regression eval
```

Hill climbs compare named measurement vectors. A gain in one dimension cannot hide a
forbidden correctness, security, cost, or latency regression.

## What exists today

The executable Python slice currently provides:

- bare `camol` as a transcript-first Textual application, plus a dependency-light
  line fallback, responsive CAMOL/camel boot art, multiline input, history, streamed
  provider text, and an arbitrary-N fleet switcher;
- durable private orchestrator sessions, a deterministic `/grill` question loop,
  human-readable N-task DAGs, exact plan/runbook digests, two-step approval, and
  automatic invalidation when frozen model or effort settings change;
- structured box/turn/token/cost/timeout limits, approved exclusions copied into
  worker rules, intermediate patch checks, and one DAG-final human-selected evaluator;
- credential-safe connection discovery for Claude CLI, Codex CLI, an
  `OPENAI_API_KEY` environment reference, and a loopback OpenAI-compatible endpoint;
- planning-only Claude/Codex/local conversations with no write tools, explicit
  requested-versus-resolved model reporting, and no hidden model call in manual mode;
- a versioned authenticated V2 client protocol for plan, cursor-based events, and
  read-only box views while retaining V1 `camol ctl` compatibility;
- terminal supervisors that remain inspectable after completion, plus automatic
  short private AF_UNIX paths on systems with small socket limits; and

- validated JSON runbooks with an arbitrary non-empty registered worker pool;
- a positive per-run `max_concurrency` ceiling;
- task dependencies and static capability matching;
- frozen plan digests and human approval;
- an append-only SQLite event ledger and deterministic replay;
- packet-hash-bound results and restart recovery after an unconsumed result;
- compact checkpoints instead of automatic transcript growth;
- evidence requirements, verifier commands, retries, and token budgets;
- debugger-case promotion into evals;
- vector-based hill-climb comparison;
- one canonical JSON digest function (`camol.schema.canonical_digest`) shared by
  plans and every readiness contract;
- versioned, validated `ProbeResult`, `ReadinessReceipt`, `WorkspaceReceipt`,
  `CapacityReservation`, `CapabilityGrant`, and `LeaseFence` contracts
  (`camol.readiness`), plus the twelve typed non-runnable reasons;
- runbook schemas v1-v4 with explicit migrations, strict current fields, and pinned
  v1 digests;
- `READINESS_RECORDED`, `TASK_WAITING`, and `TASK_WAIT_CLEARED` ledger events;
- a `BoxBinding`, `AuthorityPolicy`, and `ProbePolicy` identity model that binds every
  proof record to one lease subject, plus the pure `READY_TO_LEASE` predicate; and
- `camol doctor`: a read-only probe registry that proves task-specific readiness
  (state directory, artifact sink, Git, repository revision and dirty state, adapter
  and tool binaries, evaluator bundle, disk, capacity) and emits candidate readiness
  receipts without launching, downloading, authenticating, or spending anything;
- task-specific isolated Git worktrees, enforced sandbox policies, reservations,
  fenced leases, pre-launch rechecks, typed waits, and N-box scheduling;
- complete redacted command/tool/transcript/diff evidence and content-addressed
  export verification;
- a modular Claude CLI adapter with explicit spend preflight and requested-versus-
  resolved model identity;
- a detached single-writer supervisor with authenticated local control, drain,
  salvage, orphan-process reconciliation, and no-blind-retry external effects; and
- evaluator bundles compiled outside builder authority, protected evaluator assets,
  separate candidate-verifier worktrees, generation-based integration, visible
  counterexamples/refinement, and matched vector-valued benchmark comparison.

A green `camol doctor` is not a lease: it proves readiness dimensions only, shows
that a grant and a reservation are still missing, and is possible only for a
verified local interpreter adapter. Any hosted or unverified adapter additionally
requires provider and network proof, which no probe adapter can supply yet, so
such runbooks exit 2. Receipts produced with `--now` are synthetic fixtures that
the lease predicate rejects. Consequently, the
included fake-agent and deterministic dogfood runs prove kernel semantics only. A
hosted-model run additionally needs explicit provider readiness and spend approval.
See [docs/readiness.md](docs/readiness.md).

## Build path

The implementation sequence is deliberately narrow:

| Milestone | Deliverable |
|---|---|
| M0 (implemented; `SPECULATIVE` maturity) | Versioned readiness, workspace, reservation, grant, and lease-fence schemas |
| M1 (implemented for the local process adapter only; `SPECULATIVE` maturity) | Read-only probe registry and `camol doctor` |
| M2 (implemented) | External state directory, isolated worktrees, integration workspace, sandbox boundary |
| M3 (implemented) | Task-specific readiness gates, reservations, fenced leases, typed waits |
| M4 (implemented) | Complete redacted event and content-addressed artifact capture |
| M5 (implemented; live proof pending) | Modular Claude CLI adapter and speculative Fable profile |
| M6 (implemented locally) | Detachable client, supervised daemon, recovery, drain, and effect reconciliation |
| M7 (kernel implemented; hosted proof pending) | Frozen evaluator, controlled failure injection, matched-trial contract, and deterministic Camol-on-Camol proof |

The [v0 build plan](docs/v0-build-plan.md) gives each milestone its expected files,
tests, failure cases, and exit gate. The current alpha extends that slice with
explicit model downloads, static repository graphs, shared N-box capacity, durable
local watchers and an embeddable benchmark campaign. Dynamic VM provisioning,
live GCP/voice workflows, public-suite benchmark proof, published Homebrew
distribution and the spatial visualizer are not implied by those implementations.
The [current acceptance ledger](docs/full-implementation-progress.md) distinguishes
implemented adapters from validated workflows.

## Run the current deterministic proof

Python 3.9+ is sufficient and the simulator has no runtime dependencies:

```bash
CAMOL_STATE="$(mktemp -d /tmp/camol-v0.XXXXXX)"
python3 -m unittest discover -v
python3 -m camol validate examples/local-n-box-runbook.json
python3 -m camol doctor examples/local-n-box-runbook.json --workspace . --state-dir "$CAMOL_STATE"
python3 -m camol run examples/local-n-box-runbook.json \
  --workspace . \
  --state-dir "$CAMOL_STATE" \
  --approve-by "$USER"
```

Run the reproducible Camol-on-Camol kernel proof from a clean checkout:

```bash
CAMOL_PROOF_PARENT="$(mktemp -d /tmp/camol-proof.XXXXXX)"
python3 scripts/run_v0_proof.py --output "$CAMOL_PROOF_PARENT/result"
```

The checked-in compact attestation is
[`evidence/v0-local-kernel-proof.json`](evidence/v0-local-kernel-proof.json). It
binds the exact implementation commit and archive-manifest hash while preserving the
explicit boundary that this is not hosted-model evidence.

Or detach a draft run, inspect it, approve the exact plan, and let the daemon continue
after the client exits:

```bash
CAMOL_SUPERVISED="$(mktemp -d /tmp/camol-supervised.XXXXXX)"
python3 -m camol start examples/local-n-box-runbook.json \
  --workspace . --state-dir "$CAMOL_SUPERVISED"
python3 -m camol ctl status --state-dir "$CAMOL_SUPERVISED"
python3 -m camol ctl approve --state-dir "$CAMOL_SUPERVISED" --by "$USER"
```

Inspect the replayed projection and immutable event stream:

```bash
python3 -m camol status --db "$CAMOL_STATE/camol.sqlite3" --run-id local-n-box-demo
python3 -m camol events --db "$CAMOL_STATE/camol.sqlite3" --run-id local-n-box-demo
```

The local N-box example happens to register three fake workers so concurrency is easy
to observe; three is not a product assumption. The legacy
`examples/three-agent-runbook.json` remains frozen as the schema-v1 digest fixture and
is not the in-repository execution example. Use a schema-v3/v4 hosted profile, successful
provider preflight, an enforcing trust tier, and explicit budget approval before a
real hosted-agent run.

## Documentation map

- [Product specification](SPEC.md): authoritative product decisions, state and
  invariant model, transparency, evaluation, security, and reference profiles.
- [v0 build plan](docs/v0-build-plan.md): PR-sized implementation order and exit
  gates for the first readiness-safe Fable dogfood run.
- [V0 verification](docs/v0-verification.md): reproducible proof, failure-injection
  map, maturity boundaries, and matched-comparison rules.
- [V0 independent review](docs/v0-independent-review.md): read-only adversarial
  scope, verdict, disclosed limits, finding, and remediation.
- [Architecture](docs/architecture.md): executable kernel objects, events, delegation,
  bounded context, and feedback loops.
- [Execution topology](docs/execution-topology.md): terminal/target/worker/workspace/box
  identities, N-box scheduling, remote protocols, fencing, and backpressure.
- [Capacity and providers](docs/capacity-and-provider-model.md): resource inventory,
  model suitability, accounts, local models, and the first Fable profile.
- [Runbook reference](docs/runbook-reference.md): current executable JSON schema,
  packets, results, and commands.
- [Readiness contracts and doctor](docs/readiness.md): the lease-subject identity
  model, temporal validity, `READY_TO_LEASE`, the read-only probe registry, exit
  codes, execution guard, and redaction.
- [Debugger protocol](docs/debugger-protocol.md): observed-versus-target behavior,
  experiments, evidence, and eval promotion.
- [Embedding](docs/embedding.md): Python harness ownership, execution, approvals and exports.
- [Usage and logging](docs/usage-accounting.md): observed versus reserved spend, profiling,
  metadata log sinks and incomplete measurement coverage.
- [Durable observers](docs/durable-observers.md): bounded approved polling, normalized
  journal source, cursor recovery and daemon scheduling.
- [Codex workers](docs/codex-workers.md): frozen hosted/local worker profiles and
  explicitly supported capability boundaries.
- [Plan revisions](docs/plan-revisions.md): linked, owner-approved successors and inherited costs.
- [Benchmark campaigns](docs/benchmark-campaigns.md): frozen cohorts, paired arms and replay.
- [Packaging](docs/packaging.md): installation verification and public release gates.
- [Evaluation program](docs/evaluation-program.md): canaries, coding suites,
  long-horizon campaigns, ablations, and direct Claude CLI comparisons.
- [Repository graph](docs/repository-graph.md): dependency rail, source graph,
  execution overlays, box traversal, and later spatial visualization.

The reconstructed Buckeye/cmux/GCP/voice workflow is a demanding reference profile,
not Camol's definition. Every core, adapter, and workflow claim remains separately
labeled `SPECULATIVE`, `MAPPED`, `BACKED`, or `PROVEN` according to replayable
evidence.
