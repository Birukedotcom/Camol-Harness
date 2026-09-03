# Capacity, agent selection, and public provider model

Status: specified, not implemented beyond static process-worker registration.

Camol is a harness. It is not a model, worker agent, or vendor account. It hosts a
deterministic control plane and can invoke a replaceable orchestration agent to turn a
human-approved goal into task and placement proposals. The kernel—not that agent—owns
leases, resource ceilings, event truth, and state transitions.

## 1. Separate the product from the agents

```text
Camol harness
  deterministic kernel + scheduler + ledger + UI
  orchestration-agent adapter (replaceable)
  worker-agent adapters (replaceable and heterogeneous)
  execution-target and provider adapters
```

The orchestration agent may be Claude, Codex, a local model, another agent runtime, or
a policy-selected combination. Worker agents may differ within the same run. Camol
matches work to them; it does not impersonate them or assume their vendor identity.

The orchestrator proposes:

- task decomposition and dependency shape;
- required capabilities and context for each task;
- candidate replication or independent verification;
- an agent, model, runtime, workspace, and target placement; and
- scale-out, scale-in, reassignment, or escalation.

The kernel checks each proposal against the frozen plan, current resource evidence,
lease invariants, and the approved resource envelope before it can take effect.

## 2. Connected capacity inventory

Camol builds a live supply graph from what the user already has connected:

```text
provider connection -> models, rate limits, account scope
execution target -> CPU, memory, GPU, disk, region, trust tier
worker runtime -> agent CLI/API, tools, context and concurrency capacity
workspace -> repository, revision, branch, dirty state, services
credential reference -> allowed capability, target, expiry
local model endpoint -> loaded/downloadable models, VRAM and queue state
```

Each observation has a source, timestamp, expiry, and confidence status. Configured,
installed, authenticated, reachable, loaded, and task-ready are different states.
Capacity currently reserved by another Camol run or outside workload is subtracted
before scheduling.

The plan freezes required capabilities and a resource policy, not a guessed machine
count. Capacity is reprobed at lease time because provider quota, GPU memory, network,
credentials, and target health can change after planning.

## 3. Maximum useful utilization

The recommended default allocation mode is `saturate_connected`: use the maximum
currently connected, eligible capacity that can advance ready plan work without
crossing an approved limit.

```text
desired active boxes = minimum of
  useful ready task/candidate parallelism
  eligible unreserved worker capacity
  provider and runtime concurrency
  cost/token/time envelope
  integration and workspace pressure limits
```

This is not “launch everything.” A box is useful only when it has a ready task,
required capability, isolated workspace, valid authority, and a completion/evidence
contract. Additional boxes for the same logical task require an explicit candidate,
quorum, or verification policy.

Existing connected capacity is preferred. When no eligible capacity remains, Camol
may queue, reassign, load a local model, or propose provisioning. Provisioning is
automatic only inside an already approved provider, target class, region, count,
spend, lifetime, and teardown envelope.

A capacity decision emits:

```text
capacity snapshot and freshness
ready work and critical-path position
eligible and rejected worker candidates with reasons
selected N and marginal reason for each additional lease
resource reservations and remaining envelope
expected integration/duplication pressure
```

## 4. Agent and model suitability

Selection is capability- and evidence-based rather than vendor-hardcoded:

```text
task requirements
  -> eligible agent/runtime/model profiles
  -> target and workspace placement
  -> policy/cost/latency filter
  -> historical eval and reliability ranking
  -> lease
```

Relevant dimensions include language and framework support, repository scale, tool
use, multimodal inputs, context capacity, local/hosted placement, latency, cost,
privacy, regional constraints, historical task-family performance, retry/recovery
behavior, and independent-verification requirements.

Recorded benchmark performance is supporting evidence, not permanent reputation. It
is scoped to model version, agent version, harness configuration, tools, budget,
environment, and task family. Provider marketing names or a single aggregate score
cannot satisfy capability readiness.

The orchestration agent is selected through the same mechanism, but its task family
is planning, decomposition, reconciliation, and escalation. A worker that built an
artifact cannot become its consequential final approver merely because it is the
highest-scoring available model.

## 5. Account and credential model for a public tool

Development may use the owner's accounts to validate adapters, but those accounts are
test inputs and never become Camol infrastructure. Public distribution uses
bring-your-own connection and supports:

- provider API keys stored as OS-keychain or secret-manager references;
- supported OAuth or device authorization;
- existing CLI authentication when its provider permits delegated use;
- cloud workload identity and service accounts;
- local model endpoints with no external account; and
- organization-managed connection brokers for team installations.

Adapter development should use a sandbox or least-privilege test account with explicit
spend and environment limits where the provider supports it. Raw passwords, API keys,
refresh tokens, and session cookies are never pasted into a chat, plan, runbook, test
fixture, or repository. Authentication occurs through the provider's supported local
flow and stores only a secret reference in Camol.

Camol never commits credentials, copies them into prompts, or stores them in ordinary
event payloads. A box receives a short-lived capability to resolve only the secret and
scope required for its lease. Connection inspection renders provider, readiness,
scope fingerprint, quota class, and expiry—not the secret value or unnecessary
account identity.

Users can disconnect a provider, revoke a grant, export their non-secret run records,
and delete retained data according to policy. Local-only operation does not require a
Camol-hosted account. If a hosted Camol service is added later, tenant isolation,
regional storage, encryption, abuse controls, billing, support access, and deletion
become separate product obligations.

Provider connectors must use supported interfaces and respect their current terms,
rate limits, and authentication boundaries. Consumer-session scraping is not a public
credential strategy.

## 6. General software workflows

No repository, cloud, language, or voice stack defines Camol. Product behavior comes
from generic objects:

```text
goal -> plan -> task DAG -> capability requirements -> leases
  -> artifacts/effects -> invariant gates -> evaluation -> completion
```

Specific workflows are versioned profiles containing adapters, probes, task
templates, invariants, evaluator families, and evidence schemas. Initial reference
profiles should cover:

1. local repository diagnosis and repair;
2. multi-package or multi-service feature construction and integration;
3. remote build/deployment plus failure reconciliation;
4. long-running observation and debugging; and
5. local-model or accelerator-backed work.

The Buckeye/GCP/voice material is one demanding reference profile and migration case.
It must not make GCP, voice agents, cmux, or the owner's organization universal
dependencies. Maturity is reported per core and per adapter/profile so one backed
workflow cannot make every connector appear proven.

## 7. Public-readiness obligations

Before public use, Camol needs:

- a stable, versioned adapter and event protocol;
- signed releases, reproducible packaging, upgrade and state-migration tests;
- macOS/Linux support and explicit Windows/WSL posture;
- credential threat modeling and independent security review;
- adapter permission manifests and supply-chain verification;
- multi-run resource reservations, fairness, quota, and cancellation semantics;
- opt-in telemetry with a fully functional no-telemetry/local-only mode;
- export, retention, redaction, revocation, and deletion controls;
- provider-specific compatibility tests without embedding owner credentials;
- recovery tests across process, host, transport, provider, and storage failures; and
- documentation that labels experimental, backed, and unsupported adapters honestly.

## 8. Recommended defaults

- `saturate_connected` uses already-connected eligible capacity for real ready work.
- New paid provisioning is off until a human approves an exact reusable envelope.
- Agent/model choice is automatic inside the plan's capability and policy constraints,
  but its evidence and reason are always visible.
- Local secrets remain local; public Camol has no universal provider credential.
- The harness, orchestration agent, worker agent, model, target, and provider account
  always retain separate identities.
- Core maturity and each workflow/adapter maturity are reported independently.
