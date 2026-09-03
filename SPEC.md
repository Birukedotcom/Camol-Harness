# Camol product specification

Status: `SPECULATIVE`

This document is the authoritative product specification for Camol. The executable
protocol details in `docs/` refine this document but do not override it. A run becomes
authoritative only when a human approves the exact digest of its compiled plan.

## 1. Product thesis

Camol is a terminal-native, local-first control plane for human-guided agent work. A
human develops a plan with one orchestrator, freezes that plan, and lets the
orchestrator distribute bounded work among three agent boxes. The harness continues
until state gates are satisfied, a human decision is required, or an owner-defined
pause condition is reached.

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
- replaceable local, VM, model, and cloud adapters.

The orchestrator reasons. The deterministic kernel owns truth about state, leases,
revisions, evidence, approvals, retries, wakeups, and terminal conditions.

## 2. Product maturity and the replacement threshold

Camol must describe its maturity truthfully:

| Level | Meaning |
|---|---|
| `SPECULATIVE` | The behavior exists as design, mock, or untested adapter contract. |
| `MAPPED` | A real workflow has been recorded end to end and every step maps to an owner, adapter, state, invariant, approval, and evidence requirement. |
| `BACKED` | Camol has executed the workflow successfully with complete replayable evidence, including a controlled failure and recovery. |
| `PROVEN` | Repeated real runs meet the declared quality, cost, liveness, and recovery thresholds. |

The v1 product is not `BACKED` until it can replace the owner's current cmux workflow
for ordinary engineering work. The acceptance workflow is:

1. Open an orchestrator session in the CLI.
2. Use `/grill` to convert an objective into a human-confirmed plan.
3. Attach or provision three isolated boxes locally, through cmux, or on VMs.
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

The exact GCP product, voice stack, and current cmux interaction are discovered by
recording the real workflow. Camol must not silently assume Cloud Run, GKE, Compute
Engine, or any particular voice provider.

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

- the geometric `CAMOL` wordmark from the design conversation in the upper-left;
  its exact raw text asset remains pending because chat rendering altered several
  slash and underscore glyphs; and
- `assets/boot/camol-camel-ascii.png`, right-adjusted across the remaining space.

The PNG is the canonical camel artwork. The distribution should include wide,
medium, and compact plaintext derivatives for terminals that cannot display images.
After boot, the main view shows the orchestrator, global plan, obligation progress,
box states, latest evidence, cost, and command prompt.

Initial interactive commands include:

```text
/grill             develop and challenge a plan
/plan              view, diff, freeze, amend, or migrate the plan
/invariants        inspect invariant ownership, gates, and evidence
/boxes             view all worker boxes
/box <id>          inspect one box
/evals             inspect every visible evaluator and result
/events            inspect or stream the event ledger
/tools             inspect tool invocations and results
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

### Orchestrator authority

The orchestrator proposes plans and invariants, decomposes work, attaches context,
routes messages, evaluates discoveries, proposes amendments, integrates candidates,
and requests human decisions. It may exercise only authority already granted by the
frozen plan and gate policies.

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

## 8. Three boxes, integration, and optional expansion

V1 has three simultaneously available agent slots, though fewer may be active. Roles
are plan policies rather than permanent identities. A model can strategize on one
task and build on another, but consequential self-approval is forbidden.

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

### Model providers

Hosted and local models share a capability-based model adapter. Connections use
supported provider APIs, CLIs, OAuth/device flows, or local endpoints; Camol does not
scrape consumer browser sessions. Credentials live in the OS keychain or a dedicated
secret manager and are granted per adapter and task.

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

## 15. Current implementation and gaps

The repository already contains an executable local control-plane slice:

- a JSON runbook;
- exactly three process-backed boxes;
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
- interactive `/grill` and terminal UI;
- isolated Git worktree execution;
- real Codex, Claude, local-model, cmux, SSH, and GCP adapters;
- adaptive differential evaluation;
- read-only box attachment and explicit takeover;
- voice-agent evidence ingestion; and
- Homebrew distribution.

## 16. Build sequence

1. Record and map one current cmux-to-GCP workflow, including a voice-agent case.
2. Add invariant, obligation, gate, approval, and plan-revision schemas to the kernel.
3. Add the complete tool-event envelope and content-addressed artifact storage.
4. Prepare three isolated Git worktrees and real local agent adapters.
5. Add read-only box inspection and replayable terminal/model/tool streams.
6. Implement plan amendments, migration, reconciliation, and liveness heartbeats.
7. Wrap cmux/SSH and the actual GCP deployment/observation path.
8. Implement the evaluator compiler and adaptive counterexample loop.
9. Run the recorded workflow, force failures, recover, and promote the milestone from
   `MAPPED` to `BACKED`.
10. Add the polished TUI, supplied Camol branding, packaging, and Homebrew formula.

## 17. Decisions frozen for the initial build

- All evals, generators, thresholds, results, tool calls, and approval reasons are
  visible to authorized humans.
- Human-confirmed invariants gate plan-defined domain states.
- Human-approved evidence thresholds may authorize routine automatic transitions.
- The initial and final global gates require human confirmation.
- Boxes may propose but not authorize new work or weaker truth conditions.
- Newly discovered work enters through a versioned plan amendment.
- Peering into boxes is read-only; mutation requires explicit takeover.
- Three boxes are the v1 execution target; multiple implementations are optional and
  explicitly requested.
- Local-only, hybrid, cmux-compatible, and VM-backed execution use the same protocol.
- Integration is orchestrator-owned and every synthesized artifact is reevaluated.
- Long-running sessions are owner-controlled and pause on declared lack of verified
  progress rather than arbitrary wall time alone.

## 18. Remaining design inputs

The architecture can progress with conservative defaults, but these inputs are still
needed before the relevant adapters become `BACKED`:

1. A captured example of the actual cmux workflow, from task intake through GCP
   deployment and monitoring.
2. The GCP products, projects, regions, credential boundaries, and rollback methods
   used in that workflow.
3. The voice-agent providers and the evidence that may be retained safely.
4. Concrete numeric thresholds for cost, stall detection, probe budgets, and real
   deployment health.
5. Which external action classes, if any, the human wants to preauthorize rather than
   approve per occurrence.
