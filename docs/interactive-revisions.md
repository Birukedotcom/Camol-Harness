# Stopped-owner interactive revisions

The `camol.revision_ui.RevisionUI` service bridges an existing interactive session
to the kernel's linked-plan revision API. It does not generate a plan, start a
supervisor, run a worker, probe a model, or spend a provider budget. Applying a
revision is a separate approval operation, not an implicit `/run`.

## Review and exact approval

The intended terminal entry points are `/revise --from RUNBOOK --reason TEXT`
with optional `--effects POLICY.json`, `/revise` to inspect the current review,
and `/revise apply REVIEW_DIGEST`. Command routing is kept separate from this
service so existing applications can embed it without a TUI. The service itself
already exposes the following synchronous methods:

```python
service = RevisionUI(session_store)
review = service.propose(
    session, successor_runbook, reason="Reviewed contract amendment",
    owner="human-owner", effect_reruns=[],
)
text = render_review(review)
review = service.inspect(session)
session = service.apply(session, review["review_digest"], owner="human-owner")
```

Public methods hold the existing `SessionStore.transaction()` lock. Controllers
already inside that transaction use the matching `_propose_locked`,
`_inspect_locked`, `_apply_locked`, and `_recover_locked` seams instead. This
avoids acquiring the same non-reentrant project lock twice.

Review binds the exact parent product plan, kernel source execution snapshot,
full source identity, proposed successor, effects policy, accepted integration
commit, inherited usage, owner, session and state directory. The text includes
the reason, all global/task contract changes, every successor command and policy,
invariants and evaluator gates, prior confirmed-effect reuse, inherited charges,
and the exact acknowledgment digest. Excessively large reviews are rejected,
not rendered with omitted authority. New providers or broader policy remain
visible in the full contract delta; applying does not prove their readiness.

Applying approves the exact linked successor and seals the parent atomically in
the kernel ledger. All successor tasks restart pending and require new readiness,
candidate/integration checks, and human gates required by the new contract. The
accepted integration commit is inherited as a code baseline; old green checks
are not inherited as evidence of success. Token and provider-cost accounting,
including unknown usage flags, remains linked to the source history.

## Stopped ownership is a real prerequisite

The service acquires the same process-scoped leader lock as `Harness` and the
supervisor. A live or drained supervisor that still owns that lock prevents a
revision. The explicit session run ID and exact product/kernel plan digests are
checked; an unrelated latest run in the database is never selected implicitly.
Only the existing human owner may apply. Registered worker identities are denied.

The kernel's quiescence check rejects live leases, unresolved gates, held/suspect
capacity, unknown/requested effects, unfinished watchers or debugger obligations,
and unaccepted provisional integrations. The UI also refuses any active durable
process invocation in this controller's packet directory. It never signals a
persisted PID or assumes that closing a terminal proved that work stopped.
Reconcile those conditions through the existing supervisor/owner workflows first.

Both proposal and apply recheck the full source binding. A different checkout,
modified tracked bytes, changed approved owner, or execution advance after review
invalidates the request without approving or running the successor.

## Product envelope and compatibility

Linked revisions use `camol.product_plan` **schema version 5**. Their origin is
`linked_plan_revision`, with `parent_product_digest`, `source_run_id`,
`source_plan_digest`, and `proposal_digest`. This is distinct from a model-created
candidate; the plan is already human-approved by the exact revision acknowledgment.
`validate_revision_envelope` validates this envelope without changing legacy
product-plan normalization or any V1–V6 kernel runbook digest.

The initial supported parent set is ProductV3/V4 and subsequent ProductV5, backed
by an exact durable `SOURCE_BASELINE_BOUND` event. ProductV1/V2 parents are refused.
In particular, the older imported ProductV2 stores only workspace and revision,
not the complete raw tracked-byte identity. The service does not fabricate a
source binding after an earlier approval. A separately designed explicit baseline
migration is needed for those legacy sessions.

After apply, the interactive session keeps its original session ID and state
directory, adopts the successor product/run digests and objective, and resets the
selected box and event cursor. The result is ready for a separate explicit launch
with the normal current provider policy, readiness and spend acknowledgments.

## Crash-safe handoff

Before the kernel mutation, a private immutable review and a parent-scoped pending
pointer are persisted under the interactive project's `revision-reviews/` directory.
The records contain no raw credential values; secret-shaped content is rejected.
The minimal successor session is size-checked before the parent can be sealed.

If a failure occurs before the atomic kernel revision, the parent remains current.
If the kernel commits but writing the interactive session fails, the old session
and pending review remain. The owner can perform a handoff without another apply:

```python
session = service.recover(old_session, owner="human-owner")
```

Recovery verifies the exact retained review, parent supersession event, successor
revision/source/owner binding, and complete kernel lineage. It never replays an
apply or starts execution. Repeating `apply` with the old session after the first
session handoff is rejected as stale. Missing or changed pending records require
owner attention, not invented lineage or automatic reapproval. An externally
advanced successor is reported with its actual lifecycle; a successor itself
already superseded requires explicit recovery of that next reviewed lineage.

The existing owner-private state directory is the filesystem trust boundary.
These records are not designed to resist an administrator or hostile process with
the same operating-system identity rewriting every ledger and approval artifact.

The service's 12 tests passed on Python 3.12 (45.917 seconds) and Python 3.9
(57.966 seconds), including real kernel revision, inherited integration, lineage
export and crash recovery. This is service-level evidence. Terminal command and
ProductV5 routing integration remain required before claiming `/revise` usable.
