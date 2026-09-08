# Inspect readiness on a received task source

`camol source-handoff doctor` inspects one exact task/worker against an authenticated
received V2 source package. Run it on the machine holding that received checkout.
It is a read-only diagnostic, not a target enrollment, remote admission, isolated
workspace allocator, lease, or executor. It neither transmits the report nor records
anything in the controller ledger. A coherent probe result is not permission to run.

## Inputs and command

First use the owner-reviewed [task source handoff](source-handoff.md) flow to export
and receive the selected source. Retain the encrypted package, its exact proposal,
and its private key on the receiving side through an approved confidential channel.
Provide the full frozen runbook and the controller's expected evaluator digest;
do not derive a replacement expected digest from whatever happens to be installed
on the receiver just to obtain a green result.

The command accepts these required arguments (values are templates, not literal
ready-to-run paths or IDs):

```text
camol source-handoff doctor
  --proposal PROPOSAL.json --review-digest PROPOSAL_DIGEST --by OWNER
  --archive PACKAGE --key-file PRIVATE_KEY
  --runbook FROZEN_RUNBOOK.json --workspace RECEIVED_WORKSPACE
  --state-dir INTENDED_TARGET_STATE_DIRECTORY
  --target-id TARGET --generation GENERATION --agent-id WORKER
  --evaluator-digest EXPECTED_CONTROLLER_EVALUATOR_DIGEST
```

The worker must be an exact declared ID; the task comes from the V2 proposal. No
different worker may satisfy this selection. The intended target state directory
must be outside the source/package. If it does not exist, the report identifies
that missing preparation and does not create it. Neither the runbook's work commands
nor verification commands execute. Provider/version and other permitted built-in
read-only probes retain the existing guarded-runner restrictions.

The callable interface is `camol.handoff_doctor.inspect_handoff(...)` with the same
named inputs (`key` is the private 32-byte value, not a key filename). It performs
synchronous filesystem/subprocess reads; use a separate process outside an active
controller event loop. There is no background probe or model call on import.

## What gets checked

- Exact owner review, target ID/generation, fresh proposal and V2 task selection.
- Full normalized runbook/plan digest, selected task-contract digest and worker ID,
  before any runtime probes. The validated document is passed directly into the
  doctor, avoiding a second read of a possibly changed runbook path.
- Authentication of the encrypted package and its receipt, plus clean commit,
  tree, executable bits and working-byte identity at the reviewed destination.
  Source identity is checked before and after the probes; expiry is checked again
  afterward. A changed source or expired approval cannot return a green report.
- Existing doctor prerequisites for the selected pair, including runtime, source,
  filesystem, artifact sink, evaluator and applicable provider/network requirements.
  Other workers' runtime probes are not run. The evaluator still describes the
  full frozen plan; filtering subjects does not change its digest or weaken it.
- Observed evaluator digest versus the caller-supplied expected controller digest.
  A mismatch returns `EVALUATOR_CONFLICT` and exit 2 even if local probes are green.

## Result and trust boundary

JSON includes the source/proposal/selection/export/capsule identities, exact selected
task and worker, complete scoped doctor report, elapsed milliseconds, observation
and effective expiry, problems, and a digest of the redacted report. Nothing is
automatically saved. Exit 0 means only `selected_probes_coherent`; 2 means missing
readiness or evaluator mismatch, and 3 means a probe failed. Invalid input is refused
through the normal CLI error path. Repeating the read makes a new observation, not
a replay of cached green state.

The effective outer expiry is the earlier of source approval expiry and doctor
receipt expiry. Nested probe receipts cannot extend that source authority window.
The supplied expected evaluator digest is an operator input, not a new authenticated
controller attestation. The key authenticates the source package, not this machine
or the returned report. `target_authenticated`, `controller_state_checked`,
`execution_authority`, and `lease_authorized` remain false. `model_calls` is zero;
elapsed time measures this local diagnostic, not model usage or provider billing.

The source may be physically copied while its task, integration head, adoption or
capacity changes at the controller. No live revocation check is performed here.
Before launch, a future distributed-admission path must authenticate the actual
target, revalidate current controller state, prepare the isolated task workspace,
freeze exact authority, reserve capacity, issue a fence and enforce its lifetime.
This report is not silently accepted as any of those proofs.

## Scoped ordinary doctor

`camol doctor RUNBOOK --workspace SOURCE --state-dir STATE --task-id TASK
--agent-id WORKER` performs the same subject filtering without source handoff
authentication. Either selector is optional. Unknown IDs fail before probes;
an incapable selected worker produces no candidate, not a fallback. The full plan
and evaluator digests remain unchanged. Scoped reports use schema version 2 and
state `selected_subjects_only`; unfiltered reports retain their V1 structure.
Text rendering explicitly names the selected scope.

Embeddings use the new `DoctorOptions.task_id` and `.agent_id` fields. An optional
`document=` parameter to `run_doctor` accepts an already loaded runbook, validates
it, and avoids reopening `options.runbook`. Default file-based behavior is unchanged.
