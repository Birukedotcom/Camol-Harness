# Importing received worker reports into run history

Authenticated delivery and visible run history are distinct steps. An enrolled
receiver can acknowledge a private spool commit without making any kernel event.
`WorkerEnrollment.import_received()` now captures a bounded page of those reports
into the owner run ledger, together with its cursor and an idempotent request
receipt. This is **unverified report capture**, not task-result acceptance.

## Owner flow

The existing terminal-independent `Harness.worker_streams` service exposes:

```python
receipt = harness.worker_streams.import_received(
    scope,
    by=approved_owner,
    request_id="capture-page-1",
    limit=6,
)
page = harness.worker_streams.records(scope, after=0, limit=100)
```

The CLI equivalents are `camol worker-enrollment import` and
`camol worker-enrollment records`. Their `--help` lists required paths and IDs.
Import requires the exact run, workspace, state directory, approved owner, scope
and a caller-chosen `--request-id`. It takes the ordinary owner lock through
`Harness`; for a live daemon, use its owning controller-thread embedding instead
of starting a second owner. The import operation opens no network connection,
launches no process and does not implicitly flush a producer spool.

Each new request imports the next 0–6 received records from the kernel's existing
per-stream cursor. Use a **new request ID for the next page**. Retrying an existing
ID with the same scope and limit returns its original receipt, even if more records
have since arrived. Changing those arguments under the same ID is denied. An empty
page is also frozen, not a request that starts importing future messages on retry.

`records` reads the exact run ledger only; it needs no enrollment key or live
worker. It supports 1–100 records per page and an exclusive `--after` sequence.
Imported events are associated with their original fenced box in `camol box read`
and the ordinary `/box` event view. They are included in replay and ordinary run
export, unlike the underlying private enrollment key and spool.

## What capture does and does not prove

New imports require an active enrollment and the exact current, fresh lease.
They open the private receiver using its reviewed key commitment, validate stored
record digests and an uninterrupted source sequence, and capture credential-redacted
data. Worker timestamps are labeled reported times, not controller observations.
The controller capture time is recorded separately in the kernel receipt.

Each captured row retains source sequence, predecessor, source-record and source-data
digests, kind, reported time, redacted data and a capture digest. Source digests are
commitments to the receiver's original bytes; redaction can make the captured data
differ from those bytes. Replay validates the capture's internal shape, digest,
cursor, owner and lease binding; it does not reconstruct redacted source bytes or
authenticate a remote machine. The private receiver and kernel are owner-controlled
storage, not a sandbox against the owner UID.

Reports of tools, artifacts, usage, heartbeats and completed turns remain in the
separate `worker_imports` namespace. They do **not** update task steps, turn counts,
provider bills, heartbeat renewal, gates or execution grants. Artifact-shaped
objects inside this typed report envelope are not traversed as controller-owned
blob references by box preview or export. Worker-authored schema labels cannot
cause artifact fetches. Actual artifacts and provider receipts require their own
independent validation paths.

Capture redaction is applied before the kernel append and is not rerun when
replaying historical events. Public `records` snapshots additionally apply current
credential redaction and expose a snapshot digest plus an explicit view-transform
label. A row's capture digest names its original capture, not transformed display
bytes. Protected request identities are rejected before publication.

## Atomicity, failure and bounds

`WORKER_STREAM_IMPORTED` records the page, request receipt and cursor in **one
kernel event**. Its append compares against the entire run sequence; a concurrent
revocation, reassignment or other run append causes a conflict rather than importing
against stale state. There is no separate cursor sidecar to advance before the
event commits. A lost response after commit is recovered by the same request ID.
An append failure leaves the source spool and kernel cursor unchanged.

Already-recorded receipts can be read after expiry, revocation or key loss; that
does not authorize new capture. New imports after expiry/revocation are denied.
Received but unimported records remain retained for explicit owner investigation;
this interface does not discard or automatically salvage them.

Per-run capture is bounded to 10,000 records, 10,000 requests and 8 MiB of captured
row bytes. Each row is at most 36 KiB. Exhaustion, source gaps and source rollback
fail explicitly with no partial page append. Import does not delete acknowledged
producer rows or receiver history. Operational retention/compaction and a trusted
result reducer remain separate work.

## Remaining execution integration

An [owner-approved supervisor gateway](worker-gateway.md) now optionally pumps
allowlisted streams; it remains off by default. Target-side readiness and
launch, validated source/artifact transfer, provider-originated usage receipts,
worker turn protocol binding, cancellation/salvage and evaluator-driven task
completion remain required for distributed execution. Neither an encrypted packet
nor an imported worker success claim substitutes for those checks.
