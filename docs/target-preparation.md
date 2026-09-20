# Target-local box preparation

`camol target-prepare plan|apply|inspect` adds an explicit filesystem preparation
step after an approved V2 task source handoff. It is also importable as
`camol.target_preparation.propose`, async `prepare`, and noncreating `inspect`.
It is not a remote worker service or permission to launch a model.

## Review and effects

`plan` takes the source proposal, exact authenticated export receipt digest,
full frozen runbook, selected compatible agent, expected controller evaluator
digest, canonical private state directory, request ID, owner and expiry. It emits
JSON without creating state. Its approval window must fit inside the source
handoff window. The proposal explicitly authorizes only:

- an isolated worktree under the named state directory;
- private preparation records and candidate readiness evidence;
- worktree metadata changes inside the received standalone repository's `.git`.

The received tracked source and original controller checkout remain unchanged.
An existing canonical parent directory is required. The state destination must
be new or empty and private; linked roots and nonempty directories are rejected.
A received linked worktree with an external Git common directory is not eligible.

Use `camol target-prepare plan --help` for the required fields, review the emitted
JSON, then pass that file and its exact digest to `apply --proposal FILE
--review-digest DIGEST --by OWNER --archive PACKAGE --key-file KEY`.
The key authenticates the source package, not the receiving machine. The expected
evaluator is caller-supplied, not a fresh controller attestation.

## Lifecycle and recovery

An exclusive, fsynced intent precedes worktree changes. Preparation uses the
existing tracked admission child, with its 60-second timeout and process cleanup.
It rechecks source identity and approval expiry after the child returns. Inspect
with `camol target-prepare inspect --state-dir DIR`.

| Retained state | Meaning |
| --- | --- |
| `prepared` | A candidate bundle was assembled; its probes may still be red |
| `failed` | Preparation failed; partial files remain for inspection |
| `cancelled` | Cancellation was recorded; partial files remain |
| `PREPARATION_UNKNOWN` | Intent exists without a result; never automatically repeat |

Repeating the exact reviewed apply returns its historical result, without a new
child, re-probe, cleanup, or renewal. Missing result publication is not a reason
to redispatch. `apply` exits 0 for an assembled candidate, 2 for retained failure
or cancellation; 0 does **not** mean global readiness. `inspect` is historical
metadata, not current filesystem verification. No cleanup or deletion is implicit.

## Authority boundaries

The result explicitly sets execution authority, controller admission, target
authentication, global capacity reservation and worker start to false. Its bundle
contains target-local candidate grant/reservation records; these are not a
reservation in the authoritative controller ledger. The relocated source proof
is intentionally rejected by the original controller's source-admission check.

Still required: authenticated distributed admission bound to current controller
state, global capacity/fencing, dispatch, target worker execution, result delivery
and integration. Preparation is not completion of that pipeline. All processes
here are trusted local control-plane helpers, not an OS security boundary against
a hostile same-user process replacing files or modifying the installed package.
Read-only probes can execute explicitly permitted installed version commands;
workspace/state executables are refused. Git worktree and view plumbing uses
system-path Git, not repository-supplied PATH entries.

Pane layout and box selection remain separate from these effects; see
[pane orchestration](pane-orchestration.md). Opening a tile never prepares or
launches work.

## Verification checkpoint

The final focused group covers target preparation, workspace binary selection,
workspaces, Git inspection views, admission, child liveness, scoped receiver
diagnostics and the probe registry: **81 passed on Python 3.12 (98.448 seconds)**
and **81 passed on Python 3.9 (98.007 seconds)**. Real child CLI preparation and
inspection are included; no native account or paid inference was exercised.

Adversarial regressions first proved that ambient PATH could execute a source
Git shim during WorkspaceManager construction, and that admission's probe guard
omitted the original source checkout when inspecting a managed worktree. Both
now have passing trap tests. Git view plumbing also uses system-path Git. Early
test iterations corrected an export-record fixture lookup and moved a misplaced
trap into the actual protected source; a script outside the declared protected
roots is not covered by the workspace execution prohibition.

Cancellation, expiry after the real child completes, evaluator mismatch, source
mutation before preparation, wrong owner/key/export, nonempty/symlink roots,
lost-result recovery and original-controller rejection are tested. The result is
not a claim of hostile-host isolation or distributed execution acceptance.

The wheel and source distribution build successfully. A fresh core-only wheel
installation has 122 byte-identical Python modules and boots the preparation CLI
without Textual, MCP or cryptography. Optional cryptography is installed only in
the temporary test environment for source package tests. The same installed
regression group passed **81 tests in 96.822 seconds**, importing Camol from the
wheel outside the checkout (including the real CLI child).

The separate full Python 3.9 run on frozen predecessor `1d6cb8c` was still in
progress at this checkpoint. It does not cover these new changes. Prior completed
full Python 3.12 results on `0e5da822` were 1,295 passed in 1,363.502 seconds;
those results likewise are not a whole-suite pass for this checkpoint.
