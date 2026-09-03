# Camol product specification

Status: `SPECULATIVE`

Evidence posture: `PARTIALLY_MAPPED` from three evidence classes:

- the external, PHI-bearing `buckeye-worklog-2026-08-26_2026-09-02` bundle
  (`BUNDLE_REPORTED`);
- a read-only local Enrollment Hub checkout whose age and detached revision make its
  contents `STALE_CHECKOUT_READ`, not live-cloud truth; and
- read-only probes of the installed cmux and gcloud clients (`LOCAL_OBSERVED`).

The worklog is deliberately not committed. No production call or GCP resource was
mutated while deriving this specification. Claims remain labeled until Camol
reproduces them through its own adapters and event ledger.

This document is the authoritative product specification for Camol. The executable
protocol details in `docs/` refine this document but do not override it. A run becomes
authoritative only when a human approves the exact digest of its compiled plan.

## 1. Product thesis

Camol is a terminal-native, local-first control plane for human-guided agent work. A
human develops a plan with one orchestrator, freezes that plan, and lets the
orchestrator distribute bounded work across an N-box pool. There is no product-level
box-count constant. A run activates only the boxes justified by its task graph and
human-approved resource envelope. The harness continues until state gates are
satisfied, a human decision is required, or an owner-defined pause condition is
reached.

Camol is the harness, not an agent or model. A replaceable orchestration agent reasons
about plans, decomposition, placement, and reconciliation through an adapter. Worker
agents execute leases through other adapters. The deterministic Camol kernel decides
whether any proposal is authorized and whether its evidence can advance state.

Camol is not primarily a chat interface, a prompt collection, a web application, or
a terminal multiplexer. Its product is the durable combination of:

- a human-confirmed plan;
- a dependency DAG;
- state-specific invariants and obligations;
- evidence-gated state transitions;
- a deterministic reconciler;
- inspectable agent boxes;
- complete tool and model-call history;
- evaluation, debugging, and hill-climb feedback loops; and
- replaceable execution-target, transport, runtime, model, and cloud adapters.

The selected orchestration agent reasons. The deterministic kernel owns truth about
state, leases, revisions, evidence, approvals, retries, wakeups, and terminal
conditions.

## 2. Product maturity and the replacement threshold

Camol must describe its maturity truthfully:

| Level | Meaning |
|---|---|
| `SPECULATIVE` | The behavior exists as design, mock, or untested adapter contract. |
| `MAPPED` | A real workflow has been recorded end to end and every step maps to an owner, adapter, state, invariant, approval, and evidence requirement. |
| `BACKED` | Camol has executed the workflow successfully with complete replayable evidence, including a controlled failure and recovery. |
| `PROVEN` | Repeated real runs meet the declared quality, cost, liveness, and recovery thresholds. |

Maturity is scope-qualified: `core`, each adapter, and each workflow profile receive
their own status. The core is not `BACKED` merely because one vendor or repository
works, and one speculative adapter cannot downgrade already backed core behavior.

The Buckeye/cmux/GCP/voice flow is the first demanding reference and migration
profile, not Camol's product definition. Its profile acceptance workflow is:

1. Open an orchestrator session in the CLI.
2. Use `/grill` to convert an objective into a human-confirmed plan.
3. Discover, register, or provision eligible execution targets and activate the
   isolated boxes required by the plan within its approved resource envelope.
4. Select hosted or local models per box.
5. Distribute repository work and route all inter-box communication through the
   orchestrator.
6. Inspect each box's state, context, commands, model calls, files, diffs, and evals.
7. Build and test the kinds of repositories used in day-to-day work.
8. Perform an approval-gated forward deployment to GCP.
9. Observe deployment state, logs, health, and relevant runtime behavior from Camol.
10. Exercise a voice-agent workflow, including configuration, deployment, live or
    recorded call behavior, transcripts, model/tool boundaries, and regression evals.
11. Recover from a stopped orchestrator, disconnected worker, failed deployment, and
    rejected evaluation without losing accepted work or repeating unsafe effects.

Section 18 maps the current Buckeye GCP, voice, and cmux profile. That profile is an
adapter target, not a universal architectural dependency. It was reconstructed from
partial logs, a stale checkout, and local read-only probes; the GCP and voice lanes do
not become `BACKED` until an intact current run is captured at the real boundary.

## 3. Terminal experience

Camol installs and behaves like an engineering CLI:

```text
brew install camol
camol
```

The default experience is an interactive terminal UI with a normal non-interactive
command mode for scripts and CI. A daemon owns durable execution so closing the UI
does not abandon a run.

The boot screen uses two brand assets:

- `assets/boot/camol-wordmark.txt`, the geometric `CAMOL` wordmark in the supplied
  isometric style, in the upper-left; and
- `assets/boot/camol-camel-ascii.png`, right-adjusted across the remaining space.

The PNG is the canonical camel artwork. The distribution should include wide,
medium, and compact plaintext derivatives for terminals that cannot display images.
After boot, the main view shows the orchestrator, global plan, obligation progress,
box states, latest evidence, cost, and command prompt.

The upper-right contains a compact dependency rail. It answers whether a capability
is usable now, rather than merely whether a binary exists:

```text
DOCKER ■   GIT ■   GCP □   MODELS ■■↓
```

Selecting a dependency opens its layered readiness evidence, such as CLI presence,
version, daemon reachability, authentication, target scope, required artifacts, and
freshness. Secret values and account identifiers are never rendered there.

`■` is a solid white status box in Camol's default dark terminal theme and means the
dependency is connected and task-ready. `□` means it is known or installed but not
connected/ready; `↓` means its artifact is available to download; `!` means human
action is required; and `↻` means Camol is probing or refreshing it. The glyph and
text detail carry status independently of color. Worker boxes use the same solid-box
language so a glance across the header and box list is consistent.

The bottom edge is a persistent workspace switcher for the orchestrator and a bounded
window into the current N-box fleet:

```text
[0 ORCH] [BOXES 12/47] [!2]  [1 ■ API] ▸[2 ■ VERIFY] [3 □ DEPLOY] [MORE...]
```

The `■` and `□` glyphs continue to report connection readiness; they never mean
"selected." The `▸` cursor, reinforced by inverse-video styling when supported,
identifies the workspace being viewed. `Alt+0` selects the orchestrator; `Alt+1`
through `Alt+9` select visible pinned/recent shortcuts, not permanent box identities.
Left/right or `[`/`]` cycle through the current filtered fleet, `/box <stable-id>`
opens any box, and mouse-capable terminals may select a workspace by clicking it. The
switcher remains visible in orchestrator, repository-graph, eval, and box-detail views.

