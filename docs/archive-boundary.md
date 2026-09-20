# Portable ledger archive boundary

`camol export` and `camol verify-export` retain their existing v1/v2 archive
format. They export a selected event stream, required revision lineage, and the
ordinary content-addressed artifacts referenced by those streams. They do not
copy a repository, download dependencies, invoke providers, or resume a run.

## Integrity, not recovery authority

Successful verification means the bounded manifest, event bytes and referenced
ordinary blobs match their hashes and counts. Revision lineage is checked by its
existing verifier. Export derives redaction checks, lineage and blob inventory
from the encoded snapshot, not subsequently mutated caller objects.
`verify-export` projects exactly that verified in-memory event
snapshot; it does not reread a possibly changed archive for the displayed result.

Hashes are not signatures. An archive author who can rewrite everything can
compute new hashes. No archive grants approval, provider access, a lease, or
authority to execute recorded commands. Replay is a projection, not execution.
Unlisted files are not opened or covered by the verified inventory. Verification
does not make arbitrary additional archive contents safe to extract.

This is **not a complete recovery backup**: raw salvage, pinned Git objects,
control state, credentials, shared accounting journals and remote effects are not
included. Redacted or truncated artifacts do not reproduce their source bytes.
No cleanup/purge permission follows from an export. See
[retention inventory](retention-inventory.md) for the remaining recovery gates.

## Filesystem boundary

Select a new output directory, or an existing empty directory owned by the caller
with private permissions (0700). New archive directories are 0700 and files 0600,
subject to a more restrictive process umask. Existing directories are not chmodded.
Existing files are never overwritten. Each completed file is flushed and fsynced;
the manifest is written last. A failed export can leave a private partial output:
it is not silently deleted, reusable, or evidence of successful verification.

The caller's parent path is resolved once, allowing normal macOS `/tmp` and `/var`
aliases. The selected final root must not itself be a symlink. All members below
that root are accessed relative to pinned directory descriptors without following
links. Source CAS reads receive the same treatment under the selected state root.
Special files, pipes, hard links, linked ancestor directories, changed identities
and escaped paths are refused. Reads are nonblocking before file-type validation.
File and directory identities are rechecked before a successful return.

This does not create a transactional filesystem snapshot or protect against a
hostile kernel or an owner deliberately manipulating the filesystem. It prevents
member paths from granting arbitrary traversal and detects observed replacement;
it does not create future validity or a deletion lock.

V2 lineage IDs must fit a single safe filename component. IDs containing `/`,
`\\`, `.` or `..` as a whole component are refused, rather than interpreted as
paths. Other run IDs are not used as archive member paths. A future format can
encode more general lineage IDs without reviving path traversal.

## Bounded parsing

Both export and verification fail closed above these implementation ceilings:

- 4 MiB manifest; 1 MiB per event; 32 MiB total event-stream bytes.
- 20,000 events across the selected stream and at most 256 lineage streams.
- 10,000 distinct ordinary blobs; 8 MiB each; 64 MiB total retained blob bytes.
- 1,000,000 JSON value nodes across manifest/events and maximum depth 64.

Duplicate JSON keys, non-finite numbers, invalid UTF-8, non-object event records,
boolean event counts and inconsistent artifact sizes are rejected. Failure is
reported without echoing supplied file contents. These are archive-operation
bounds, not retention enforcement or run-wide storage quotas; larger runs need
a future explicit bounded streaming format, not unchecked reads.
