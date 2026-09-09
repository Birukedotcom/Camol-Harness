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

## Model-assisted delegation revisions

An existing source-bound, human-approved V5/V6 run can request a linked successor
proposal from the selected planning model:

```text
/delegate --propose --from REVIEWED_SEED.json --reason "Why work changes" --goal "Observable outcome"
```

The seed is an explicitly reviewed complete successor envelope, with a new run ID,
exact workers/profiles, command/evaluator authority and resource ceilings. It may
describe a different N-box topology from the parent, but the model cannot expand
the seed's authority. The existing proposal validator preserves commands, task
and step identities, evaluator assets, resource limits, prior invariant definitions
and obligations; every proposed task gate and final acceptance requires human
approval. Instructions/goals, additional acceptance conditions and dependencies
can be refined within that envelope. Added tasks must satisfy the existing bounded
seed command, attempt, evidence and resource rules. A model asking for more
authority receives no grant: it can return questions for the human instead.

Before invoking the planner, Camol checks the exact approved parent/source,
quiescence, absence of active/orphan invocations, distinct unused successor ID,
human/worker identity separation, and explicit prior-effect reuse policy. A live
owner, dirty source or invalid effect policy denies the request without a model
call. `--effects POLICY.json` supplies the same exact `reuse_confirmed` policy
used by handwritten revisions; the model cannot generate or approve that policy.

The one no-tools planning call sends the seed, goal, bounded dialogue, reason,
effect policy and parent-state metadata. Parent metadata names run/plan/product
digests, event cursor, state digest, integration head and up to 64 task status /
dependency / assignment / attempt rows. It is historical context, not readiness
or current tool-output evidence. No repository crawl or raw worker transcript is
implicitly sent. The complete prompt remains bounded to 12,000 characters; the
seed is at most 24,000 bytes and the model response at most 64,000 characters.
Planning has a 120-second request timeout but no hard token/cost/internal-request
cap. Those limits are disclosed before calling; unknown usage is not zero usage.

After parsing, Camol rechecks the exact source checkout and seed bytes, then the
parent plan, execution digest and event cursor under the owner lock. Any parent
advancement rejects the stale candidate, retains planning usage, and requires a
new explicit request. An accepted candidate is passed as data to the existing
revision service, which produces the full before/after impact review. The session
stays on its parent run with its original plan and approval. The proposal journal
links the planning call, request/response digests, parent context and exact review
digest; it is provenance, not authority to execute.

Use `/revise` to reopen the durable review without another model call. Only
`/revise apply REVIEW_DIGEST` adopts it, and `/run` is a separate action. New task
gates use `/gate TASK ASSESSMENT_DIGEST`; final acceptance remains `/accept` with
its exact outcome digest. Questions, rejected output and cancellation do not
publish or approve a successor. No uncertain model request is automatically retried.

This implements model-assisted **stopped-run** amendments. It does not implement
live lease reassignment, automatic authority expansion, selective carryover of
green gates, independent oracle adequacy proof or real provider acceptance.