Selecting a box enters a read-only peer view without changing or interrupting its
execution. That view exposes live terminal output plus `context`, `tools`, `diff`,
`evals`, `events`, and `evidence` subviews. A disconnected box remains selectable and
shows its last durable screen/checkpoint as explicitly stale. Sending input, attaching
an interactive shell, or taking control is a separate, logged takeover action governed
by the frozen plan and its approval policy.

Camol also has a repository graph view backed by a read-only Python crawler. It keeps
three graph layers distinct:

- environment dependencies: tools, daemons, credentials, runtimes, SDKs, and model
  artifacts available to each box;
- source dependencies: repositories, workspaces, packages, modules, build targets,
  tests, containers, services, and deployments; and
- execution overlays: tasks, agents, invariants, evidence, failures, and affected
  deployment identities.

The view supports overview, focused-neighborhood, change-impact, cycle, path-explain,
and graph-diff modes. Large repositories are clustered by workspace/package/service,
cycles are collapsed into explicit strongly connected components, and transitive
noise is hidden by default without deleting the underlying edges. Every node and edge
links back to the crawl receipt or runtime probe that established it. The full crawler
and rendering contract lives in `docs/repository-graph.md`.

Initial interactive commands include:

```text
/grill             develop and challenge a plan
/plan              view, diff, freeze, amend, or migrate the plan
/invariants        inspect invariant ownership, gates, and evidence
/boxes             view all worker boxes
/box <id>          inspect one box
/box next|previous cycle through box workspaces
/evals             inspect every visible evaluator and result
/events            inspect or stream the event ledger
/tools             inspect tool invocations and results
/deps              inspect tools, daemons, auth, runtimes, and downloaded models
/repo crawl         create or refresh the current repository graph snapshot
/repo graph         inspect repository dependencies and execution overlays
/repo impact        show what a selected node can affect downstream
/repo why           explain the evidence-backed path between two nodes
/bench              capture, run, compare, or inspect benchmark campaigns
/model             inspect or select a model adapter
/effort            set reasoning effort within policy
/login              configure a supported provider connection
/refine            initiate a measured candidate hill climb
/pause              checkpoint and pause allowed work
/resume             resume from durable state
```

`/btw` may open a side conversation with the orchestrator, but it cannot change an
active plan unless its result is promoted through the normal amendment path.

## 4. Authority model

### Human authority

Humans own goals, intended behavior, business truth, invariant confirmation,
high-impact credentials, irreversible actions, plan expansion, exceptions, and the
final acceptance of consequential behavior.

### Orchestration-agent authority

The replaceable orchestration agent proposes plans and invariants, decomposes work,
attaches context, routes messages, evaluates discoveries, proposes amendments,
integrates candidates, and requests human decisions. It may exercise only authority
already granted by the frozen plan and gate policies. It is a component used by
Camol, not Camol's identity or source of durable truth.

### Kernel authority

The kernel validates schemas and transitions, assigns leases, checks hashes and
revisions, persists events, enforces gates, runs deterministic checks, applies
budgets, wakes waiting work, and rejects stale or unauthorized claims.

### Box authority

A box executes one leased task inside an isolated workspace. It may inspect and
change resources granted by its capability policy, emit evidence, ask questions, and
propose more work. It cannot create authoritative tasks, amend the plan, weaken an
invariant, alter an evaluator, merge global state, approve its own consequential
claim, or communicate authoritatively around the orchestrator.

## 5. Plans, DAGs, obligations, and evidence

A compiled plan contains:

```text
identity and revision
goal and exclusions
kernel and plan-defined states
task dependency DAG
global and local invariants
obligations and evidence policies
visible evaluator bundle
box capabilities and placement policy
model policy
authorization policy
cost, liveness, and stopping policy
integration policy
amendment and migration policy
```

The DAG and obligation ledger are related but not interchangeable:

```text
Task node -> artifact and evidence -> obligation -> state transition gate
```

The DAG answers what can run next and in parallel. Obligations answer what must be
demonstrated before the system may advance. One task may discharge several
obligations, and one obligation may require evidence from several tasks.

## 6. States and invariant gates

Camol has a small stable kernel lifecycle that every reconciler understands:

```text
DORMANT -> ASSIGNED -> PREPARING -> EXECUTING -> CLAIMED -> EVALUATING
                                      ^                         |
                                      +-------- REPAIRING <-----+

EVALUATING -> READY_TO_MERGE -> INTEGRATING -> ACCEPTED
           -> AWAITING_HUMAN
           -> PAUSED
           -> BLOCKED
```

The plan defines domain states within that lifecycle. A voice-agent plan might define
`CAPTURE_BASELINE_CALL`, `LOCALIZE_TURN_DIVERGENCE`, `DEPLOY_CANDIDATE`, and
`VERIFY_LIVE_CALL`. Each domain-state exit declares the invariants and evidence that
gate it.

An agent never marks a state complete. It emits a completion claim. The gate accepts,
rejects, or escalates that claim.

Gate verdicts are precise rather than a generic pass/fail:

```text
DISPROVED
NOT_DISPROVED_WITHIN_BUDGET
STATISTICALLY_REGRESSED
SUPPORTED_BY_REQUIRED_EVIDENCE
OBSERVATION_INCOMPLETE
EVIDENCE_CONFLICT
HUMAN_ACCEPTED
```

`NOT_DISPROVED_WITHIN_BUDGET` cannot discharge an obligation that requires positive
or real-boundary evidence.

### Invariants

Invariants come from human intention and policy, not from trusting an implementation.
When no trusted implementation exists, the human-confirmed invariant set is the
normative oracle. Existing implementations, alternate candidates, traces, and
differential probes are empirical evidence; disagreement among them does not decide
what is correct.

Effective invariants are inherited:

```text
kernel invariants
  + global plan invariants
  + current domain-state invariants
  + task-local invariants
```

A box may propose a stronger invariant. It cannot weaken an inherited invariant.

Every invariant records:

```text
id, revision, owner, scope, modality
preconditions, predicate, observables
severity, falsification strategies
required evidence, approval policy
```

Useful modalities include `always`, `never`, `eventually`, `until`, `preserved`,
`at_most_once`, and `exactly_once`. Statements without observable signals remain
human acceptance principles until the plan adds instrumentation.

### `/grill` invariant derivation

`/grill` is a mandatory planning stage, not a detachable skill. It asks:

