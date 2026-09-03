# Camol architecture

Status: executable Python control-plane foundation.

The consolidated product requirements and decisions are maintained in
[`../SPEC.md`](../SPEC.md). This document describes the current executable
architecture and narrower implementation model.

## 1. Product thesis

Agent work becomes slow and unreliable when execution is spread across chat history,
terminal panes, repo-local skills, human memory, and ad hoc status messages. The fix
is not a more elaborate prompt. The fix is a control plane that makes work and its
evidence explicit.

Camol has one authoritative control plane and an N-box worker pool. Each run has a
finite human-approved resource envelope. A replaceable orchestration agent proposes
the boxes its plan can justify inside that envelope; the kernel admits and leases
them. Workers may run beside the control plane or on remote execution targets, but
they never become independent sources of truth. They lease plan-selected tasks, emit
evidence, ask questions, propose follow-up work, and return claims. The kernel decides
what enters the run and what is accepted.

Camol itself is the harness, not the reasoning agent. In this document,
"orchestrator" means the replaceable orchestration-agent component operating through
the deterministic control plane. Agent proposals are inputs to kernel decisions, not
authoritative state mutations.

The first reusable proof case is Sarah in Enrollment Hub:

- live call behavior supplies the observation;
- logs, commands, model-boundary captures, transcripts, and code state supply the
  evidence;
- the intended conversation supplies the target behavior;
- a bounded change supplies the experiment;
- a new deterministic or recorded case supplies the permanent eval.

Sarah remains a domain agent. The debug lifecycle that repaired Sarah becomes a
general harness protocol.

## 2. The control-plane shape

```text
                              human/team policy
                                     |
                                     v
                         +-----------------------+
                         | CAMOL CONTROL PLANE   |
                         | orchestration adapter |
                         | DAG, leases, evidence |
                         | gates, budget, state  |
                         +----+----------+-------+
                              |          ^
                   task lease |          | events / claims / questions
                              v          |
                   +---------------------+----------------------+ ... +
                   |                     |                           |
             +-----+------+        +-----+------+              +-----+------+
             | box A      |        | box B      |              | box N      |
             | role/task X|        | role/task Y|              | role/task X|
             | worker     |        | worker     |              | worker     |
             +------------+        +------------+              +------------+
                   |                     |                           |
                   +---------------------+----------------------+ ... +
                                         |
                                         v
                              append-only event ledger
                                         |
                     +-------------------+-------------------+
                     |                   |                   |
                  replay/UI          debugger          eval/hill climb
```

The transport can be local IPC, authenticated worker RPC, SSH, a provider API,
tmux/cmux compatibility, or another substrate. The execution target might be the
control-plane host, a VM, container, pod, bare-metal host, or provider job. Transport
and target are both replaceable; their contracts are not.

The diagram deliberately shows boxes A and N on the same logical task. Parallel
candidates are represented by separate leased task IDs in one comparison group;
other plans may partition work or activate only one box. Box identity never implies
`builder`, `verifier`, or any other permanent role. Detailed topology and scaling
semantics live in [`execution-topology.md`](execution-topology.md).

## 3. Authority boundaries

### Orchestrator owns

- the run objective and acceptance criteria;
- decomposition into a dependency DAG;
- worker registration, capability matching, leases, retries, and budgets;
- all agent-to-agent routing;
- authorization for destructive or externally visible actions;
- the append-only evidence ledger;
- acceptance, rejection, and escalation of worker claims;
- promotion of a resolved case into an eval or durable policy.

### Workers own

- execution inside one leased task;
- bounded local reasoning and tool use;
- emitting exact command, tool, transcript, environment, diff, artifact, and result
  evidence;
- asking for missing input through the orchestrator;
- proposing follow-up tasks without silently creating authoritative work;
- reporting completion or a typed block.

### Humans/team policy own

- goals and business truth;
- credentials and high-impact approvals;
- whether a changed behavior is actually desirable;
- exceptions to budgets, safety gates, and promotion rules.

Provider accounts used during development are test connections owned by the
developer. Public Camol installations use user-owned provider or local-model
connections; no owner credential is part of the product.

