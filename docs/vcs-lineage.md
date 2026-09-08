# Candidate relationships and verification impact

This implements the recorded-candidate portion of SPEC §19.6, separate from the
static repository dependency graph. It uses the existing run ledger, not a second
source of execution truth, and works without a terminal multiplexer.

## Inspection

`/vcs` displays captured candidates, task/head identities, accepted integration
counts and owner-declared relationships. `/vcs 50` advances the offset. Each page
contains at most 50 candidates and 50 relationships. It requires an existing exact
run, returns focus to the orchestrator and makes no model call or live Git probe.

`camol vcs inspect` returns the complete bounded JSON snapshot. All VCS CLI
operations require `--state-dir STATE --run-id RUN`: your existing absolute run
state directory and exact run ID. Optional `--db DATABASE` must name an existing
database inside that directory; the default is `camol.sqlite3`. Reads share the
exact-run inspector's default ceilings: 20,000 events, 2 MiB per event and 64 MiB
total. Missing, corrupt or unsafe state fails without creating a replacement.
Snapshots permit at most 10,000 candidates and 20,000 relationship changes.

Nodes retain candidate/evaluator/salvage digests, task/box/lease and workspace
identity, source base, captured HEAD, and commit IDs directly named by capture or
integration receipts. Accepted integrations retain branch, repository identity,
revision, checks and receipt digests. This is not a complete commit ancestry crawl.

`captured_content_digest` binds the base-to-workspace patch, captured base/HEAD and
untracked-file digest inventory. That patch is **not** necessarily a HEAD-to-dirty
working-tree diff. `git_status_digest` is therefore null. Push receipt, PR and
remote review fields are explicitly unobserved, never inferred from local green
tests. Captured files, prompts and tool output are not included.

## Owner-reviewed relationships

| Relationship, source → target | Meaning |
| --- | --- |
| `depends_on` | Source relies on target |
| `absorbs` | Source incorporates target's work |
| `backports` | Source adapts target for another context |
| `deploys` | Source represents work deploying target |
| `supersedes` | Source replaces target as an owner declaration |
| `abandons` | Source records a decision to abandon target |
| `conflicts_with` | Symmetric conflict; canonical endpoint order |

Endpoints must be distinct exact captured candidate IDs from this run, not pane
indices, task labels, branches or prefixes. Consumption edges (`depends_on`,
`absorbs`, `backports`, `deploys`) must remain acyclic. The other edges express
review decisions, not execution dependencies. No label changes code, integration
selection, task dependencies, budgets, gates, worker leases or plan approval.

`camol vcs propose --source CANDIDATE --target CANDIDATE --relation RELATION
--reason TEXT` produces JSON without appending. Include the common state/run
options. Save the output to a private proposal file and review it. The proposal
binds run, plan, graph/event cursor, endpoints, action, reason and human owner.
`--action remove` proposes removal of an existing link, retaining its history.

Apply with `camol vcs apply --workspace REPOSITORY --proposal FILE --by OWNER
--review-digest DIGEST`, plus common state/run options. The repository is the
source Git checkout associated with this run; the digest is the exact value in
the reviewed proposal. The CLI acquires the execution leader lock. Stop/drain an
active supervisor first. New changes require no active task leases. The kernel
checks the exact approved human owner and compares event sequence before append.
Stale snapshots, unknown fields, changed bodies, foreign IDs and cycles are refused.

Exact retries return the recorded proposal without another event. Retrying an old
add cannot resurrect a subsequently removed link; inspect the current graph for
current state. Workers cannot author these events. This uses Camol's local owner
authorization, not a new remote identity authentication protocol.

## Prospective impact, not automatic execution

`camol vcs impact --candidate ID --change rebase` computes a conservative rerun set.
Repeat `--candidate` for multiple seeds. Supported changes: `merge`, `rebase`,
`fold`, `environment`, `plan`. Iterative traversal combines candidate consumers
with task dependents until stable, including other candidates of affected tasks.
Environment/plan changes conservatively affect every task. Results list candidate
and integration phases, frozen verification purposes, acceptance requirements,
required evidence, and related conflict/replacement decisions requiring review.

This is hypothetical: it does not prove Git changed, rewrite historical green
records, or start tests. Execute changed work through the reviewed successor-plan
workflow, whose kernel currently reverifies **all** destination tasks. This query
does not relax that policy. Existing final-acceptance digests remain code/evidence
bound; descriptive relationships do not create or revoke historical acceptance.

## Embedding and recovery

An open `Harness` exposes `vcs_snapshot()`, `propose_vcs_relation(...)`,
`apply_vcs_relation(proposal, by=..., review_digest=...)` and
`vcs_impact(candidates=[...], change=...)`. Apply refuses while its runner is active.
`Orchestrator` exposes snapshot/proposal/apply with explicit run ID and optimistic
concurrency. Pure `camol.vcs.snapshot(state)` and `impact(state, ...)` require
replay-validated state; they do not validate arbitrary external Git reports.

`VCS_RELATION_CHANGED` events replay and survive ordinary run export. Removal is
a new event, not erased history. Sealed predecessor relationships remain available
through revision lineage; they are not implicitly transferred to a successor.
Recorded state requires the same owner-controlled storage as the rest of Camol.

## Remaining §19.6 scope

Remote push/readback receipts, provider PR/check/review observations, cross-run or
cross-repository objects, live change detection and workflow-specific Git mutations
remain open. These relationships are owner declarations, not independent ancestry
or deployment proof. This layer is not completion of the full VCS specification
or distributed build execution.
