# Responsive initial box preparation

Initial per-box workspace preparation and readiness probes now run in a tracked
local child process. Previously the async runner called `AdmissionController.prepare`
synchronously: a blocked version probe also blocked control requests and existing
lease watchdogs on that event loop. A real gated interpreter-version probe reproduced
that behavior without a model or provider call.

This is local control-plane preparation, **not a distributed worker executor**.
The child receives the frozen runbook, selected task/agent, exact source/state paths,
target identity, baseline, evaluator digest and observation time. It validates the
plan and selected configuration before filesystem work. It never opens the kernel
event store, approves a plan, reserves shared capacity, leases work or invokes an
agent turn. Existing guarded probes retain their restrictions on executing project
code. Worktree/Git metadata creation remains a normal preparation effect.

The controller accepts only the expected response schema and subject, reloads the
owned workspace receipt, and rechecks current run/plan/owner/task/worker state before
publishing success or failure. Shared capacity and source admission remain kernel-side.
Pause/drain is checked after preparation and again before leasing. An owner stop or
terminal transition cannot publish a late admission. Unrelated integration progress
does not automatically invalidate an independent candidate's captured base; normal
dependency, source, evaluator and integration checks still apply.

## Process lifetime and evidence

Preparation uses the current Python and explicitly selected Camol package with
isolated Python startup; repository `PYTHONPATH`/site customization is not its code
loader. JSON input/output has a 20-MiB ceiling, allowing the existing 8-MiB runbook
plus selected task/agent fields. The process has a 60-second operational deadline.
Neither this deadline nor process success extends readiness receipt freshness.

The helper runs as the trusted controller OS user, with access needed for source
Git metadata and control-state preparation. Its process record says
`developer_trusted`; it is **not** a sandboxed agent or new model authority grant.
The worker's separately frozen sandbox policy remains unchanged. Do not expose
this private local preparation entry point as an unauthenticated remote endpoint.

Records live under `packets/RUN/admission-preparation/*.invocation.json`, using the
existing process identity/lifecycle schema. They contain argv/policy digests and
start/end state, not serialized runbook input or raw stderr. The controller's normal
startup orphan scan includes them. Staged-input process cleanup bounds cancellation
and pipe settlement. Cancellation waits for that cleanup before returning and cannot
publish a later result. A 10,000-record per-run ceiling requires explicit archival;
automatic retention is not added here.

Cancelled or failed preparation can leave its owned worktree and preparation files.
These are not rollback, readiness, reservation or lease receipts. Existing idempotent
workspace checks determine whether later preparation may reuse them. This change does
not authorize deletion of partial work or imply salvage has completed.

## Coverage and remaining work

Tests use a real gated version-probe process, an embedded owner callback, and the
authenticated supervisor socket. They check responsive status/drain, no lease after
a mid-probe pause, tracked-child cancellation, no delayed event publication, terminal
state changes, and rejection of changed plan/task/agent input before filesystem work.
Normal N-box build, recovery, shared-capacity and supervisor regression groups cover
the changed driver path. The private synchronous `_ensure_admissions` helper remains
available for existing direct embeddings/tests; the async driver does not use it.

Other controller-side filesystem/Git operations and active-lease reproof paths have
their own latency/cancellation behavior. This is not a claim that every operation is
nonblocking or that arbitrary OS-level I/O stalls are contained. Target-side remote
admission, source/artifact transfer, dispatch, authenticated result reduction and
distributed cancellation remain separate requirements.
