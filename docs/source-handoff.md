# Approved source handoff

The source handoff path copies a source-bound run's approved baseline or exact
task-specific accepted source to a destination workspace for an adopted target
generation. It is a prerequisite for
distributed execution, not a remote executor, transport connection, readiness
receipt or worker grant. The target descriptor is owner-reviewed metadata, not
hardware identity or a live controller authorization at the receiving machine.

## Prepare, export, receive

The run must already have a source baseline bound before plan approval and an
active adopted target. Missing source identity is an error, not permission to
substitute the current checkout. V1 carries only the initial approved baseline.
V2 binds a pending task to the current accepted integration head, or the approved
baseline if no work has been integrated. Incremental artifact transfer remains a
separate distributed-dispatch requirement.

## Task-specific source (V2)

Add `--task-id TASK` to `camol source-handoff plan` (or `task_id=...` to
`Harness.propose_source_handoff`) to select task-bound source. The task must be
pending or waiting, with every dependency succeeded. A succeeded, leased, running,
verifying, blocked, unknown or dependency-blocked task cannot obtain a new task-copy
proposal. This is preparation eligibility, not full readiness or permission to run.
Synchronous embedding planning is refused while that `Harness` is executing, so
source inventory cannot block its event loop; stop execution before calling it.
The standalone planning CLI is a separate read-only process and still requires
fresh export approval afterward.

The proposal adds a versioned `selection`: exact frozen task-contract digest,
source origin, origin digest, and clean commit/tree/working-byte identity. An
accepted integration binds its complete kernel receipt and recorded workspace;
the initial baseline binds its original approval record. This means a dependent
task receives earlier accepted changes, not a convenient current branch or the
original repository without those changes. The exporter still checks that the
original human-approved source remains unchanged.

A linked revised plan may inherit an integration head without having a same-run
integration receipt. Its selection binds the exact revision lineage record. Supply
`--source-workspace ABSOLUTE_PATH` (or `source_workspace=...`) naming an existing
clean checkout of that inherited commit, for example the parent's retained
integration workspace. If omitted, the baseline checkout is examined and must
actually contain that exact inherited head; Camol never silently substitutes its
old HEAD or creates a worktree during planning. For same-run integrations or the
baseline, an explicitly supplied path must equal the recorded source path.

V2 uses the same explicit export/receive commands and encrypted package envelope as
V1. The receiver reconstructs the selected commit independently and returns its
actual source identity. Source/head, task eligibility, contract, target and owner
are rechecked during export and before ledger append. If another integration or
task transition makes the selection stale, review a new proposal. A lost event
append can recover its exact package only while its current authorization remains
valid; it cannot use an old package to bypass an advanced task or integration head.
Already-recorded export retries remain historical metadata, not a new copy grant.

The receiver still has no live controller connection. Copy approval can outlive a
controller-side state change until expiry; a received copy is **not** readiness,
an authenticated target attestation, or a worker lease. Distributed dispatch must
revalidate current task/source/target authority before launch. Old evaluator gates,
billing claims, or task success are never inherited merely by copying source.

## Package commands

`camol source-handoff plan` accepts `--state-dir STATE --run-id RUN --generation
GEN --adoption-digest DIGEST --destination-workspace ABSOLUTE_PATH --request-id ID
--by OWNER --expires-at TIMESTAMP`. It only reads the existing ledger. Review the
entire JSON proposal and save it. The destination path is the intended path on
the receiving filesystem, not necessarily on the controller. Use canonical paths
(on macOS `/private/tmp/...` rather than the `/tmp` alias).

The proposal binds source commit/tree/checkout digest, run and plan, complete target
descriptor and adoption digest, destination, owner and a positive approval window
of at most one hour. It explicitly discloses transfer of reachable Git history and
permits encrypted source only. It grants no execution permission.

`camol source-handoff export --workspace SOURCE --state-dir STATE --run-id RUN
--proposal FILE --by OWNER --review-digest DIGEST --key-file KEY --output PACKAGE`
takes the existing exclusive controller lock and writes a new private package
outside the source and state directories. Stop the supervisor first. Encryption
uses the existing optional `camol-harness[recovery]` dependency. An explicit private
32-byte key is required; `camol recovery keygen --output PRIVATE_KEY_DIRECTORY`
can generate one without displaying its bytes. Keys are not included in packages,
events, receipts or Git configuration. Provision them separately over an approved
confidential channel; Camol does not upload or enroll anything here.