- What must always be true, never happen, or eventually happen?
- What survives retries, crashes, concurrency, restoration, and plan amendment?
- Which effects and information flows are permitted across each boundary?
- Who is authorized to cause each effect?
- What is the source of truth and what counts as equivalent behavior?
- Which inputs are valid, and how would a violation be observed?
- What could pass ordinary tests while remaining catastrophically wrong?

The state sequence is:

```text
GOAL_CAPTURED -> BOUNDARIES_MAPPED -> INVARIANTS_PROPOSED
  -> INVARIANTS_CHALLENGED -> EVALS_COMPILED
  -> AWAITING_CONFIRMATION -> PLAN_FROZEN
```

The human sees and confirms the complete result before work begins.

### Evidence thresholds

All threshold definitions are visible and frozen with the plan:

| Level | Default meaning |
|---|---|
| `basic` | Required build, deterministic checks, evidence kinds, and policy checks pass. |
| `backed` | `basic` plus property/metamorphic checks, bounded adaptive probes, applicable integration checks, zero unresolved critical counterexamples, and reproducible receipts. |
| `critical` | `backed` plus independent evaluation, real-boundary evidence where safe, rollback proof, and explicit human approval. |

These are operational sufficiency thresholds, not mathematical proof. The human may
preauthorize an automatic state transition by approving its exact threshold. Every
automatic transition records which policy, invariant results, artifact hash, plan
revision, and human authorization allowed it.

Human approval is always required to freeze the initial plan, weaken or remove an
invariant, change an active threshold, resolve a consequential ambiguity, authorize
an irreversible external action, materially expand the goal, or accept the final
global outcome in v1.

## 7. Plan amendments and discoveries

Frozen means immutable by revision, not unchangeable forever. Active plan files,
states, invariants, and evals are never edited in place:

```text
discovery -> proposal -> impact analysis -> human confirmation
  -> Plan vN+1 -> affected boxes checkpoint -> migration -> reconciliation
```

Every box is pinned to a plan revision and evaluator-bundle hash. A revision delta
identifies changed states, invalidated completion claims, reusable artifacts,
new obligations, evaluator changes, and the resume location. State-schema changes
require a tested migration. Unaffected boxes may continue only when the impact
analysis demonstrates that their assumptions and dependencies remain valid.

## 8. N-box scheduling, integration, and optional expansion

Camol has no fixed fleet size. A run may have zero to N active leases, where N is the
finite concurrency ceiling in its human-approved resource envelope. The orchestrator
selects how many boxes are useful; the kernel enforces cost, authority, infrastructure,
rate-limit, and concurrency ceilings. Boxes are role-neutral execution containers;
roles, tasks, and models are plan assignments rather than permanent identities. A
model can strategize on one task and build on another, but consequential self-approval
is forbidden.

The default allocation policy is `saturate_connected`: use the maximum already
connected, eligible, and unreserved capacity that can advance ready plan work without
crossing cost, token, time, provider, infrastructure, security, workspace, or
integration limits. It does not invent tasks to occupy capacity. Loading a local
model or provisioning a new paid target requires the corresponding plan authority.
Every allocation records the capacity snapshot, rejected candidates, selected N, and
the reason another lease was expected to help.

The orchestrator may choose any plan-backed topology:

```text
partitioned   boxes work different independent or dependent subtasks
replicated    boxes produce competing candidates for the same logical task
mixed         boxes build, research, verify, debug, deploy, or observe
sequential    a box changes assignment after its lease closes
```

Replicated work uses distinct leased task IDs joined by a candidate/comparison group;
two boxes never hold the same exclusive lease. The human-approved plan decides when
extra implementations or independent verification justify their cost.

Boxes work in isolated worktrees or remote workspaces. The orchestrator owns the
canonical integration workspace:

```text
box candidate -> local gate -> completion claim -> orchestrator integration
  -> global invariant gates -> accept, repair, or roll back
```

On explicit request, Camol may expand a state into multiple implementations. It
compares their behavior, evidence, tradeoffs, and invariant coverage. Semantic
clustering can reveal agreement, but a majority is not truth. Combining candidates
creates a new artifact that must be evaluated again; it does not inherit their pass
status automatically.

## 9. Complete transparency and tool logging

Every evaluation is visible. Every command, model request, tool call, message,
approval, remote action, result, and redaction is represented as structured events.
Terminal text is a projection of this ledger, not the authoritative record.

Evidence and claims carry an epistemic status:

```text
OBSERVED | EXECUTED | DERIVED | INFERRED
HUMAN_REPORTED | UNVERIFIED | CONTRADICTED
```

An inferred claim cannot satisfy a gate requiring executed evidence. Corrections
append a contradiction or supersession event; they never rewrite the original claim.

### Tool invocation envelope

Every tool invocation records at minimum:

```text
invocation_id, run_id, task_id, box_id, turn_id
actor and delegated authority
tool name, adapter kind, and tool-schema hash
start/end timestamps and duration
canonical arguments or a redacted content reference
cwd and workspace/source revision
allowlisted environment identity and runtime versions
approval requirement and approval event
status, exit code, retry/idempotency identity
stdout, stderr, response, and artifact references
before/after mutation receipts where applicable
token, compute, and provider usage
causation_id and correlation_id
event and content hashes
```

Commands are stored as argument arrays rather than ambiguous shell strings. Streaming
tools emit start, bounded chunk/reference, and terminal events. A tool result cannot
be attached to another context packet because the packet and invocation hashes are
part of its receipt.

For critical boxes, the wrapper alone is not considered complete observation. Policy
may require process-tree accounting, filesystem before/after receipts, network
destination observation, credential-access events, cloud audit correlation, and
child-tool causation links. Missing expected telemetry produces
`OBSERVATION_INCOMPLETE`; absence of a parsed event is not evidence that the event did
not occur.

Model-call evidence includes provider, model, effort, sampling configuration, input
message hashes, complete tool definitions, tool choices, output, finish reason,
usage, latency, and relevant cache identifiers. Provider receipts are preferred over
agent-estimated token counts.

External mutations record the requested action, authorization, target identity,
idempotency key, observed result, and a post-action read. Deploying to GCP therefore
requires more evidence than recording that a command exited zero.

### Secrets, redaction, and retention

Full transparency is scoped to authorized humans; it does not mean placing secrets
in plaintext logs. Secrets are references resolved only inside an authorized adapter.
Redaction occurs before ordinary ledger persistence and emits its own record with the
redaction policy and hash of the protected source. When policy permits encrypted raw
retention, the ledger stores only a scoped reference and content hash.

