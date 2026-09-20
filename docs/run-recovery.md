# Completed-run evidence and code recovery

This is an explicit backup of a completed run's **recorded evidence and code**.
It is not an operational snapshot, a resumed run, permission to delete the source,
or proof that all project data was captured. The independent reconstruction works
without the original checkout or state directory. It does not launch an agent.

## Review and CLI

Install the optional `recovery` extra. Use the real source checkout, existing state
directory, and exact completed run ID. Stop its supervisor first: planning/export
acquire the normal exclusive `Harness` owner lock and refuse a competing owner.
The CLI opens the existing database through the normal harness lifecycle; it is
not a strictly non-mutating database inspection command. No worker/model starts.

The command sequence is:

1. `camol recovery plan-run --source SOURCE --state-dir STATE --run-id RUN`
2. Read the returned scope, streams, captures, accepted revisions, exclusions,
   limits and `review_digest`. `content_verified: false` means this review has not
   checked that Git objects or artifact bodies are still available.
3. `camol recovery keygen --output NEW_PRIVATE_KEY_DIRECTORY`
4. `camol recovery export-run --source SOURCE --state-dir STATE --run-id RUN --by OWNER --review-digest DIGEST --allow-encrypted-raw --key-file KEY_DIRECTORY/key.bin --output NEW_ARCHIVE`
5. `camol recovery verify-run --archive ARCHIVE --key-file KEY_DIRECTORY/key.bin`
6. When wanted, `camol recovery restore-run --archive ARCHIVE --key-file KEY_DIRECTORY/key.bin --output NEW_RESTORED_DIRECTORY`

Uppercase names are values to replace, not runnable paths. `--db` optionally
selects an existing database for planning/export. Do not supply credentials or key
bytes in command arguments. Keep the private key separately from the archive;
loss of it makes the encrypted parts unrecoverable. The owner comparison binds
the recorded plan owner, not a new multi-user identity or human-authentication
system. Export requires that exact owner, exact review digest and explicit raw
content opt-in. Changed events require a new review.

## Contents and independent verification

The selected run must be completed, have accepted integrated code, and satisfy
the existing quiescence predicate. Complete linked-plan ancestry is verified;
every stream must have the same recorded approving owner. Recovery covers every
recorded candidate/salvage receipt and every accepted integration commit across
those streams, deduplicated by exact identities. Failed attempts with captured
salvage are included; unrecorded work is not invented.

The private archive contains:

- `ledger/`: the ordinary redacted, replayable event/artifact archive, including
  revision ancestry. **This directory is not additionally encrypted.** Redaction
  is not a guarantee that the remaining evidence is nonsensitive.
- `captures/<digest>/recovery.camol`: encrypted raw workspace recovery capsules.
- `commits/<revision>.camol`: encrypted Git bundles for accepted commits.
- `manifest.camol`: an encrypted, authenticated coverage manifest, written last.

The manifest uses a distinct versioned marker and AES-GCM authenticated context;
accepted-commit parts also bind the exact revision in that context. It binds the
ordinary ledger manifest, exact human-reviewed coverage, and every part's bytes,
digest and reconstructed tree. Missing or extra declared parts, substituted
receipts, changed coverage, wrong keys, changed bytes and unsafe filesystem
members refuse completion. Possession of the key is not an external signature or
proof against an archive creator who can replace both evidence and keys.

Export independently reconstructs all code and re-verifies the ordinary ledger,
pins its member identities, then rechecks the owner-held review before publishing
the manifest. Verification reconstructs in private temporary storage and removes
its own scratch. Restore replays and recomputes coverage from the archived ledger,
reconstructs the code into new repositories, copies and re-verifies the ledger,
and writes `recovery-result.json` last. No hooks, project commands, model calls,
remotes, source refs or source state stores are installed/changed by reconstruction.

Restored code is **plaintext**, potentially sensitive and untrusted to execute.
`captures/<digest>` contains each captured workspace; `accepted/<revision>` contains
each accepted commit repository. `accepted_head` identifies the final accepted
revision. `ledger/` remains historical evidence, not a new mutable run database.
Results always say `operational_restore: false`, `resume_authorized: false`, and
`cleanup_authorized: false`.

## Embedding

An open, stopped `Harness` exposes `recovery_plan()` and
`export_recovery(destination, by=..., review_digest=..., allow_encrypted_raw=True,
key=...)`. The latter supplies the final owner-held recheck. Pure
`plan_run_recovery(events=..., lineage=...)` does not read Git, CAS or encryption
keys. `verify_run_recovery` and `restore_run_recovery` need only archive/key and,
for restore, a new output path.

The lower-level `export_run_recovery` is a trusted embedding primitive: its caller
must own the run lock and arrange the authoritative recheck. Supplying event
dictionaries or an owner string does not itself establish that ownership. Prefer
the `Harness` wrapper for an actual state directory.

## Bounds and exclusions

V1 allows at most 1,024 code parts, 128 MiB of stored encrypted code, 256 MiB of
aggregate reconstructed file content, and 50,000 reconstructed entries. Individual
workspace/bundle/object limits still apply. The ordinary ledger retains its own
archive limits; its bytes are not included in the stored-code ceiling. Git
reconstruction shares a 900-second plumbing deadline across parts; export also
has a 1,800-second aggregate deadline checked between operations. Per-command
Git deadlines remain bounded. These are not hard OS CPU, RAM or whole-disk quotas:
Git metadata/objects and export validation scratch also consume space, and a part
can fail after creating private partial output.

The [workspace recovery](workspace-recovery.md) limits still apply: salvage V1
flattens staged/unstaged changes, lacks original untracked modes/metadata, and
does not recover ignored files, external submodules or external LFS objects.
Credential stores, live processes, shared accounting journals, unrecorded files,
remote effects, operational adoption and cleanup permission are excluded.
Different recorded ancestry owners are conservatively refused.

Only absent or empty private outputs are accepted; input stores are not overwritten.
Failed private outputs are not automatically erased or declared complete. The
installed Git, cryptography and owner-controlled host remain trusted; no hostile
kernel or deliberate same-owner race protection is claimed. No retention hold is
released. Full state/control/accounting restore, key lifecycle, expiry and safe
teardown remain separate work.
