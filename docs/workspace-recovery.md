# Encrypted workspace recovery

This implements source-independent recovery of an **already captured**
`SalvageReceipt`: its base/head commit histories, tracked binary patch and regular
untracked bytes. It does not silently capture a live worker, revise its plan,
resume a task, remove a worktree or authorize retention cleanup.

## Install and commands

The optional `recovery` extra supplies the authenticated-encryption library. From
this source checkout, in its virtual environment:

```sh
python -m pip install -e '.[recovery]'
camol recovery --help
```

A system Git supporting v3 bundles and bundle
stdin is required; verification currently covers Apple Git 2.50.1 with SHA-1
and SHA-256 repositories. No cloud or model account is involved.

The commands below show the required inputs, not existing paths on your machine:

```text
camol recovery keygen --output NEW_PRIVATE_KEY_DIRECTORY
camol recovery export --source SOURCE_REPOSITORY --state-dir STATE_DIRECTORY \
  --salvage CAPTURED_RECEIPT.json --policy EXPLICIT_POLICY.json \
  --key-file PRIVATE_KEY_DIRECTORY/key.bin --output NEW_ARCHIVE_DIRECTORY
camol recovery verify --archive ARCHIVE_DIRECTORY --key-file PRIVATE_KEY_DIRECTORY/key.bin
camol recovery restore --archive ARCHIVE_DIRECTORY --key-file PRIVATE_KEY_DIRECTORY/key.bin \
  --output NEW_RESTORED_REPOSITORY
```

`keygen` creates a 0700 directory containing a 0600, 32-byte key file. It prints
the path, never the key. Keep that key separate from the archive, back it up
securely, and do not commit it. Losing the key makes the capsule unrecoverable.
Existing key files must be owner-private regular files, without links.

An export policy is an explicit caller opt-in, for example:

```json
{
  "schema": "camol.recovery_policy",
  "schema_version": 1,
  "owner": "your-owner-id",
  "purpose": "workspace-recovery",
  "salvage_digest": "sha256:EXACT_CANONICAL_SALVAGE_RECEIPT_DIGEST",
  "allow_encrypted_raw": true
}
```

Replace the owner and digest with the actual values. The digest is
`SalvageReceipt.from_dict(receipt_json).digest()`, not a hash of the JSON file's
whitespace. No additional fields or implicit opt-in are accepted. The owner field
does **not** authenticate the caller or manufacture a human/kernel approval. This
trusted local interface assumes the caller already has access to the source,
salvage and encryption key; it is not a multi-user access-control service or an
automatic retention-policy amendment.

Python embeddings use `export_workspace_recovery`, `verify_workspace_recovery`,
`restore_workspace_recovery`, `generate_recovery_key` and `load_recovery_key` from
`camol.recovery`. Key bytes are explicit arguments, never environment-variable or
account discovery. Existing runtime salvage capture remains a separate operation.

## What is actually recovered

The single `recovery.camol` file encrypts both metadata and content. AES-256-GCM
uses a fresh random 12-byte nonce and a versioned format marker as authenticated
associated data. Authentication failure precedes creation of a restore target.
The implementation uses the maintained library API, not custom cryptographic
primitives. See [the AEAD documentation](https://cryptography.io/en/latest/hazmat/primitives/aead/).

Git's own `pack-objects` writes the selected base/head reachable object graph to
a pack. A strict [v3 bundle header](https://git-scm.com/docs/gitformat-bundle) names
only `refs/recovery/base` and `refs/recovery/head`, with no prerequisites or
optional filters. This avoids temporary refs in the source repository. Unrelated
branches and objects unique to them are not selected. Source refs, checkout and
salvage CAS are not changed. Shallow history is refused, not called complete.

Before publishing an export, Camol reconstructs a separate repository from those
bytes. Restore independently verifies/unbundles Git objects, checks both exact
commit identities, runs strict Git object checking, reconstructs the candidate
index from the base plus patch, and copies raw object bytes into the new workspace.
Object metadata and bytes are read in two bounded batches, not two Git processes
per file; repeated object IDs are read once while their restored file sizes still
count separately toward the workspace byte limit.
No checkout filters, hooks, templates, provider command, project script or network
protocol is enabled. Executable tracked files and tracked symbolic links are
preserved as data; they are not run. Links are never followed when writing another
member. Untracked paths cannot install `.git` controls or collide with tracked
content. The path contract conservatively rejects compatibility-normalized Git
control names, short-name-style aliases, Unicode format/control characters,
backslashes and alternate-stream colons; unsupported filenames are not renamed.
No remotes or borrowed object-store links are configured.

`verify` really reconstructs into private temporary storage and removes its own
scratch copy afterward. `restore` deliberately produces plaintext project files
in the explicitly selected new private directory. Treat those files as sensitive
and as untrusted project code until reviewed. A result binds the capsule digest,
salvage digest and reconstructed candidate tree. Both `resume_authorized` and
`cleanup_authorized` are always false.

## Scope and limits

V1 salvage contains a flattened base-to-worktree patch, not the original staged
versus unstaged split. Restoration keeps the original head commit and stages the
captured tracked tree relative to it. Untracked file content is preserved but v1
did not capture its original mode, timestamps or extended attributes; it is
restored privately without an executable bit. Ignored files absent from the
receipt, empty directories, external submodule contents, Git LFS objects outside
Git, credentials/config/hooks, run ledgers, invocation journals and remote effects
are **not** recovered. Gitlink/submodule entries in the captured candidate are
refused. LFS pointers remain pointers. This is not an entire-project or entire-run
backup, and cannot support a claim of safe teardown on its own.

Bounds: 64 MiB encrypted capsule, 16 MiB bundle, 100,000 Git pack objects, 10,000
candidate/untracked entries, 8 MiB per retained content object and 32 MiB total
captured/restored content. Git commands have 30-second deadlines within a
120-second plumbing budget and observed output ceilings. These are not hard OS
CPU/memory/disk quotas; pack decompression still runs in the trusted host Git
process. The operator must trust the installed Git and host boundary. No support
is claimed for hostile-kernel or deliberate same-owner filesystem races.

New archive/output directories are private and existing content is never
overwritten. Outputs must be separate from input source/state/common Git stores
or the input capsule. A failed restore may leave its explicitly requested partial
private directory, which is not declared complete and is not automatically erased.
Private temporary files are removed normally, not securely erased; no guarantee
is made against host swap, privileged access or forensic disk recovery.

For recorded evidence plus every captured candidate and accepted commit across a
completed run's ancestry, see [completed-run recovery](run-recovery.md). That
additional layer still does not restore operational authority.

Full retention enforcement, expiry, archive-key lifecycle, multi-user authorization,
complete run/control/accounting capture and relocation/adoption remain separate
requirements. This recovery proof alone does not release any hold described in
[retention inventory](retention-inventory.md).
