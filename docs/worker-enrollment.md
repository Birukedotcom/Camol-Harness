# Owner-reviewed worker stream enrollment

This implements enrollment for an **evidence stream on an already-admitted lease**.
It does not discover or adopt a machine, authenticate an SSH host, prepare a remote
checkout, launch a remote worker, or prove readiness. It connects the
[worker delivery primitive](worker-delivery.md) to the authoritative kernel's owner
review, revocation and lease-mutation serialization.

## Owner flow

`Harness.worker_streams` exposes a terminal-independent `WorkerEnrollment` service.
The service also accepts an owner-managed `Orchestrator`, exact run ID and private
state directory containing that kernel database. Keep that directory outside all
model/workspace write grants; `developer_trusted` execution does not enforce that
separation. Call this synchronous API on its owning controller thread.

1. Construct the V1 stream binding described in `worker-delivery.md`, using the
   actual active lease fence and admitted runtime. Control-plane, generation and
   stream IDs are explicit owner-selected identities, not discovered attestations.
2. `prepare(stream, by=owner)` checks current lease identity and generates a fresh
   random 32-byte key in private control storage. It returns only a public proposal
   containing the exact stream, permission, key commitment and review digest.
   Preparation alone does not enroll the stream or allow receipt ingestion.
3. `approve(proposal, by=owner, review_digest=proposal['digest'])` rechecks the exact
   material and active lease, then records `WORKER_STREAM_ENROLLED` with optimistic
   kernel append. The approved human must match the frozen run owner. Workers cannot
   approve, and a changed plan, fence, runtime, proposal or key fails closed.
4. After a separate secure enrollment/transport setup, feed a signed producer batch
   to `receive(scope, raw_batch)`. The returned acknowledgment still means only
   durable storage of untrusted worker evidence, not kernel promotion or success.
5. `revoke(scope, by=owner, reason=...)` records `WORKER_STREAM_REVOKED`. It prevents
   further receipt retrieval through this service, including duplicate batches.

Only one stream may be active for a lease. Revoke it before approving a replacement
generation/key. An exact approval retry cannot rewrite or resurrect a revoked
stream. The ledger's `active` enrollment status is an allowlist state, **not** a live
connection, fresh lease or task-readiness indicator. Every receive checks the current
fence, effective expiry and kernel state again. Owner inspection remains possible
after revocation and is associated with the box's retained event view.

The native CLI provides `camol worker-enrollment prepare|approve|revoke|inspect`;
each subcommand's `--help` gives its required paths and review parameters. Mutations
take `--workspace`, `--state-dir`, `--run-id`, `--by` and the same leader lock as
other synchronous owner commands. To address an already-owned daemon, explicitly
use `--live --plan-digest DIGEST` or the controller-side embedding. Inspection uses the bounded,
noncreating exact-run reader and needs no raw key. This command does not add a
public network endpoint; the optional [worker gateway](worker-gateway.md) has its
own exact owner-reviewed listener policy.

For local owner embeddings, `producer(scope, root, by=owner)` creates the matching
private producer spool. It does not transmit a key, change account login or bind a
remote machine. Protect the object and its key from model process access.

`import_received(scope, by=owner, request_id=..., limit=6)` explicitly captures a
received page into the run ledger with a durable retry receipt. `records(scope)`
reads the retained captures without a key. The CLI also exposes `import` and
`records`; see [capture semantics and bounds](worker-import.md). This does not
accept worker claims as executed tools, provider bills or completed tasks.

## Race and failure behavior

Incoming batches are authenticated before requesting the kernel write lock, then
authenticated again at ingestion. The receiver holds a SQLite write reservation on
the kernel database while rechecking enrollment and lease state and committing its
own spool. Competing kernel writers, including another connection/process trying to
reassign the lease, cannot pass that critical section. Lock acquisition is bounded
to one second; the original database busy timeout is restored on every exit.
An existing caller-owned transaction is rejected rather than borrowed or rolled
back. Cancellation releases the lock and leaves committed receipts recoverable.

The kernel and spool are **not one atomic database transaction**. No kernel result
event is appended by receiving a batch. A signed success, heartbeat or token count
therefore cannot complete a task, renew its lease, discharge a gate, count as a
provider bill or become verified progress. Serialized kernel promotion and artifact
validation are still required next. Acknowledged backlog is not a teardown receipt.

Key and proposal files are created exclusively and synchronized before returning
the proposal. Interrupted partial setup is retained as unavailable; Camol never
silently replaces the key under the same stream identity. If event append loses its
optimistic race, prepared receiver files confer no authority. There are ceilings of
1,000 prepared directories per state directory and 1,000 enrollment events per run;
partial preparation counts toward the directory ceiling. Automatic key cleanup,
rotation, archival and operational recovery are not implemented.

Known enrollment-key encodings are redacted from revocation notes. If key material
is missing, changed or unsafe, revocation still works but its free-text note is
withheld because key-specific redaction cannot be checked. Private keys are never
included in ordinary ledger export; exported enrollment/revocation events replay
their public commitments and statuses without consulting keys or today's credential
environment. Ordinary export does **not** include these private spools or restore
their operational enrollment. A recovered public record is not authorization to
resume a worker without the required secure material and fresh lease checks.

## Remaining distributed-execution work

An opt-in [TLS evidence transport](worker-tls.md) now connects the service to
bounded encrypted sockets and an explicit one-batch CLI flush. It does not
automatically configure a listener, distribute keys or launch workers.

Machine discovery/adoption, production transport lifecycle, target-side
readiness and launch, source/artifact transfer, native event pumping, trusted usage
measurement, promotion cursors, remote cancellation, salvage and lifecycle recovery
remain open. Tests here exercise real local kernel leases, competing SQLite writers,
private material, packet authentication, replay/export and owner CLI/API paths—not
a hosted worker, model account or actual cross-host build.