## 4. Durable primitives, not skills

Most current “skills” are one of five things wearing the same costume:

| Current shape | Camol home |
|---|---|
| a command recipe | versioned task template |
| safety rules | enforceable policy |
| environment setup | worker adapter + capability probe |
| debugging method | debugger state machine |
| institutional memory | incident, eval, or decision record |

A small number of human-readable playbooks can remain as onboarding and emergency
guidance. They are documentation, not hidden runtime authority. If a rule matters to
correctness, the harness should be able to reject an event that violates it.

## 5. Core domain objects

### Run

A goal-scoped execution with acceptance criteria, budgets, policy version, and a
terminal verdict. A run is replayed from its events rather than mutated as an opaque
row. The human approves the exact runbook digest before execution, and completion is
the conjunction of declared empty-set conditions rather than an agent confidence
claim.

### Task

A bounded unit with inputs, capability requirements, dependencies, constraints, an
evidence contract, and acceptance criteria. A task can be leased to one worker at a
time. Its ordered steps name instructions, expected commands, and completion checks.
Follow-up tasks enter only through the orchestrator.

### Worker

A registered agent-capable runtime endpoint on an execution target. Identity and
readiness are separate from target existence. A VM, container, or local process being
“running” does not prove its agent, checkout, dependencies, or task channel are ready.

Suggested readiness vector:

```text
transport_ready -> runtime_ready -> agent_ready -> project_ready
```

Each transition requires positive evidence and expires after a defined interval.

### Evidence

An immutable observation linked to a task, debug case, experiment, or review. The
first evidence kinds are:

```text
command | tool_call | transcript | environment | artifact | diff | test_result | claim
```

Evidence may point to content-addressed blobs rather than placing secrets or very
large payloads directly in the ledger. Redaction happens before persistence and is
itself recorded.

### Routed message

An agent-to-agent envelope accepted and recorded by the orchestrator. Direct worker
channels are transport details, not authoritative communication. This gives the
orchestrator context, prevents circular delegation, and makes questions/replies
replayable.

### Debug case

A typed gap between observed behavior and target behavior. It owns a reproduction,
required evidence, hypotheses, experiments, verification, and eventual eval.

### Hill climb

A bounded comparison between a baseline vector and one candidate vector. Promotion
requires at least one material improvement, no forbidden regression, reproducible
evidence, and a regression case. Camol does not collapse unrelated measures into a
single score.

## 6. Event lifecycle

The SQLite event ledger is the write model. Everything else is a projection.

```text
RUN_CREATED -> PLAN_APPROVED -> RUN_STARTED
  -> TASK_LEASED -> TASK_STARTED
  -> AGENT_TURN_RECORDED + EVIDENCE_RECORDED*
  -> TASK_SUBMITTED -> TASK_VERIFICATION_RECORDED
  -> TASK_SUCCEEDED | TASK_RETRY_SCHEDULED | TASK_BLOCKED
  -> DEBUG_CASE_OPENED?
  -> DEBUG_CASE_VERIFIED?
  -> EVAL_PROMOTED?
  -> RUN_COMPLETED | RUN_BLOCKED
```

Every event carries `event_id`, `run_id`, `actor_id`, `occurred_at`, `type`, `payload`,
and optional `causation_id`/`correlation_id`. SQLite adds a monotonic per-run `seq`.
Replaying events must deterministically reconstruct scheduler and audit state.

## 7. Delegation and review

Delegation is a DAG, not a free-form swarm:

1. The orchestrator declares a task and its acceptance/evidence contract.
2. The scheduler selects a ready worker whose capabilities are a superset of the
   task's requirements.
3. The worker receives a lease containing only its bounded context.
4. Each turn returns completed step IDs, a compact checkpoint, token use, evidence,
   and a typed `continue`, `complete`, or `blocked` result bound to the packet hash.
5. Completion is a claim. For high-risk tasks an independent verifier adjudicates
   the claim against evidence.
6. The orchestrator accepts, retries, decomposes, or escalates.

Useful role separation is policy, not agent identity. The same model may build on one
task and verify a different task, but should not independently approve its own
high-risk claim.