Every run declares retention for transcripts, audio, model inputs, tool results, and
artifacts. Voice recordings and sensitive customer data default to the narrowest
retention and access policy.

### Peering and intervention

Humans and the orchestrator can inspect:

```text
/box 2 status       /box 2 context      /box 2 events
/box 2 transcript   /box 2 tools        /box 2 diff
/box 2 evals        /box 2 evidence     /box 2 shell --read-only
```

Peering is read-only and does not change box state. Mutation requires an explicit,
authorized takeover event. Evidence created after an intervention is linked to that
intervention so reproducibility claims remain honest.

## 10. Evaluation

The generic pre-run evaluator is an evaluator compiler. It specializes visible eval
families from the frozen plan's interface, input domain, preconditions, observables,
equivalence relation, invariants, reference sources, risk, and budget.

The default cascade runs cheap and deterministic checks first:

1. Plan/schema, build, type, lint, and static policy checks.
2. Existing unit and integration tests.
3. Property-based and metamorphic tests.
4. Adaptive differential probes.
5. Side-effect, security, concurrency, and non-functional probes.
6. Real-boundary or end-to-end checks where authorized.
7. Human adjudication for consequential ambiguity.

Adaptive probes may generate new inputs after a completion claim. Their generator,
configuration, budget, and every generated probe are visible. A counterexample can
disprove an invariant or equivalence claim. Exhausting the probe budget without a
counterexample is evidence, not proof.

Coding benchmarks apply repeatable outside pressure at several cadences. Every agent
turn runs repository-local gates; every meaningful candidate hill climb also runs a
small frozen external-suite canary; scheduled campaigns run rotating benchmark
slices; and releases run pinned full cohorts plus long-horizon builds. Camol never
collapses correctness, invariant violations, cost, liveness, recovery, and human
intervention into one score. Harness revisions are tested against matched direct-CLI
and one-active-box controls so orchestration gains can be distinguished from model or
budget changes. The first workflow comparison uses direct Claude CLI, one Claude-backed
Camol box, and Camol's plan-selected adaptive topology against reconstructed real
tasks. The suite selection, paired-task protocol, adapter contract, metrics, and
anti-overfitting rules live in `docs/evaluation-program.md`.

A failed evaluation creates a permanent counterexample linked to the plan revision
and artifact hash:

```text
EVALUATING -> COUNTEREXAMPLE_RECORDED -> FAILURE_CLASSIFIED
  -> REPAIRING | EVAL_AMENDMENT_PROPOSED
  |  ORCHESTRATOR_ESCALATION | PLAN_AMENDMENT_PROPOSED
```

Deterministic precondition checks and plan-defined comparators run before a model
judges whether a behavioral difference is important. Consequential ambiguity goes to
the human. Probe processes cannot change guards, authoritative evals, credentials, or
the systems under comparison.

## 11. Debugger and hill climbs

The debugger is a permanent state protocol:

```text
observed behavior -> target behavior -> reproduction -> localization
  -> bounded experiment -> verification -> visible regression eval
```

Its evidence bundle includes exact commands and tools, relevant transcripts, model
boundaries, environment and deployment identity, inputs and outputs, artifact hashes,
diffs, evaluations, and separate builder and verifier claims.

A hill climb compares a baseline measurement vector with a candidate. Promotion
requires a meaningful gain, no forbidden regression, reproducible evidence, and a
new regression case for any discovered failure family. One aggregate score cannot
hide a safety, correctness, cost, or liveness regression.

## 12. Context continuity and liveness

The complete transcript remains inspectable, but ordinary model calls receive a
compiled context packet:

```text
goal and plan slice
current state and effective invariants
remaining task steps
accepted dependency receipts
latest checkpoint and failure delta
evaluator contract
authority, cost, and turn budget
```

This keeps context bounded while preserving durable truth. Accepted steps remain
accepted unless a plan amendment or integration change explicitly invalidates their
evidence.

For every unfinished obligation, the reconciler must maintain one state:

```text
ready | leased | waiting-with-wake-condition | escalated
awaiting-human | paused | blocked
```

An owner may authorize an unbounded run, but not unobserved waste. Pause policy uses
verified progress—obligations discharged, counterexamples resolved, or accepted
integration evidence—rather than message or token volume. A pause is restartable and
does not erase leases, checkpoints, or history.

Durable watchers use a query, cursor or watermark, deduplication identity, wake
policy, observation window, and terminality condition. A timed-out poll returns to
`waiting`; it does not complete the watcher. A watcher never reports the same source
event twice unless a later revision explicitly reopens it.

## 13. Replaceable adapters

### Execution substrate

Every runner implements the same lifecycle:

```text
discover -> probe -> provision? -> prepare -> launch -> lease
  -> stream events -> checkpoint -> stop -> destroy?
```

Initial substrates are:

- local processes and PTYs;
- isolated Git worktrees;
- cmux as a compatibility and migration adapter;
- SSH-connected machines;
- provisioned VMs and hosted workers; and
- GCP command/API adapters for approved deployment and observation.

Camol never treats an open terminal, running VM, or successful SSH connection as a
ready agent. Readiness requires transport, runtime, agent, checkout, and project
evidence.

The worker registry may contain any number of discovered, dormant, historical, or
unavailable targets. Each run's plan-approved resource envelope supplies its finite
concurrency ceiling. Targets created outside Camol enter through an adoption flow:

```text
discover -> establish identity -> inspect work -> bind project
  -> register -> task-specific readiness probe
```

Task readiness includes the required language toolchains, dependencies, credentials,
services, database/migration state, source revision, and adapter capabilities. An
execution target that is alive but cannot execute its assigned task is not ready.

The terminal is a detachable client or an optional PTY session, not an execution
target or connectivity boundary. A control-plane daemon may run locally, inside a VM,
or on another durable target; closing the terminal must not end the run. The complete
identity, transport, lifecycle, N-box admission, and scaling contract lives in
`docs/execution-topology.md`.

### Model providers

Hosted and local models share a capability-based model adapter. Connections use
supported provider APIs, CLIs, OAuth/device flows, or local endpoints; Camol does not
scrape consumer browser sessions. Credentials live in the OS keychain or a dedicated
secret manager and are granted per adapter and task.

Development may use the owner's accounts for adapter testing, but those accounts are
never Camol infrastructure or distribution credentials. Public use is bring-your-own
connection: provider keys, supported OAuth/device authorization, workload identity,
existing permitted CLI login, or a local endpoint. Agent and model selection is based
on task requirements, observed readiness, policy, cost/latency, and version-scoped
evaluation evidence. The full capacity, suitability, public credential, and workflow
profile contract lives in `docs/capacity-and-provider-model.md`.

