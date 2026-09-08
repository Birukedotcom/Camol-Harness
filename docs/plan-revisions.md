# Frozen plan revisions

Plans are immutable. Camol changes an approved plan by creating a linked successor
run, not by editing a live state graph or resetting old events. The initial
implementation deliberately supports one migration strategy: `reverify_all`.

The owner reviews a concrete normalized schema-5 successor and an exact proposal
digest. The proposal shows task additions/removals/changes, invalidated transitive
dependents, global-contract changes, the source integration revision, preserved
artifact identities, cumulative usage charges, and external-effect reuse policy.
A global contract change invalidates every task. Even unchanged tasks restart
pending in this version: no old step completion, green evaluator result, human
gate approval, reservation, or authority grant crosses into the successor.

## Preconditions and atomicity

Applying a revision requires the source to be approved and quiescent: no active
task lease, pending human task gate, unreleased reservation, unsettled provisional
integration, unresolved external-effect intent, pending watcher, unresolved debug
obligation, or authorized/running debug execution. A proposal
may be discussed while work drains, but any subsequent execution change makes
its source digest stale and requires a new proposal. The control plane must also
check that the inherited Git commit exists in the source repository. The runner
materializes isolated worktrees and freezes new evaluators from that immutable
revision; it never edits the original checkout.

The source is sealed with `RUN_SUPERSEDED` in the same SQLite transaction that
creates, links, and approves the successor. Both streams use compare-and-swap
sequence checks. Failure rolls the entire transaction back. A sealed source
rejects further writes and execution. Its evidence and spend remain intact.
The successor has its own run ID, leases, worktrees, evaluations, and final human
acceptance. Source integration code is a baseline, never a proof that new work
is correct.

The original human owner's identity must approve the exact proposal. A worker ID
cannot self-approve. These methods belong to the owner-only control plane; an
owner-name string is not an authentication mechanism for an untrusted remote
caller. An embedding service must authenticate its callers before exposing any
approval API, and workers must not receive the event-store/control capability.

## External effects

Revision is not permission to replay a deployment, payment, or other mutation.
Every retained task with a previously confirmed effect needs an explicit
per-effect `reuse_confirmed` entry containing the exact request digest and
provider readback digest. The successor can return that prior result for the
same provider, operation, target, request, and idempotency key; it does not gain
authority to invoke the remote mutation again. Changing the request or renaming
a task to bypass the existing effect identity is denied. Unknown/requested
effects must be reconciled first. Other rerun strategies are unsupported and
fail closed; a generic assertion that an operation is idempotent is insufficient.

## Embedding contract

The core methods are:

```python
proposal = orchestrator.propose_revision(
    source_run_id, normalized_schema5_runbook, "Reason for amendment",
    effect_reruns=[],
)
# Present the complete proposal to the authenticated human owner first.
successor = orchestrator.apply_revision(
    source_run_id, proposal["proposal_digest"], approved_by=human_owner,
)
```

Public `Harness`/CLI wrappers additionally own the leader lock, Git validation,
and selection of the successor. This core API does not start a provider call.
The successor's inherited token and provider-cost charges still count toward its
frozen total budgets. Unknown source usage retains its conservative charge and
an explicit unknown marker; a revision cannot create a free budget reset.

## Portable lineage

`collect_revision_lineage(store, run_id)` gathers every ancestor. Pass that mapping
as `lineage_events` when exporting through `RunArchive.export`. Revision exports
use archive schema 2 with each complete source ledger and all referenced blobs.
Verification replays each source, verifies its seal and exact successor binding,
and checks the whole hash-linked chain. Export of a successor without its source
chain fails. Ordinary non-revision archives remain schema 1.

The migration is intentionally conservative: no automatic selective task-state
carryover, no revised external-effect replay, and no reclassification of old
evidence under new invariants. Those require additional explicit protocols.
