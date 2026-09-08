# Durable observation

Camol's observer loop is implemented for embedding, foreground CLI use, and the
detached supervisor. A terminal closing does not end the daemon's approved watches.
This release includes one standard source: a bounded, owner-controlled local JSONL
journal of normalized observation metadata. It does **not** yet include a live GCP,
GitHub, voice-provider or database connector, nor prove their source coverage.

## Two distinct contracts

`WatchSpec` freezes source identity, query, expected event classes, correlation
keys and exact target identity, terminal event, parser/schema/fixture digests, poll
timeout and batch bound. An unrelated call/build ending cannot complete this watch.

`camol.watch_schedule` v1 separately freezes the exact source plugin and
configuration, interval, total poll count and absolute expiry. Both require explicit
owner approval. Credentials belong in host-owned resolvers, never in these contracts
or public cursors. A scheduled poll consumes one attempt before querying; cancellation,
failure and restart do not refund it. Repeating the same schedule approval is
idempotent and cannot reset its budget. A changed budget needs a new reviewed digest.

The approved plugin implementation digest must still match after upgrade/restart.
Unknown or changed plugins yield `OBSERVATION_INCOMPLETE`. Camol does not import a
module or execute a command named by a worker-provided source configuration.

## Python embedding

Use `with Harness(workspace, state_dir) as run` after preparing and approving a run.
Construct a `WatchSpec`, then explicitly create it with
`run.watch(spec, approved_by=owner)`. `run.observers()` returns a `WatchRuntime`.
Its `configure(schedule, approved_by=owner, approval_digest=canonical_digest(schedule))`
does not query or start a background task.

Await `runtime.tick()` for one due pass, or `runtime.run_until_settled()` for the
bounded loop. The embedding host owns this awaitable and must await cancellation
before closing the harness. `Harness.run_async()` does not silently start a second
observation lifetime. Pass trusted `ObservationSource` implementations through
`run.observers(sources=[...])`; custom plugins must be registered again on restart.
They implement `validate`, asynchronous `probe`, and asynchronous `fetch`, and must
actually terminate when cancelled. Source callbacks are trusted host extensions,
not a sandbox for arbitrary Python code.

## Standard normalized journal

The source path must be absolute, canonical and owned by the current OS user. No
symlinks, device files, FIFOs, or group/world-writable journal are accepted. The
approved read ceiling is at most 16 MiB; unbounded log tails need another plugin.
Nothing in the journal is executed. It is a metadata feed, not a generic parser for
unstructured or sensitive log bodies.

Use `JournalSource().binding(source_id, path)` to obtain the frozen plugin binding.
The spec query is exactly `journal:<source_id>` and parser version is `jsonl-v1`.
`journal_header(spec)` produces the first JSON line. `journal_fixture_digest(spec)`
produces the digest of the parser's explicit positive/negative fixture contract;
set it on the finalized spec. Each following newline-terminated record has exactly:

```json
{
  "event_id": "build-one",
  "revision": 1,
  "event_class": "ended",
  "correlation": {"build": "one"},
  "occurred_at": "2026-09-07T00:00:00+00:00",
  "content_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

The illustrative digest above is not evidence; real producers hash their actual
source content. The header and each record must match the approved spec. Probe
receipts establish accessible instrumentation and parser behavior only: a producer
can lie or omit events, and a readable file cannot prove exhaustive cloud coverage.
Duplicate JSON keys (including nested correlation keys), non-finite values and
ambiguous cursor/header objects are rejected, not resolved using a last-value rule.
The parser's readiness fixtures exercise these negative cases.

The durable cursor pins device/inode, consumed-byte offset and a prefix hash.
Appends advance it atomically with accepted observations. Previously consumed-byte
changes and file rotation fail closed, preserving the old cursor. A partial last
line waits. Supply a new reviewed watch/source contract to adopt a rotated journal;
Camol does not silently skip unknown bytes.

## CLI and daemon

```text
camol watch validate --spec watch.json --schedule schedule.json
camol watch create --state-dir STATE --workspace REPO --spec watch.json --by OWNER --digest SPEC_DIGEST
camol watch schedule --state-dir STATE --workspace REPO --schedule schedule.json --by OWNER --digest SCHEDULE_DIGEST
camol watch poll --state-dir STATE --workspace REPO
camol watch run --state-dir STATE --workspace REPO
camol watch inspect --state-dir STATE
camol watch stop --state-dir STATE --workspace REPO --watcher-id ID --by OWNER --reason REASON
```

The capitalized arguments are explicit values, not shell commands to paste unchanged.
`validate` prints the canonical digests without starting work. `inspect` is read-only
and does not create absent state. Offline mutation/polling holds the same exclusive
state-directory lock as execution. Add `--live` to create, schedule, inspect, stop or
reopen through an already-running local supervisor; no second database owner is
created. `serve`/`start` automatically resume approved standard-source schedules.
Drain pauses new queries; shutdown awaits cancellation of an active query.

## Failure and authority semantics

- Runtime poll, batch and instrument-failure events have the dedicated
  `camol-observer` actor; replay rejects worker impersonation. Creation, scheduling,
  reopening and stopping remain exact owner actions. Actor names are structural
  provenance checks, not cryptographic authentication: controller filesystem and
  process isolation still protect the writer boundary.
- Empty results, parser/schema drift, source errors and timeouts remain waiting.
  Exception bodies and source payloads are not copied into the ledger.
- Poll authority ends at the earlier of the per-poll deadline and schedule expiry.
  Late results are rejected during ingestion **and replay**, even after a clock jump.
- A crash leaves an unknown, charged poll. It can be closed as `recovered_unknown`
  only after its bounded lifetime; this does not claim that the query succeeded.
- An unrelated run event does not invalidate a pending source read. A changed watch
  cursor, spec, attempt or approval does: true concurrent results cannot overwrite it.
- Duplicate event/revision pairs are idempotent; conflicting content is a visible
  conflict. Newer revisions supersede older ones for terminality within a batch.
- A completed watch does not poll forever. Explicit owner reopen is required to
  observe a later correction, and only new source evidence can complete it again.
- Owner stop is `stopped`, not `completed` or successful evidence. Expiry and exhausted
  poll count are visible scheduler states, never successful completion.

Watchers are separate observation contracts, not automatic task-success or invariant
votes. A workflow must explicitly connect observed evidence to its evaluator and
acceptance requirements. This source protocol does not establish exactly-once cloud
effects or authorize a deployment, message or data deletion.

Tests cover real-file streaming, partial records, revision conflict, corruption,
permissions, cursor restart, actual supervisor restart, approval retry, cancellation,
unrelated event interleaving and mid-poll authority expiry.