Selection considers context size, tool support, structured output, latency, cost,
local hardware, privacy, and task capability. Placement policies include
`local_only`, `local_first`, `private_data_local`, `cost_first`, and
`capability_first`. Model binaries and dependencies may be downloaded during an
approved preparation state and pinned for offline execution.

### Voice-agent observability

Voice work requires additional evidence types:

```text
audio reference and retention policy
timestamped speaker transcript
turn and session identifiers
ASR, endpointing, model, tool, and TTS boundaries
latency by stage
tool requests and results
deployment/configuration identity
expected and observed terminal conversation outcome
```

The first divergent event, not the final conversational symptom, drives debugging.

## 14. Security and external effects

- Tool permissions are deny-by-default capabilities attached to a lease.
- Read, write, execute, network, deploy, credential, and destroy are distinct grants.
- Destructive and externally visible actions are human-gated unless the exact action
  class and target scope were preauthorized.
- Remote effects use idempotency keys or explicitly declare that replay is unsafe.
- Raw worker output and retrieved content are untrusted evidence, never instructions.
- Evaluators are isolated from builders and cannot be weakened by probe code.
- Artifacts and evidence are content-addressed; accepted results are bound to source,
  environment, plan, and evaluator revisions.
- The orchestrator performs post-action reads to distinguish command success from
  real system success.

Remote mutations have explicit outcome states:

```text
EFFECT_REQUESTED -> EFFECT_CONFIRMED | EFFECT_REJECTED | EFFECT_UNKNOWN
```

`EFFECT_UNKNOWN` requires a provider read and reconciliation before retry. Camol does
not assume that a timed-out deployment, migration, message, or payment failed.

## 15. Current implementation and gaps

The repository already contains an executable local control-plane slice:

- a JSON runbook;
- an arbitrary non-empty configured process-worker pool;
- a positive per-run `max_concurrency` ceiling;
- a task DAG and capability scheduler;
- SQLite event replay;
- frozen plan digests and human approval;
- task leases, compact checkpoints, evidence requirements, verifier commands,
  retries, token budgets, and restart recovery;
- debugger-case promotion; and
- vector-based hill-climb comparison.

The following specification areas are not yet implemented and remain speculative:

- first-class invariant and obligation records;
- `basic`, `backed`, and `critical` gate compilation;
- plan revisions and state migrations;
- the full tool-invocation envelope and artifact store;
- epistemic evidence status, observer-readiness gates, and evidence-conflict states;
- durable cursor-based watchers and cross-system identity correlation;
- connected-capacity inventory, reservations, and suitability-based agent/model selection;
- dynamic worker discovery, authenticated adoption, and provisioning;
- marginal-value scale-out, admission control, drain, and backpressure policies;
- salvage-gated teardown and remote-effect reconciliation;
- versioned deployment identities, CI waivers, and VCS integration relationships;
- interactive `/grill` and terminal UI;
- isolated Git worktree execution;
- real Codex, Claude, local-model, cmux, SSH, and GCP adapters;
- adaptive differential evaluation;
- adaptive box-topology policy and first-class candidate/comparison groups;
- external coding-suite adapters, canary campaigns, and benchmark ablations;
- read-only box attachment and explicit takeover;
- voice-agent evidence ingestion;
- dependency readiness inventory and repository graph crawling/rendering; and
- Homebrew distribution.

## 16. Build sequence

The implementation-ready, pull-request-sized version of this sequence—including the
`READY_TO_LEASE` invariant, isolation tiers, first Claude/Fable profile, tests, and
promotion gates—is maintained in `docs/v0-build-plan.md`. That plan is the operational
roadmap from the current deterministic simulator to the first real-agent dogfood run.

1. Stabilize generic invariant, obligation, gate, approval, epistemic-status,
   resource-envelope, and plan-revision schemas with migration tests.
2. Add the complete tool-event envelope, central event streaming, and
   content-addressed artifact storage.
3. Add connected-capacity inventory, reservations, suitability matching, and the
   `saturate_connected` allocation policy.
4. Wrap real Claude, Codex, and local-model agents through the same versioned adapter
   contract using owner-provided test connections without embedding those accounts.
5. Split the supervised control-plane daemon from detachable CLI/TUI clients and add
   the authenticated execution-target/worker protocol.
6. Add dynamic worker discovery/adoption/provisioning, task readiness, admission
   control, N isolated workspaces, drain, salvage, and teardown.
7. Implement `/grill`, plan amendments/migrations, read-only box inspection, and
   replayable terminal/model/tool streams.
8. Add dependency readiness probes, repository crawl snapshots, impact queries, and
   the terminal graph/fleet views.
9. Implement cursor-based watchers, reconciliation, evidence invalidation, liveness
   heartbeats, event backpressure, and multi-run resource accounting.
10. Implement the evaluator compiler, adaptive counterexample loop, coding-suite
    adapters, and direct-agent versus Camol benchmark campaigns.
11. Back the generic local-repository and multi-service reference profiles, including
    worker/daemon interruption and clean replay.
12. Treat the reconstructed Buckeye worklog as one sanitized profile input; then
    capture and back its intact cmux/GCP/voice path without making those technologies
    core dependencies.
13. Add signed packaging, Homebrew distribution, provider-connection documentation,
    migration/recovery testing, and the public-use security boundary.
14. Add the polished TUI with the supplied Camol branding after the control plane and
    evidence semantics are backed.

Post-v1, add an optional spatial build visualizer that projects repository topology,
box placement, task flow, build artifacts, and evaluation state into a navigable
almost-3D scene. It is a read-only projection of the same graph snapshots and event
ledger, never a second source of orchestration truth. The v1 dependency and repository
views remain terminal-native and two-dimensional.

## 17. Decisions frozen for the initial build

- Camol is the harness and deterministic control plane, not an agent, model, or
  provider account. Orchestration and worker agents are replaceable adapters.
- All evals, generators, thresholds, results, tool calls, and approval reasons are
  visible to authorized humans.
- Human-confirmed invariants gate plan-defined domain states.
- Human-approved evidence thresholds may authorize routine automatic transitions.
- The initial and final global gates require human confirmation.
- Boxes may propose but not authorize new work or weaker truth conditions.
- Newly discovered work enters through a versioned plan amendment.
- Peering into boxes is read-only; mutation requires explicit takeover.
- There is no product-level box-count constant. Every run declares a finite,
  human-approved concurrency and resource envelope; the orchestrator selects any
  justified active count within it. Boxes may receive different tasks, distinct
  candidate tasks for the same logical objective, or mixed roles. Multiple
  implementations are optional and explicitly requested or approved by the plan.
