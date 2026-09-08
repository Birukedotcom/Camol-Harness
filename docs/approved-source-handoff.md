# Approved source handoff

Product proposal V3 approves the plan **and** a clean committed source identity:
canonical workspace, commit, tree, and raw tracked-byte digest. This is bounded
to ordinary tracked files; it is not a hermetic dependency/environment snapshot.
Ignored files and ambient tooling remain governed by separate readiness and
sandbox policies. Symlinks/submodules and credential-like source paths are not
supported by this proposal identity version.

`spawn_supervisor(..., expected_source=proposal['source'])` persists an immutable
per-run launch binding and passes its digest to the detached daemon. The daemon
starts isolated Python from the installed package, not a module or
`sitecustomize.py` supplied by the source checkout. It requires the exact launch
binding before approval and records `SOURCE_BASELINE_BOUND` in the authoritative
ledger. `PLAN_APPROVED` binds that event's canonical digest. Removing the sidecar
cannot erase the ledger identity or make the approval replay as unbound.

Runner entrypoints restore the sidecar from this ledger event, verify the raw
source identity, freeze evaluator assets from the approved revision, and pin
workspace creation to that revision (or an accepted integration descendant).
Admission and lease-refresh replay require the exact source-baseline probe;
materialization cannot follow a changed mutable HEAD. The source is checked
again before actual adapter launch, including after capacity waits. Drift pauses
work before leasing/spending against different code and requires a newly reviewed
proposal. Existing task worktrees remain retained for inspection.

Linked plan revisions explicitly carry the original approved source into a new
run-scoped binding and owner approval; accepted integration history remains the
successor's separately recorded execution baseline. Legacy direct CLI/API runs
without a source-bound proposal keep their existing semantics. Embedders wanting
this guarantee must call `Orchestrator.bind_source` before `approve_plan`, or pass
`expected_source` through the supervisor API. A source sidecar alone is not an
authority to retrofit an already approved legacy run.

## Persisted process ownership limit

Direct live child handles still support bounded cancellation. After a controller
restart, the legacy `ps lstart` process birth marker is only second-resolution;
it is not a non-reusable process handle. A matching live persisted invocation
therefore blocks resume and force-stop remains fail-closed: no signal and no
false `terminated_by_supervisor` journal entry. The owner must inspect/reconcile
that process scope externally before safe recovery. Camol does not infer an
unknown orphan's death from an unrelated PID reuse or silently release its work.
An absent leader is marked dead only when a read-only process-group existence
probe also finds no group. A surviving or permission-inaccessible group keeps
the invocation active. Deliberately detached descendants are outside this
legacy process-group proof; stronger containment needs an appropriate OS runtime.
