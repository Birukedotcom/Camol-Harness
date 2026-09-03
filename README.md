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

> Project status: `v0.0 / SPECULATIVE`. The repository contains a tested deterministic
> kernel simulator. It is not yet safe to run a real coding agent because
> task-specific readiness, worktree/process isolation, and fenced leases are still
> being implemented.

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

The wordmark and supplied PNG are the canonical boot assets. The future CLI renders
the wordmark from the upper-left and the camel right-adjusted when the terminal is
wide enough, with plaintext fallbacks for terminals without an image protocol.

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

`■` means connected and ready for the selected task—not merely installed. `□` means
known but not ready. Selection is a separate cursor. The bottom switcher traverses
the orchestrator and a window over an arbitrary N-box fleet; box numbers are visible
shortcuts, not permanent roles or a fixed worker count.

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
public or production-safe maturity claim. Evaluators and their frozen fixtures remain
outside builder write authority.

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

- validated JSON runbooks with an arbitrary non-empty registered worker pool;
- a positive per-run `max_concurrency` ceiling;
- task dependencies and static capability matching;
- frozen plan digests and human approval;
- an append-only SQLite event ledger and deterministic replay;
- packet-hash-bound results and restart recovery after an unconsumed result;
- compact checkpoints instead of automatic transcript growth;
- evidence requirements, verifier commands, retries, and token budgets;
- debugger-case promotion into evals; and
- vector-based hill-climb comparison.

The current box directory is not yet a Git worktree or security sandbox. The current
scheduler can still lease an idle static capability match without a task-specific
readiness receipt. Consequently, the included fake-agent demo tests kernel semantics
only; replacing its command with a real coding agent is not yet supported or safe.

## Build path

The implementation sequence is deliberately narrow:

| Milestone | Deliverable |
|---|---|
| M0 | Versioned readiness, workspace, reservation, grant, and lease-fence schemas |
| M1 | Read-only probe registry and `camol doctor` |
| M2 | External state directory, isolated worktrees, integration workspace, sandbox boundary |
| M3 | Task-specific readiness gates, reservations, fenced leases, typed waits |
| M4 | Complete redacted event and content-addressed artifact capture |
| M5 | First real Claude CLI / Fable 5.1 adapter profile |
| M6 | Detachable client, supervised daemon, recovery, drain, and effect reconciliation |
| M7 | Frozen evaluator, controlled failure injection, and first Camol-on-Camol dogfood proof |

The [v0 build plan](docs/v0-build-plan.md) gives each milestone its expected files,
tests, failure cases, and exit gate. Dynamic VM provisioning, multi-provider routing,
automatic local-model downloads, GCP/voice workflows, the repository graph UI,
benchmark-scale N-box campaigns, Homebrew distribution, and the spatial visualizer
come after the one-box vertical slice is backed.

## Run the current deterministic proof

Python 3.9+ is sufficient and the simulator has no runtime dependencies:

```bash
python3 -m unittest discover -v
python3 -m camol validate examples/three-agent-runbook.json
python3 -m camol run examples/three-agent-runbook.json \
  --db .camol/demo.sqlite3 \
  --workspace . \
  --approve-by "$USER"
```

Inspect the replayed projection and immutable event stream:

```bash
python3 -m camol status --db .camol/demo.sqlite3 --run-id three-agent-demo
python3 -m camol events --db .camol/demo.sqlite3 --run-id three-agent-demo
```

The example happens to register three fake workers so concurrency is easy to observe;
three is not a product assumption. Do not replace the fake adapter with a real agent
until the M0-M4 readiness and isolation gates are implemented.

## Documentation map

- [Product specification](SPEC.md): authoritative product decisions, state and
  invariant model, transparency, evaluation, security, and reference profiles.
- [v0 build plan](docs/v0-build-plan.md): PR-sized implementation order and exit
  gates for the first readiness-safe Fable dogfood run.
- [Architecture](docs/architecture.md): executable kernel objects, events, delegation,
  bounded context, and feedback loops.
- [Execution topology](docs/execution-topology.md): terminal/target/worker/workspace/box
  identities, N-box scheduling, remote protocols, fencing, and backpressure.
- [Capacity and providers](docs/capacity-and-provider-model.md): resource inventory,
  model suitability, accounts, local models, and the first Fable profile.
- [Runbook reference](docs/runbook-reference.md): current executable JSON schema,
  packets, results, and commands.
- [Debugger protocol](docs/debugger-protocol.md): observed-versus-target behavior,
  experiments, evidence, and eval promotion.
- [Evaluation program](docs/evaluation-program.md): canaries, coding suites,
  long-horizon campaigns, ablations, and direct Claude CLI comparisons.
- [Repository graph](docs/repository-graph.md): dependency rail, source graph,
  execution overlays, box traversal, and later spatial visualization.

The reconstructed Buckeye/cmux/GCP/voice workflow is a demanding reference profile,
not Camol's definition. Every core, adapter, and workflow claim remains separately
labeled `SPECULATIVE`, `MAPPED`, `BACKED`, or `PROVEN` according to replayable
evidence.