- The default allocation mode maximizes useful already-connected capacity for ready
  work; loading models or provisioning paid targets requires plan authority.
- Owner accounts may validate adapters during development but are never embedded in
  Camol. Public distribution uses bring-your-own provider or local-model connections.
- Buckeye/GCP/voice/cmux is a reference profile, not Camol's universal workflow.
- Local-only, hybrid, cmux-compatible, and VM-backed execution use the same protocol.
- Integration is orchestrator-owned and every synthesized artifact is reevaluated.
- Long-running sessions are owner-controlled and pause on declared lack of verified
  progress rather than arbitrary wall time alone.
- Each candidate hill climb receives a frozen coding-benchmark canary; full suites
  and long-horizon build campaigns run at scheduled or release cadence.
- The spatial, almost-3D build visualizer is explicitly post-v1.

## 18. Buckeye adapter profile and initial operating defaults

The previously open design inputs are resolved below far enough to build the adapter.
This does not upgrade the evidence posture: an implementation description and an
intact Camol-produced run are different things.

### 18.1 Reconstructed workflow

The recoverable workflow is:

```text
human objective and planning dialogue
  -> local orchestrator workspace in cmux
  -> remote cmux workspaces attached through SSH to persistent tmux sessions
  -> Claude/Codex-style agents working in isolated VM checkouts and branches
  -> commit, push, PR, review, CI, and release-candidate custody
  -> human-authorized staging deployment
  -> exact GCP revision, traffic, health, and log readbacks
  -> durable voice-call watcher with a timestamp/job watermark
  -> evidence-backed debug case, repair, redeploy, and regression evaluation
```

Local probes observed a working cmux socket, one local orchestrator workspace, and
multiple SSH/tmux-backed remote workspaces. The installed cmux exposes structured VM,
workspace, terminal-replay, screen-read, and event APIs, so Camol should use those
APIs when available and keep raw SSH/PTTY as a fallback. cmux cloud authentication
was signed out, while already-open SSH workspaces remained visible. These are
separate readiness dimensions.

This is not the required intact trace. Some VM-local histories were deleted before
the worklog was assembled, and current gcloud authentication requires interactive
reauthorization. The first adapter proof therefore starts a new, non-PHI staging run
after the human completes `/login`; it records every command and tool envelope from
plan approval through one controlled deployment, one watcher observation, and one
failure/recovery. Authentication refusal is `AUTH_REQUIRED`, not an agent failure.

### 18.2 GCP deployment profile

The staging profile is fixed as follows unless a later live inventory contradicts it:

| Boundary | Mapped target |
|---|---|
| Project and region | `buckeye-hub`, `us-central1` |
| Primary compute | Cloud Run services for API, agent-runtime worker, web, and portal; PaddleOCR is a declared supporting service |
| Voice compute | Cloud Run worker pool `enrollment-voice-sarah`, reported by the worklog but absent from the stale checkout's deploy definition |
| Images and builds | Artifact Registry repository `buckeye`; release-candidate images are digest-pinned; Cloud Build is used by the staging-only break-glass path |
| Database | Cloud SQL for PostgreSQL; the managed staging release binds an exact fresh database generation rather than mutating the legacy database |
| Network | staging VPC and regional subnet; the database path is private rather than laptop-accessible |
| Secrets and identity | Secret Manager numeric versions, service-specific runtime service accounts, and GitHub Actions Workload Identity Federation |
| Durable objects | GCS for Terraform state, pre-deploy database exports, documents, and separately scoped voice recordings |
| Observation | Cloud Logging plus Cloud Run service, revision, worker-pool, traffic, and health readbacks |

Production is a separate `buckeye-hub-prod` profile and is outside the first backed
slice. Camol never infers production authorization from staging authorization.

Credential boundaries are explicit:

- Local gcloud OAuth belongs to the human and can be refreshed only through an
  interactive gate. Camol stores an account/project fingerprint and expiry state,
  never the credential.
- GitHub Actions uses short-lived WIF/OIDC authority for the deploy service account.
- API and worker runtimes use different service accounts, database logins, and exact
  numeric Secret Manager bindings.
- cmux cloud identity, local cmux socket access, SSH transport identity, VM identity,
  agent login, and GCP identity are independent readiness checks.
- LiveKit-to-GCS recording currently requires a narrowly scoped recording-writer key
  because the writer runs outside GCP. It is an exception with rotation and audit
  obligations, not a general credential pattern.

### 18.3 Deployment and recovery contract

The supervised release path pins one source SHA, authenticates preparation,
provisioning, certification, and deployment receipts, deploys four digest-pinned
zero-traffic revisions, smokes the API candidate, activates the worker, proves it
healthy, and then moves API, web, and portal traffic to exact revisions. Every
mutation is followed by a readback. Mutable `latest` traffic targets do not count as
deployment evidence.

Camol must distinguish three recovery regimes:

1. Before traffic activation, a failed candidate is abandoned with no live traffic
   shift.
2. During the initial workload-control cutover, routing back to a legacy revision or
   reconnecting the old database is forbidden. Recovery is repair-forward or a
   known-good workload-control commit provisioned against another fresh database.
   Migrations are forward-only.
3. A later compatible post-cutover rollback is allowed only after compatibility with
   the active schema, database generation, runtime bindings, and bounded worker
   overlap is independently proven. The current evidence does not authorize that
   path.

Older staging scripts also expose pre-deploy GCS export and import commands. Those
are useful evidence and a break-glass mechanism, but they do not override the newer
clean-cutover recovery contract. Mixing commands across workflow generations is a
gate failure.

### 18.4 Voice provider and retention profile

The mapped voice stack is:

| Function | Provider or boundary |
|---|---|
| Session, SIP, dispatch, and egress | LiveKit Cloud |
| Speech to text | Soniox `stt-rt-v5` by default; AssemblyAI `u3-rt-pro` as explicit fallback |
| Conversational model | OpenAI `gpt-5.4-mini` by default; Baseten-hosted `zai-org/GLM-5.2` as an explicit alternate |
| Text to speech | Cartesia `sonic-3.5` |
| Noise cancellation | optional Krisp BVCTelephony family |
| Audio storage | LiveKit egress to a dedicated GCS recordings bucket |
| Optional model tracing | Braintrust |

Provider choice and resolved model identity are startup evidence. Missing credentials
fail loudly; an undeclared fallback cannot satisfy a gate.

Camol's safe default is metadata-rich and content-minimal:

