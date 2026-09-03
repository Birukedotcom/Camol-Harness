# Execution topology and N-box scheduling

Status: specified; the local registered-worker subset is executable.

Camol delegates to as many boxes as a plan can justify within its approved resource
envelope. There is no product-level box-count constant. Every actual run still has a
finite concurrency, cost, authority, and infrastructure ceiling.

## 1. Do not confuse the terminal with the machine

The terminal is a user interface or an optional process session. It is not the
connectivity, compute, isolation, or authority boundary.

```text
human
  -> CLI/TUI client
  -> Camol control-plane daemon
       -> state/event and artifact stores
       -> scheduler and reconciler
       -> execution-target adapters
            -> workers -> boxes -> task leases
```

Camol may be installed and launched from a terminal inside a VM. In that topology,
the control-plane daemon and some workers can communicate locally. Closing the
terminal detaches the UI; it must not terminate the daemon or abandon the run.

The same protocol also supports a laptop controlling remote machines, a remote Camol
daemon with a thin local TUI, or a control plane and worker fleet inside a cluster.

## 2. Stable resource vocabulary

| Resource | Meaning |
|---|---|
| Control plane | The authoritative daemon containing the deterministic kernel, scheduler, reconciler, and approved orchestrator interface. |
| Client | A CLI, TUI, or automation process attached to the control plane. A client can disconnect without changing run truth. |
| Execution target | A compute boundary on which work may run: the same host, a VM, container, pod, bare-metal machine, remote development environment, or provider job. |
| Transport | How Camol reaches a target or worker: local process/IPC, authenticated worker RPC, SSH, cmux compatibility bridge, container exec, or a provider API. |
| Worker | A registered agent-capable runtime endpoint advertising observed capabilities and capacity. One target may host several workers. |
| Runtime | The agent CLI/API wrapper, model endpoint, language toolchains, system packages, and process environment used for a turn. |
| Workspace | The exact checkout, worktree, mount, branch, dirty-state digest, caches, and artifact paths used by work. |
| Box | Camol's inspectable execution unit binding a worker, runtime, workspace, capability grants, credential scope, and current state. |
| Session | An optional PTY, terminal stream, or tool-RPC conversation attached to a box. A box may exist without a terminal session. |
| Lease | Fenced, time-bounded authority for one box to work one task attempt. |

These identities are recorded separately:

```text
control_plane_id, client_id, target_id, transport_id
worker_id, runtime_id, workspace_id, box_id, session_id, lease_id
```

A successful SSH connection proves only transport reachability. A running VM proves
only target state. An open terminal proves only a session. None proves that the agent,
checkout, credentials, dependencies, or assigned task are ready.

## 3. Deployment shapes

### All local to one target

The TUI attaches to a supervised local daemon. The daemon launches isolated
workspaces and agent processes on the same machine. This also describes Camol running
entirely inside one VM.

### Remote control plane

The daemon runs on a durable target near the repositories, models, or cloud network.
Clients attach over an authenticated tunnel or API. Local terminal loss does not
interrupt work.

### Distributed workers

The daemon schedules across registered targets. An authenticated worker service is
the preferred steady-state channel; SSH or a provider API may bootstrap and recover
it. Outbound worker registration is supported for targets behind NAT. cmux is a
compatibility adapter, not a kernel dependency.

### Mixed local and hosted models

Model placement is independent of box placement. A local box may call a hosted model;
a remote GPU worker may host a local model endpoint; several boxes may share one
endpoint subject to its queue and memory limits.

## 4. Choosing N

N is an orchestration decision constrained by deterministic admission control. The
human approves a resource envelope rather than a universal box count:

```text
maximum concurrent leases and provisioned targets
total and per-task token/provider spend
wall-time and compute ceilings
provider request/token rate limits
CPU, memory, GPU/VRAM, disk, and image-cache budgets
repository write and integration concurrency
network, credential, region, and data-residency policy
```

The orchestrator proposes tasks and topology. The kernel may activate another box
only when:

1. a plan-backed task or candidate attempt is ready;
2. an eligible worker or permitted provisioning path exists;
3. isolation, authority, and dependency probes pass;
4. the approved resource envelope has capacity; and
5. expected marginal value exceeds the plan's scale-out threshold.

Useful topologies include partitioned subtasks, parallel candidates, map/reduce,
pipelines, independent verification, specialist escalation, and durable observation.
Idle replicas are not useful parallelism. The scheduler records why it selected N and
why each additional lease was expected to help.

