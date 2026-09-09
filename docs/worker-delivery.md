# Durable worker evidence delivery

This is an execution-protocol prerequisite, not a distributed executor. It provides
an embeddable, authenticated producer outbox and receiver inbox for a single exact
worker lease. The scheduler still executes locally. The existing SSH bridge still
controls a supervisor, not a worker service.

## Identity and trust

An owner creates a `camol.worker_stream` V1 binding containing `control_plane_id`,
`worker_generation`, `stream_id`, `runtime_id` and the complete existing
`camol.lease_fence`. The fence binds run, task, box, worker, target, epoch, plan,
workspace, evaluator, readiness, grant and reservation identities. Display names,
pane numbers, host reachability and capacity labels cannot replace these fields.

The owner supplies a fresh random 32-byte key for this exact stream, generation
and runtime. The [owner enrollment service](worker-enrollment.md) now issues and
reviews that material against an active kernel lease. Distribute it only over a
separately authenticated, confidential channel. Host authentication, machine
registration and automatic key rotation are **not implemented by this module**. HMAC-SHA256 authenticates possession
of the scoped key; it is neither encryption, remote attestation, nor proof that
worker claims are true. Producer and receiver share that key and can both produce
MACs. Keys and spool directories must remain outside model/workspace write grants.

Batch, acknowledgment and stored configuration use distinct MAC domains. Reopening
a spool with another role, key, generation, runtime or binding is rejected. The key
is not stored in its database, passed in process argv/environment by this module,
or included in inspection results. The private file reader accepts exactly 32 raw
bytes, never a symlink, hard link, public file, pipe or automatic account lookup.

## Python integration

Owner embeddings use `camol.worker_delivery.WorkerDelivery`:

```python
producer = WorkerDelivery(producer_dir, stream, enrollment_key,
                          role="producer", create=True)
receiver = WorkerDelivery(receiver_dir, stream, enrollment_key,
                          role="receiver", create=True)

producer.queue("checkpoint", {"summary": "bounded checkpoint"},
               occurred_at=worker_reported_timestamp)
batch = producer.batch()
# In the authoritative controller's serialized lease/ingestion section:
ack = receiver.accept(batch, authorize=lambda binding: authorize_state(
    freshly_read_state, binding, now=controller_timestamp))
producer.acknowledge(ack)
```

The variables above are explicit embedding inputs, not discovered defaults. The
owner must hold the controller's lease-mutation serialization boundary around
fresh state reading, authorization and receiver ingestion. A stale snapshot or a
worker-provided state is not authorization. `authorize_state` checks the active
kernel fence, epoch, worker, admitted runtime, plan and effective lease expiry;
future-dated, expired, replaced or terminal work cannot append fresh evidence.
The separate owner enrollment pins control-plane/generation/stream identities.
The module cannot enforce an arbitrary embedding's controller lock or callback.

`deliver_once(producer, transport)` performs one explicit batch/ack exchange. The
transport is an owner-provided callable with its own confidentiality, timeout,
cancellation and output bounds. It is not automatically retried or described as a
native bounded network transport. The byte protocol can be carried over existing
authenticated channels; no network listener or new remote command is installed.

The producer commits before exposing a batch. The receiver authenticates the batch,
validates a contiguous hash chain and authorizes every new record before committing
the whole batch. Only then is an acknowledgment returned. A storage error or denied
record rolls back the batch. If the reply is lost after commit, reconnecting and
resending the same batch recovers its receipt without duplicate ingestion.
Changing a previously used sequence or its predecessor is rejected.

An authenticated exact retry of already-stored records can recover an acknowledgment
after the lease ends. It cannot append anything or call the lease guard again.
Acknowledgments bind the exact stream, sequence and queued digest; stale replies
cannot roll the producer cursor backward. Reopening a spool preserves pending work.

## Evidence, backpressure and inspection

Allowed records are `heartbeat`, `checkpoint`, `tool`, `artifact`, `usage`,
`diagnostic` and `turn_result`. Bodies are bounded JSON objects, not executable
commands. Artifact bodies may identify evidence, but this module does not transfer
or verify artifact bytes. Usage remains a worker claim, not a provider bill. Worker
timestamps are not controller-measured latency. No received claim changes kernel
task state, usage accounting, evaluator gates or final acceptance.

Capture redaction occurs before producer signing, including known raw printable,
hex and standard/URL-safe base64 representations of the enrollment key. New
receiver records violating its redaction policy are rejected without being stored. This is the existing
best-effort redaction policy, not a guarantee that all sensitive data is detectable.
Exact receipt recovery and historical reads do not reinterpret retained records
against later environment secrets. The operator must apply data-classification and
retention policy before enabling production transcripts.

Each record is at most 32 KiB; a batch holds at most six records and 256 KiB;
inspection pages hold at most 100 records. A producer blocks at 256 unacknowledged
records. Each spool holds at most 10,000 total records; acknowledged records are not
silently discarded. These are fixed V1 safety ceilings, not proven fleet-scale
limits. SQLite commits use full synchronization and bounded lock waiting, with
private owner-controlled files. Unacknowledged backlog must throttle worker work
in the eventual executor; the current local scheduler does not yet consume it.

For an existing enrolled spool:

```text
camol worker-delivery inspect --root EXISTING_SPOOL --binding STREAM_JSON \
  --key-file PRIVATE_RAW_KEY --role producer --after 0 --limit 100
```

Use `--role receiver` for its inbox. Uppercase arguments are explicit local paths.
Inspection does not create storage, enroll, connect or execute. It verifies the
exact local enrollment and reports pending counts plus retained records. Its flags
`execution_authority=false` and `kernel_promoted=false` are intentional. A fully
acknowledged producer is not proof of kernel consumption or safe worker teardown.

## Remaining execution integration

Owner-reviewed stream enrollment and controller-serialized receipt ingestion are
now implemented separately in [worker enrollment](worker-enrollment.md). The next
required work is an authenticated remote worker service and machine adoption,
source/workspace delivery, target-side admission and fenced launch, independent
artifact/evaluator ingestion, controller-serialized promotion cursors, heartbeat
reconciliation, remote cancellation and salvage. Generation-key rotation,
confidential transport, native event pumping, retention and teardown proofs also
remain open. The current primitive is tested with real local child processes and
real kernel leases, not an actual remote fleet or hosted model account.
