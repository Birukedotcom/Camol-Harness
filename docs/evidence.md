# Evidence, artifacts, export, and replay

Camol treats the SQLite ledger and its content-addressed artifact store as the
authoritative record. Terminal scrollback is a projection and is not required
for recovery or review.

## Evidence records

Every newly ingested item is a versioned `camol.evidence` record bound to exactly
one task lease or one debug case. Task evidence carries the run, task, agent,
lease, and current fence digest. Replay rejects an item attached to another or
expired assignment context.

The first epistemic statuses are:

```text
OBSERVED | EXECUTED | DERIVED | INFERRED
HUMAN_REPORTED | UNVERIFIED | CONTRADICTED
```

Worker-returned evidence is always ingested as `UNVERIFIED`. A worker cannot set
its own status or attach a forged artifact reference. Camol-controlled adapter,
collector, and verifier boundaries may emit `OBSERVED` or `EXECUTED` evidence.
The completion gate accepts only the status appropriate to each evidence kind;
an unverified command cannot satisfy an executed-command requirement. A claim
remains `UNVERIFIED` by definition until evaluators accept or contradict it.

Evidence payloads reject unknown envelope fields, unsupported JSON values,
nesting beyond 16 levels, excessive collections, and inline data above 256 KiB.
Larger content belongs in the artifact store.

## Artifact records

`ArtifactStore` writes immutable blobs below:

```text
STATE_DIR/artifacts/sha256/<first-two-hex>/<remaining-hex>
```

An `ArtifactRef` records:

- the digest and size of the retained bytes;
- the hash and byte count of the complete observed source;
- media type and encoding;
- whether redaction or truncation occurred and the exact retention policy;
- run, task, worker, lease, channel, invocation, and producer role; and
- the observation timestamp.

Text is redacted before persistence. Raw secret-bearing stdout, stderr, packet,
result, diff, and text-artifact content is not copied into the ordinary blob
store. Existing blobs are verified byte-for-byte before reuse; missing,
symlinked, size-mismatched, or digest-mismatched content is corruption.

Process pipes are drained concurrently to avoid deadlock, hashed over their full
content, and retain at most 1 MiB per stream. The ledger makes truncation visible.
The adapter records its command envelope, sanitized argv, cwd, allowlisted
environment names (never values), sandbox identity, timestamps, exit status, and
stdout/stderr references. The verifier does the same. Workspace snapshots store
the tracked binary diff plus content-addressed copies of changed regular files.

## Portable archives

```bash
camol export \
  --db /absolute/state/camol.sqlite3 \
  --state-dir /absolute/state \
  --run-id RUN \
  --output /absolute/export

camol verify-export /absolute/export
```

An archive contains canonical JSON Lines events, every referenced blob, and a
digest-bound manifest. Export first verifies each blob and refuses a ledger that
still contains material the active redactor would change. `verify-export`
checks the manifest, event stream, blob inventory, blob hashes and sizes, then
rebuilds the run projection from events. A successful replay therefore does not
depend on the originating terminal session or live state directory.

Binary artifacts are retained byte-for-byte in V0. Workflows carrying regulated
or secret binary data must keep those files out of ordinary evidence until an
encrypted retention backend and corresponding authority policy are configured.