- retain PHI-free event envelopes, timestamps, correlation identifiers, provider and
  deployment identities, redaction receipts, verdicts, and content hashes;
- do not copy raw audio, raw transcripts, provider prompts, or patient-bearing tool
  results into the ordinary ledger or Git;
- keep sensitive content behind an encrypted, role- and purpose-scoped external
  reference with an explicit TTL and access audit only when a human approves it;
- permit committed evaluator fixtures only after deterministic de-identification and
  human review; and
- require a separate retention decision before any real-PHI recording campaign.

The inspected checkout is not sufficient proof of a safe production posture. It
contains an encrypted transcript column but also a transitional plaintext column and
a decryption fallback, while the recording runbook describes a staging bucket with
soft-delete but no CMEK or locked retention policy. Before the voice lane can become
`BACKED`, Camol must inspect the live schema and bucket policy and return
`SECURITY_BLOCKED` if any reachable plaintext path, undefined deletion schedule, or
unapproved recording policy remains.

### 18.5 Provisional numeric defaults

These values are `PROVISIONAL_DEFAULT`, visible during `/grill`, and frozen into each
plan revision. They may be changed only by a normal amendment. They are designed to
fail into review or a restartable pause, not to discard work.

| Concern | Initial default |
|---|---|
| Worker heartbeat | emit every 30 seconds while active; after 3 missed heartbeats (90 seconds), enter `LEASE_SUSPECT` and probe transport, process, and tool state |
| Lease recovery | do not reassign before 10 minutes, and then only after proving the worker dead and proving no external effect is in flight; otherwise use `EFFECT_UNKNOWN` |
| Semantic stall | `STALL_REVIEW` only after both 15 minutes and 3 completed agent turns produce no new accepted obligation, counterexample resolution, or integration evidence; 2 consecutive windows pause or escalate |
| Bootstrap token profile | 4,000 tokens per turn, 400 reserved for the checkpoint, 30,000 per run, and 6 turns per task; warn at 80% and stop before the next turn at 100% |
| Paid-provider spend | every hosted run must freeze an absolute USD ceiling; warn at 80% and require a human extension before crossing 100%; there is deliberately no hidden product-wide dollar amount |
| Adaptive probes | `basic`: deterministic suite only; `backed`: at most 12 generated probes or 15 minutes; `critical`: at most 24 probes or 45 minutes plus an independent verifier; a decisive counterexample stops the gate early |
| API candidate smoke | `/startupz`, `/readyz`, and `/health` must each return 200 within 6 attempts, 5 seconds apart, with a 10-second request timeout |
| Worker activation | at most 30 readiness polls, 2 seconds apart; exact revision and digest, desired replicas `1`, `ContainerHealthy=True`, and `Ready=True` are all required |
| Post-promotion soak | 10 polls over 5 minutes at 30-second spacing, 100% required health success, unchanged deployment identity, and zero new P0/security signatures |
| Voice adapter `BACKED` smoke | 5 controlled non-production calls; 5/5 terminal and correctly correlated; 5/5 expected outcomes delivered; 100% required telemetry; zero P0, PHI leak, mid-sentence TTS truncation, or unexplained silence of 12 seconds or more |
| Voice quality `PROVEN` | at least 100 completed trials and enough successes for the 95% Wilson lower confidence bound to meet the plan's target rate; every safety invariant still requires zero violations |
| CI waiver | maximum lifetime 14 days, with owner, reason, compensating evidence, and remediation obligation |
| Teardown | zero required unpushed commits, zero required untracked artifacts, zero in-flight effects, and zero unacknowledged events |

The five-call voice gate proves integration, not population-level quality. Long-running
work may raise or remove turn and run caps through human approval, but liveness,
checkpointing, provider spend, and semantic-progress observation remain mandatory.

### 18.6 Initial external-action policy

No action is implicitly preauthorized merely because a tool can perform it.

| Policy | Action classes |
|---|---|
| Automatic inside a frozen plan | scoped reads; box-local edits; builds, tests, and evals; local model execution; read-only peering; PHI-safe log queries; orchestration messages |
| Plan-preauthorized within exact ceilings | hosted model calls; creation/restart of Camol-owned ephemeral workers within the run's approved concurrency, provider, and spend envelope; pushes to dedicated task branches; draft PR creation; teardown of Camol-owned disposable workers after a complete salvage receipt |
| Human approval per occurrence | OAuth/device login; secrets or IAM changes; merge to a protected branch; CI waiver; staging deployment; database migration or restore; real voice call/message; adopted-VM destruction; any production mutation |
| Never automatic | weakening an invariant or evaluator; treating `EFFECT_UNKNOWN` as failed and blindly retrying; exposing PHI or credentials; bypassing a gate; routing initial-cutover traffic to a legacy revision |

Approval binds the exact target, artifact/deployment digest, scope, expiry, maximum
cost, and action class. A material difference creates a new approval request.

### 18.7 Remaining proof gates, not design ambiguity

Four facts still require live or owner evidence before the relevant lane is `BACKED`:

1. Capture the intact Camol-produced command/tool trace after the human reauthenticates
   cmux/GCP; do not attempt to reconstruct deleted VM history.
2. Reconcile the current live voice worker-pool deployment definition with the service
   release workflow and record which revision owns each call.
3. Obtain the human/compliance decision for real-PHI audio and transcript retention,
   including deletion schedule, region, CMEK, locked-retention posture, and permitted
   reviewers.
4. Replace the provisional cost, latency, and reliability defaults with baselines from
   repeated Camol-observed runs.

## 19. Requirements backed by the Buckeye worklog

### 19.1 Evidence boundary

The external worklog covers 2026-08-26 through 2026-09-02. Its summary reports 815
typed prompts across seven Claude sessions, 1,189 authored commits, 5,703 distinct
files touched, 108 opened pull requests, and fourteen sandbox VMs active at the end
of the work period. It also contains GCP and voice-call forensic notes, including
PHI-bearing transcripts that must remain outside Git.

Camol did not produce this bundle and has not independently replayed its source
systems. These facts therefore have `BUNDLE_REPORTED` provenance. The bundle is
sufficient to derive product requirements but not to declare an adapter `BACKED`.

### 19.2 Workflow crosswalk

