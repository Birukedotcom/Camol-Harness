# Read-only retention inventory

This is the first bounded implementation toward SPEC §9 and §19.5. It inventories
ordinary artifact references and names protected storage boundaries. It does **not**
enforce expiry, approve cleanup, archive data, truncate logs, remove files, unload
models, reconcile remote effects, or claim a complete recoverable snapshot.

## Public API

The CLI exposes the same inspection only:

```sh
camol retention inspect --state-dir /absolute/state --db /absolute/state/camol.sqlite3 --run-id EXACT_RUN --policy inspection-policy.json
```

Replace the illustrative paths and run identity with the actual stopped run.
Output binds `inventory` and `inventory_digest`; exit zero means inspection
completed, never that removal is safe. Refusal exits two. No default run, policy,
archive, or purge action is inferred. The strict policy file is bounded to 64 KiB.

```python
from pathlib import Path
from camol.retention import (
    CLASSES, InventoryLimits, RetentionPolicy, RetentionRule, inspect_retention,
)

policy = RetentionPolicy(
    run_id=run_id,
    plan_digest=frozen_plan_digest,
    owner=recorded_approving_owner,
    rules=tuple(RetentionRule(name, None, "hold") for name in CLASSES),
)
inventory = inspect_retention(
    state_dir=Path(explicit_state_directory),
    database=Path(explicit_database_path),
    run_id=run_id,
    policy=policy,
    limits=InventoryLimits(),
)
report = inventory.to_dict()
report_digest = inventory.digest()
```

Paths must be existing, absolute, canonical paths without symlink components. The
database must be contained in the selected state directory. Nothing searches the
home directory, discovers another project, or follows paths found in event bodies.

`RetentionPolicy.from_dict()` accepts exactly schema `camol.retention_policy`,
version `1`, `run_id`, `plan_digest`, `owner`, and `rules`. Each rule has exactly
`content_class`, `max_age_seconds`, and `disposition`. There must be one rule for
each of `transcripts`, `audio`, `model_inputs`, `tool_results`, `artifacts`,
`recovery`, and `unknown`. Ages are null or positive bounded integer seconds;
dispositions are `retain` or `hold`. Unknown content requires indefinite hold.
Use the existing strict JSON contract decoder when accepting JSON text/files;
`from_dict` cannot recover duplicate keys already discarded by another parser.

Policy identity must match the selected frozen run and recorded human plan owner;
registered worker identities cannot supply that owner. This comparison is **not**
a new approval, a proof that the current caller is human, or a retroactive change
to the original runbook. Rules are declarations for inspection only. Expired ages
do not release any hold in this slice.

## Snapshot and limits

Only a cold, checkpointed database is supported. A nonempty WAL or rollback
journal raises `RetentionError` with `UNSUPPORTED_LIVE_SQLITE`; the inspector never
checkpoints or repairs it. The inspector streams a bounded database snapshot from
its verified nofollow source descriptor into a fresh 0700 diagnostic directory in
the system temporary area, using a 0600 file. SQLite opens only this private copy,
never the source pathname; a source-path swap cannot redirect SQLite to another
database. The temporary snapshot is removed on success or failure, is not a durable
archive, and is the only content written by inspection. The source remains untouched.
A read-only immutable SQLite connection avoids the SHM
creation/write behavior possible with ordinary `mode=ro` on a live WAL database.
Stop and drain through the existing owner-controlled harness workflow before
requesting inspection; do not delete sidecars to satisfy this prerequisite.

One read transaction captures every event stream in this database. Each stream
gets its event count, contiguous last sequence, and ordered canonical-event digest.
The report also binds the selected plan, policy, state-directory device/inode,
and database content digest. It rechecks file identities, retained artifact bytes,
database bytes/sidecars and shallow directory metadata before returning. Concurrent
changes require a fresh inspection; the result grants no future lock or lease.
The same-user filesystem/SQLite trust boundary still applies: this is not protection
against a hostile kernel or an owner deliberately arranging an undetectable ABA.

Default bounds are 128 MiB database, 20,000 events across 256 streams, 1 MiB per
event, 32 MiB aggregate event payloads, 10,000 referenced objects, 8 MiB per blob,
64 MiB aggregate blob bytes, 256 root entries, and 100,000 JSON nodes per event.
JSON depth and SQLite VM work are also bounded. Database and blob verification
read bytes twice to detect intervening substitution; these are inspection limits,
not a run-wide disk quota or an enforced storage-retention policy.

Reads require owner-controlled, single-link regular files and descriptor-anchored
directories. Symlinks, hard links, pipes, devices, escaped database paths, malformed
JSON, missing prerequisites, and exceeded bounds refuse the snapshot without
changing permissions or creating source files. `RetentionError` always means
retain everything, not that cleanup is permitted.

## Ownership and coverage

`RetentionInventory` contains typed `StreamCut`, `ArtifactInventory`, and
`ProtectedRoot` records. Artifact ownership comes from all referring **event
streams**, not the artifact producer's claimed identity:

- `run_exclusive`: all recognized references in this database snapshot belong to
  one run, identified by `reference_owners` (which may be another run).
- `state_shared`: two or more runs refer to the same digest.
- `unknown`: unsupported event/reference semantics make exclusivity incomplete.

Identical references deduplicate stored bytes while preserving reference counts
and owners. Conflicting byte counts, missing bodies, and corrupt bodies are holds.
`bytes_verified` concerns the retained CAS bytes only; `full_source_retained` is
false for truncation or transformed/redacted content. Neither proves that a
disposable worktree is recoverable. The inspector does not replay/validate every
event's execution semantics or re-evaluate old gates.

Every artifact's content classification remains unknown: a filename, media type,
or redaction flag cannot establish that it is nonsensitive. Every action is `hold`.
`reference_scan_complete` refers only to recognized event-reference extraction;
`filesystem_inventory_complete` and `deletion_authorized` are always false.
No claim is made about CAS references in unscanned external/shared files or about
unreferenced CAS blobs.

The shallow root listing marks these boundaries held without opening their
contents: ordinary unenumerated CAS data, raw salvage, worktrees/records, invocation
packets, control credentials/locks, model stores/hosts, SSH receipts, watcher state,
project transcripts, and provider-preflight accounting/unknown-effect journals.
Unrecognized roots remain unknown/held. Arbitrarily named or external stores are
not discovered; explicit non-observation is reported. External source repositories,
model files, API keys, watcher journals and remote targets are never followed.

## Before any future purge

The [portable ledger archive boundary](archive-boundary.md) now bounds and anchors
archive reads/writes, rejects ambiguous JSON and unsafe member paths, and preserves
the distinction between integrity verification and recovery authority.

The existing run export includes ordinary ArtifactRefs and revision lineage, not
the separate salvage CAS or all required Git objects. Verified replay is not a
teardown receipt. A later archive slice must independently restore pinned Git and
salvage bytes; a later purge slice needs kernel sealing, shared-reference release,
preserved accounting/unknown-operation guards, explicit authority and durable
archive/tombstone semantics. None of those capabilities are asserted here.

[Encrypted workspace recovery](workspace-recovery.md) now independently reconstructs
one explicitly selected captured workspace's Git history and salvage bytes. It is
not attached as a complete run backup or teardown grant: v1 salvage coverage gaps,
shared state/accounting and remote effects remain held. Inventory inspection does
not automatically invoke or approve that export.
