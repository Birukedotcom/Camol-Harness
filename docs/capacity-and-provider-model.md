# Capacity, agent selection, and public provider model

Status: schema-6 shared admission and local execution are implemented and tested.
Dynamic provider/target discovery, authenticated remote workers, auto-provisioning,
and physical provider-request accounting through CLI internals remain unbacked.

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

## 9. First hosted-model proof profile

The first real hosted-model proof is a generic Claude CLI box requesting Claude
Fable 5.1. This is an adapter profile, not a dependency of the kernel and not a claim
that Fable runs locally. The box owns the isolated workspace, sandbox, tools, and
evidence collector; inference uses the user's supported remote provider connection.

Readiness requires a compatible CLI, supported authentication, model entitlement,
quota and spend capacity, task capability fit, sandbox policy, isolated workspace,
and a runnable frozen evaluator. The lease and model-call envelope record both the
requested and resolved model so a refusal, provider fallback, or safeguard reroute
cannot silently inherit Fable-specific capability claims.

As of 2026-09-03, Anthropic documents model ID `claude-fable-5-1` and requires Claude
Code 2.1.255 or later for Fable 5.1. Those values belong in versioned adapter
compatibility data and must be reprobed rather than hard-coded into scheduler logic.
The complete implementation order and dogfood acceptance gate live in
[`v0-build-plan.md`](v0-build-plan.md).

## 10. Executable shared broker (schema 6)

V1–V5 retain their original digests and per-run capacity semantics. V6 opts into the
shared SQLite admission broker and retains V5 invariant gates/final human acceptance.
The plan freezes an exact `run.capacity_policy`:

```json
{
  "namespace": "team-local",
  "allocation": "saturate_connected",
  "reservation_ttl_seconds": 90,
  "allow_owner_declared_supply": false,
  "rate_scope": "harness_turn_estimate"
}
```

Each agent declares separate `capacity_pools` for `target`, `runtime`, and optional
`provider`. Each task declares nonnegative `resource_requirements` for `cpu_millis`,
`memory_bytes`, `gpu_millis`, `vram_bytes`, and `disk_bytes`, plus a `placement` map
using OS, architecture, region, locality, or trust-tier constraints. A leased task
also reserves one target slot and one runtime/provider session. Shared physical
resources must use the same namespace and pool identity across runs; creating a
second alias is not another machine or additional quota.

Pools are versioned, expiring supply observations with capabilities, placement,
limits, outside workload use, and explicit `observed` or `owner_declared` provenance.
Only ready, fresh, policy-permitted supply is usable. Owner-declared supply requires
an explicit allowance in the approved plan; it is never relabeled observed. Supply
changes are preserved in the broker's audit table. A CLI login does not publish
provider quota. The broker does not provision machines, load/download models, or
inspect credentials.

The default database is `capacity.sqlite3` under the per-user Camol state root;
multiple project state directories share it. Embedded/test callers can inject an
explicit `CapacityBroker` path. Tests use only temporary broker databases. Database
files are owner-only. `read_only=True` inspection requires an existing database and
does not create directories or change database configuration.

Reservation is atomic across all requested pools, subtracting both observed outside
use and all active/suspect Camol reservations. Queued admission is round-robin among
runs and FIFO within a run. A resource-starved older request blocks conflicting
younger work but not work using independent pools; an impossible or unready request
does not globally stall other pools. Capacity is never fabricated to meet a desired
box count. Every selected reservation and waiting reason is recorded in the run
ledger; lease replay rejects missing, changed, or expired global receipts.

Expiry does not make a possibly running process disappear: an expired reservation
becomes `suspect` and continues to hold resources. The runtime stops/reconciles
processes before release. A captured candidate awaiting human review may retain its
slot; verification-only recovery can restore that held reservation after proving
the worker process stopped and obtaining fresh supply. It does not authorize a new
worker call. Source plan revisions require shared reservations to be settled too.