### Bounded-context compounding

Token efficiency comes from controlling what crosses a turn boundary. A new turn gets
the frozen objective, current task, remaining steps, rules, dependency receipts,
latest checkpoint, latest verifier delta, and remaining budget. It does not inherit
the raw transcript by default. This makes a checkpoint a state transition rather than
a chat summary.

On a red verification, accepted steps remain accepted and only the failure delta is
fed into the next attempt. The scheduler can move the attempt to another eligible
agent. Its deterministic preference order is capability fit, verified task history,
fewer failed attempts, verified steps per 1,000 tokens, then total tokens. This is an
operational hill climb, while product-quality changes use the explicit vector
comparison described below.

## 8. Execution-target adapter contract

Each execution-target adapter implements the same lifecycle:

```text
discover -> probe -> provision? -> prepare -> launch -> lease -> stream -> stop
```

Minimum adapter operations:

- `list_workers()` returns stable provider and harness identities;
- `probe(worker)` emits structured readiness evidence;
- `prepare(worker, project_ref, source_ref)` creates an exact, resumable workspace;
- `launch(worker, task_lease)` starts the requested agent runtime;
- `send(worker, envelope)` delivers an orchestrator-routed message;
- `events(worker, cursor)` streams structured worker events;
- `stop(worker, reason)` ends the agent process without deleting its target;
- `destroy(worker)` is a separately authorized destructive operation.

The existing Enrollment Hub cmux/exe.dev flow is a good adapter seed, but pane text,
Termius entries, and provider-running flags must stay derived views rather than
truth.

## 9. Feedback loops

Camol uses three nested loops:

### Task loop

`delegate -> execute -> evidence -> verify -> accept/retry`

This closes ordinary work without turning every failure into a permanent doctrine.

### Debug ratchet

`observe -> target -> reproduce -> localize -> experiment -> verify -> eval`

This turns a real failure into a case that can never silently recur.

### Hill-climb loop

`baseline vector -> candidate -> trials -> guardrail comparison -> promote/reject`

Promotion creates a new versioned policy/config/code reference and pins later runs to
it. In-flight runs retain the version they began with.

## 10. Storage and security

- SQLite in WAL mode is the first durable write model; the event stream remains
  inspectable through the CLI and replayable after restart.
- Large artifacts move to a content-addressed store with hashes in events.
- Secrets are references, never evidence payloads.
- Transcript retention and redaction policy is explicit per run.
- Raw worker output is untrusted data.
- Every external mutation includes an idempotency key and authorization decision.
- Deletion of an execution target or evidence bundle is a distinct event and requires
  a retention policy or explicit human authorization.

## 11. Build order

1. **N-worker Python kernel (present):** frozen runbook, SQLite replay, task DAG,
   process adapters, compact checkpoints, exact leases, evidence/verification gates,
   token budgets, restart recovery, debugger ratchet, and vector hill-climb logic.
2. **Real local agent wrappers:** wrap Codex and/or Claude so they consume a turn
   packet and emit the result contract; prepare isolated Git worktrees for each box.
3. **Execution-target adapter:** wrap the existing Enrollment sandbox controller;
   prove target, worker, runtime, and workspace identity/readiness without relying on
   terminal scraping.
4. **Orchestrator planning session:** turn the collaborative plan conversation into a
   reviewed runbook draft and explicit plan amendments.
5. **Debugger vertical slice:** import one Sarah defect bundle and promote it into a
   runnable regression case.
6. **Eval runner:** deterministic first, recorded/model-judged behind explicit spend
   gates.
7. **Projection/UI:** fleet, DAG, evidence timeline, debug case, hill-climb vectors,
   and one next action.
8. **Durability hardening:** lease expiry/heartbeats, content-addressed artifacts,
   redaction, secrets references, idempotency contracts, and multi-user authorization.

The sample control-plane milestone passes deterministically with three workers in the
first wave, one dependent integration task, independent command verification, and a
restart replay. Three is a property of that fixture, not the kernel. The next
milestone repeats the proof with real agent processes, a variable worker count, and
isolated worktrees.