Scale-in is also a state transition. A box must stop accepting work, checkpoint,
salvage required changes and evidence, reconcile external effects, release its lease,
and only then become dormant or eligible for teardown.

## 5. Box and lease lifecycle

```text
DISCOVERED -> PROBING -> READY -> LEASED -> RUNNING
  -> WAITING | CHECKPOINTING | VERIFYING
  -> DRAINING -> READY | DORMANT -> TERMINATED

any live state -> SUSPECT -> RECOVERED | LOST | QUARANTINED
```

Lease fencing prevents a delayed or partitioned worker from writing after a task was
reassigned. Heartbeats are health evidence, not task progress. A replacement worker
receives the latest durable checkpoint and accepted receipts, never an invented
continuation of missing state.

Arbitrary external effects cannot be exactly-once. Deployments, messages, cloud
mutations, and other effects require idempotency identities plus provider-side reads
and reconciliation before retry.

## 6. Scaling concerns that must remain explicit

### Orchestrator attention

The model-facing orchestrator cannot ingest N full transcripts. The kernel consumes
heartbeats and routine events; boxes emit bounded checkpoints and typed deltas; an
attention queue surfaces questions, contradictions, failures, approvals, and
integration pressure. Coordinator agents may summarize a task group, but they do not
become independent authorities.

### Backpressure and event order

Worker event streams have bounded buffers, cursors, acknowledgements, and retry
identities. Ordering is defined per run/causal chain rather than assumed from wall
clock time. Slow storage or evaluation throttles new leases before evidence is lost.
SQLite remains suitable for the small, single-writer local slice; the event-store
interface must permit a transactional service backend when measured contention,
durability, or multi-host availability requires it.

### Workspace and integration pressure

Every write-capable lease receives an isolated worktree or equivalent workspace.
More boxes increase merge conflicts and duplicated investigation, so scale-out uses
the repository graph and file/ownership forecasts. The orchestrator owns an
integration queue; synthesized artifacts are evaluated again.

### Heterogeneous capacity

Scheduling matches operating system, architecture, toolchain, model, accelerator,
region, data policy, credentials, and observed dependency readiness. Local-model
workers additionally advertise VRAM residency, context capacity, batching limits,
and queue depth.

### Security and tenancy

Workers authenticate to the control plane and receive short-lived, lease-scoped
capabilities. Target ownership, trust tier, repository access, credential references,
network egress, and data classification are placement constraints. A worker cannot
self-register into trusted work merely because it can reach the daemon.

### Cost and cleanup

Provisioning, model use, idle compute, storage, network egress, and retained artifacts
share one budget ledger. Orphan detection, drain deadlines, salvage receipts, and
teardown policy apply per target and box. Adopted infrastructure is never destroyed
under the policy for Camol-created disposable infrastructure.

## 7. N-box terminal navigation

A fixed row containing every box does not scale. The bottom switcher shows the
orchestrator, fleet totals, attention count, and a bounded recent/pinned window:

```text
[0 ORCH] [BOXES 12/47] [! 2]  [1 api-build] [2 voice-eval] [3 deploy] [MORE...]
```

`Alt+0` selects the orchestrator. `Alt+1` through `Alt+9` select the currently visible
shortcuts, not permanent box identities. `/box <stable-id>` opens any box; `/boxes`
supports state, task, target, model, and attention filters. Cycling operates over the
current filtered set. Connection, attention, task state, and selection remain
separate visual signals.

## 8. Recommended initial decisions

- Keep one authoritative supervised daemon; make CLI/TUI processes detachable clients.
- Use local process execution first and an authenticated worker RPC as the preferred
  remote protocol; retain SSH and cmux as bootstrap/compatibility transports.
- Replace fixed agent count with a positive `max_concurrency` resource-envelope field.
- Let the current executable use a finite pre-registered worker list, then add dynamic
  discovery/provisioning without changing lease semantics.
- Scale out only for ready task graph width, explicit candidate replication,
  verification, or observation—not to satisfy a target fleet size.
- Virtualize fleet navigation and summarize routine events before they reach the
  orchestrator model context.
- Keep the event-store API backend-neutral; move beyond SQLite only after measured
  concurrency or availability requirements justify it.

Remaining plan inputs are provider-specific ceilings, the first authenticated worker
protocol, default scale-out utility/cost thresholds, and the fleet size at which the
initial SQLite writer must be load-tested rather than assumed sufficient.