Move the two-file package through the owner's chosen transfer mechanism. On the
receiving side, use `camol source-handoff receive --archive PACKAGE --proposal
FILE --review-digest DIGEST --by OWNER --target-id TARGET --generation GEN
--key-file KEY --output DESTINATION`. The exact destination, target, generation,
owner and proposal must match, and the approval must still be fresh. The receiving
CLI interprets these as an explicit local copy approval, not authentication of a
live remote control plane. It does not read remote state or infer target readiness.

These are argument templates, not ready-to-paste commands. Use actual reviewed IDs,
paths and digests. No command connects to a model, cloud account or SSH host.

## Evidence and filesystem behavior

The package contains `source.camol` (authenticated encryption of the proposal,
capture timestamp and Git bundle) and `receipt.json` (bounded metadata). Export
reads only the exact approved revision's reachable history, not unrelated refs,
untracked/ignored files, credentials stores, worker packets or the event database.
Old commits may contain sensitive content: encryption and explicit history review
are essential; a clean current tree is not a historical secret scan.

The existing bounded recovery decoder verifies objects in an isolated repository,
reconstructs raw tracked bytes without checkout filters/hooks, then checks the
selected commit, tree, executable bits and path-independent checkout digest.
Destination path replaces only the source identity's location field. No repository
remote or source Git configuration is copied. The source checkout remains untouched.
This version inherits the baseline policy's ordinary-file restriction; symlinks,
submodules, tracked credential-shaped paths, shallow/incomplete history and content
beyond the recovery ceilings are refused, not silently omitted.

Limits inherit recovery bounds: 16 MiB packed bundle, 64 MiB capsule, 8 MiB per
restored file, 32 MiB current-tree content, 10,000 files, 100,000 packed objects and
bounded Git subprocess time. They do not constitute a hostile Git-object sandbox
or a full bound on expanded ancestral history. Public packaging metadata can be
planned without the encryption extra; copy operations require it.

The receiver requires a new empty private destination and never overwrites an
existing project. A failed reconstruction can leave partial output for inspection;
it is never marked ready, automatically executed or silently deleted. Expiry is
checked before and after reconstruction. Already-dispatched filesystem writes
cannot be undone by a later expiry or target retirement.

## Controller records and retry

Successful exports append `SOURCE_HANDOFF_EXPORTED` with the exact proposal and
capsule receipt; replay/export preserves that record. `camol source-handoff inspect
--state-dir STATE --run-id RUN [--request-id ID]` reads retained metadata. The
encrypted package itself remains in the selected output directory; an ordinary
run ledger export does not implicitly include it or its key.

`Harness.propose_source_handoff(...)` and `Harness.export_source_handoff(...)`
provide the same owner-side workflow. Current owner/source/target validity is
rechecked during capture and before event append. Active embedded execution is
refused for synchronous export. The receiver function is
`camol.source_handoff.receive_source(...)`; it returns independently verified copy
metadata, never a controller-approved launch grant.

After a lost event append, retry can verify the existing authenticated package
and record the same capsule without re-encrypting or overwriting it. An already
recorded request returns historical metadata, even if a different output path is
supplied; it does not recreate a missing package or prove that the old output still
exists. A changed request body is refused. Corrupt/incomplete packages are retained
and never repaired implicitly. Receivers do not overwrite existing output on retry.

Remote transport, live revocation checks, actual target-side worker admission,
execution, accounting and result promotion remain open. The received-copy receipt
is deliberately not ingested as green readiness evidence.

After receiving a V2 task copy, `camol source-handoff doctor` can authenticate the
package and inspect that exact task/worker's local prerequisites without preparing
or launching it. The complete [handoff diagnostic scope](handoff-readiness.md)
explains expected evaluator binding, unchanged-source checks and why the resulting
observation still is not authenticated target admission.