Provider pools additionally declare a rolling window with `max_requests`,
`max_tokens`, `window_seconds`, and `scope`. Debits occur before an uncached adapter
invocation, are idempotent by invocation identity, and survive cancellation or
unknown results until window expiry. Current Claude/Codex wrappers use
`harness_turn_estimate`: one charge represents a Camol turn/CLI session, which may
contain multiple internal provider requests. This is scheduling pressure, **not a
hard physical RPM/TPM guarantee**. Requiring `provider_request` fails closed for these
adapters until a request-observing execution/proxy adapter exists. Likewise CPU,
memory, and GPU values are admission reservations, not a claim of OS/cgroup resource
enforcement. Strong resource isolation requires an appropriate execution backend.

A financial budget denial before an invocation intent can defer a reserved rate
call. `CAPACITY_CALL_DEFERRED` binds its exact running lease/turn, live budget
wait and pre-intent binding digest to a new deterministic call ID. Every retry
still passes supply and rolling-window checks; the old debit is never deleted or
refunded. It therefore cannot reuse an expired receipt, silently reset accounting,
or treat an unknown/launched invocation as unlaunched. Even an unexpired deferred
debit stays charged until expiry, so a replacement can temporarily wait for rate
capacity. Broker-committed replacement receipts are recovered by the same ID if
local event publication was interrupted. Missing durable deferral evidence is
not reconstructed from the absence of a PID. See [usage accounting](usage-accounting.md)
for trust and restart boundaries.

The broker provides finite, shared accounting and suitability/fairness checks. It
does not yet supply automatic historical-performance ranking, dynamic model loading,
cloud provisioning, or an authenticated cross-host broker transport. These remain
separate adapter and operational proof obligations.

## Execution-backed local placement

Capacity supply attributes describe a pool; they are not proof that a worker
actually executes there. The current `HarnessRunner` is local. A V6 task with
nonempty `resource_requirements.placement` now requires an additional target-bound
`execution.placement` probe before admission and any shared reservation or lease.
Its definition pins the exact required fields and local sandbox policy tier.

- `os` and `architecture` use exact lowercased `platform.system()` and
  `platform.machine()` values from the running harness (for example `darwin` and
  `arm64`). There is no alias normalization, binary-architecture inspection,
  container discovery, hardware attestation or emulation guarantee.
- `locality` is `local`, meaning the host running this harness, including when
  Camol itself runs inside a VM. A terminal pane or a pool named remote does not
  change the executor.
- `trust_tier` comes from the frozen sandbox policy; admission independently
  requires the existing sandbox boundary probe. A developer-trusted tier is not
  enforcing isolation. The read-only doctor does not prepare or prove that boundary
  and therefore reports its placement trust tier as unproven.
- `region` is unproven. No environment variable, connection name or owner-declared
  pool label is promoted to geographic execution evidence.

Mismatching or unproven required fields produce `POLICY_DENIED`, with observed
values and a repair/review hint. The runner also validates the exact placement
probe and re-observes local attributes on assignment/resume and before each new
worker invocation, before rate debit. A changed observation pauses the assignment
without starting a worker turn. Empty placement retains earlier behavior.

Existing ledgers remain readable/replayable; an old admission without this proof
cannot authorize a new placement-constrained launch. These checks are a local
admission prerequisite, not a distributed execution protocol or a continuous
hardware monitor. The host and embedding Python process remain trusted.

### Remote execution still requires a separate lifecycle

The existing SSH control client attaches to an already running remote supervisor;
it does not make this runner's workspace, sandbox, evaluator or adapter remote.
Before distributed workers can be called implemented, their adapter must bind
target identity and execution observations to admission, prepare isolated source
and immutable evaluator inputs there, execute under fenced authority, and return
authenticated event/artifact sequences. Reconnect must reconcile the exact
invocation rather than duplicate it. Cancellation, checkpoint/salvage and teardown
must prove what stopped and what was retained before releasing capacity or deleting
anything. Adoption and owner-approved provisioning are distinct operations.

Those are still open implementation and fault-injection gates. This placement
guard deliberately does not invent that authority from capacity labels, pane
selection, an SSH connection, or the trusted `adapter_factory` embedding hook.