| Observed workflow | Required Camol primitive |
|---|---|
| Repeated planning and specification conversations | `/grill`, semantic decisions, frozen plan revisions |
| Many parallel and disposable VMs | worker discovery, adoption, leases, readiness, salvage, teardown |
| Large branch and PR volume | VCS integration graph and evidence invalidation |
| CI repair, monitoring, and temporary skips | visible gates, durable watchers, expiring waivers |
| Manual and automated GCP deployment | approval, deployment identity, effect reconciliation, post-action reads |
| Live GCP log investigation | bounded queries, watermarks, deduplication, cross-source correlation |
| Voice-call review and debugging | observation contracts, terminality gates, statistical campaigns, restricted evidence |
| Competing implementations and generated probes | optional candidate expansion and differential evaluation |
| Corrections to earlier diagnoses | epistemic status, contradiction events, observer-readiness gates |
| Work rescued before VM deletion | central streaming and salvage-gated teardown |

### 19.3 Observer readiness

The worklog records a debugger reporting zero tool calls while the system had accepted
hundreds of voice events. Its parser expected an obsolete log representation. This
creates a kernel requirement: an evaluator must prove its observation channel is
ready before interpreting absence.

```text
OBSERVER_DISCOVERED
  -> SCHEMA_IDENTIFIED
  -> FIXTURE_DETECTED
  -> CORRELATION_PROVEN
  -> OBSERVATION_READY
```

The readiness receipt includes source schema/version, query window, parser version,
fixture or heartbeat, expected event classes, correlation keys, freshness, and known
blind fields. A broken instrument blocks consequential evaluation before product code
is changed.

### 19.4 Correctable knowledge

The worklog explicitly corrects earlier diagnoses after new executed evidence. Camol
must preserve that distinction:

```text
Claim A [INFERRED]
  -> contradicted by Evidence B [EXECUTED]
  -> Claim C [DERIVED]
```

The UI shows the current conclusion without hiding the reasoning history. Debug and
evaluation packets receive the corrected claim plus the evidence that superseded it,
not the obsolete diagnosis as unqualified context.

### 19.5 Fleet adoption and salvage

The worklog records machines created outside the controller, stale local inventory,
provider schema drift, unpushed commits, untracked artifacts, and VM-local prompts
lost after deletion. Therefore:

- Provider schemas use versioned decoding and capability negotiation; optional or
  renamed provider fields do not invalidate the entire fleet response.
- Provider identity, harness identity, VM name, box identity, project, branch, and
  lease are distinct fields.
- A discovered machine can be adopted without pretending Camol created it.
- Event and artifact streaming begins before disposable work is trusted.
- Teardown requires a salvage receipt proving no required unpushed commit, untracked
  artifact, running invocation, or unacknowledged event remains.
- Provider deletion and session/logout cleanup are separate recorded effects.

### 19.6 VCS and integration graph

The worklog contains stacked, folded, superseded, closed, merged, and still-open pull
requests. Task dependencies alone cannot express this. Version-control objects
support:

```text
depends_on | supersedes | absorbs | conflicts_with
deploys | backports | abandons
```

Every candidate records source base, head, dirty-state digest, commit identities,
remote push receipt, PR identity, checks, review state, and integration result. A
merge, rebase, fold, environment change, or plan amendment computes which evidence
must be rerun.

### 19.7 Deployment truth and waivers

A deployment identity is a vector:

```text
source revision
build identity
image digest
service revision
configuration digest
infrastructure resources
database migration head
deployment timestamp and target
```

Environment-only changes create a new identity even when the image is unchanged.
Post-deployment evidence is invalid if it cannot be bound to that vector.

A temporarily bypassed CI or release condition creates a visible waiver rather than
a green check:

```text
failed invariant, reason, owner, scope
created_at, expires_at
compensating evidence
remediation obligation
```

Expired waivers block the gate. Results produced under a live waiver display
`GREEN_WITH_WAIVER`.

### 19.8 Durable watchers

The worklog repeatedly re-arms timed monitors and separately maintains a review
watermark so calls are not reported twice. Camol adopts the latter as the contract.
Watchers survive UI exit and orchestrator restart, resume from the last acknowledged
cursor, distinguish no-new-data from broken observation, and wait for terminality
before judging a live call.

### 19.9 Identity and correlation

Voice work spans caller or participant identity, call job, voice session, screening,
worker generation, deployment, turn, tool invocation, database write, and terminal
outcome. Multiple calls may belong to one screening and may overlap. Camol stores
these as typed correlation links rather than assuming one call equals one run.

Evidence sources may disagree. The reconciler records `EVIDENCE_CONFLICT`, identifies
the conflicting windows and correlation assumptions, and opens an experiment or
human gate. It does not manufacture a winner.

### 19.10 Voice observation contracts

A voice campaign declares:

```text
scenario distribution and valid inputs
deployment/model/tool identities
audio, transcript, state, and tool observables
call terminality condition
trial count and controlled variables
acceptable stochastic variance
latency and outcome thresholds
retention and redaction policy
```

A single call may disprove a safety or state invariant when its observation is
decisive. Quality and reliability claims require a declared campaign and cannot be
established by one favorable call. A correct internal refusal that produces silence
still violates the end-to-end caller outcome, demonstrating why local correctness
does not imply global correctness.

### 19.11 Human-gate scale

The recorded work volume makes per-command human approval impractical. Human gates
belong at semantic, authority, risk, and state boundaries. Routine tool calls proceed
only under a visible preauthorized policy. Plan expansion, weaker invariants,
consequential ambiguity, production mutation, live waivers, and final acceptance
remain human decisions.

## 20. Buckeye reference-profile vertical slice

The `profile:buckeye` maturity claim earns `BACKED` only after one real run completes
this sequence with replayable evidence. Passing it does not automatically back the
core, another workflow profile, or every adapter:

```text
human /grill session
  -> frozen plan and invariant gates
  -> discover or adopt one real worker
  -> prove task-specific readiness
  -> stream every command, tool, model, and artifact event centrally
  -> bind work to an isolated branch/worktree
  -> produce and integrate one reviewed change
  -> pass CI or display an explicit unexpired waiver
  -> obtain human staging-deploy approval
  -> deploy and reconcile the exact GCP deployment identity
  -> prove observer readiness
  -> observe one terminal voice call from a durable watcher cursor
  -> create one evidence-backed debug case
  -> repair, redeploy, and rerun the affected gates
  -> restart the orchestrator without losing or duplicating work
  -> salvage and tear down the worker without losing required evidence
  -> obtain final human acceptance
```

The controlled proof must include at least one simulated or safe occurrence of an
observer blind spot, remote `EFFECT_UNKNOWN`, rejected evaluation, expired waiver,
and interrupted worker. Camol remains `SPECULATIVE` for any adapter or evidence lane
that is mocked rather than exercised at its real boundary.
