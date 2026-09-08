# Reviewed single-branch publication

An owner can now publish an exact accepted integration from a completed run.
This is a Git effect, separate from remote observation and candidate evaluation.
It requires its own exact proposal approval and durable intent. A push does not
create a PR, interpret repository approval rules, deploy software or grant a worker
more authority. Active or incompletely accepted runs cannot publish through this path.

## Review and execute

First inspect `camol vcs inspect --state-dir STATE --run-id RUN` and choose the
exact candidate/integration IDs. Prepare a target JSON object with exactly:

```json
{"kind":"github_https","repository":"OWNER/REPO","branch":"build/result"}
```

The other supported kind is `local_bare`, whose repository is an absolute,
non-symlinked path to a bare Git repository. SSH and arbitrary URL helpers are not
accepted. Neither transport discovers accounts or logs in.
GitHub owner/repository case aliases normalize to one identity. Local proposals
also record directory device/inode; replay does not re-stat the filesystem, but
execution checks the reviewed identity before observation, dispatch and confirmation.
Those checks detect replacement at those boundaries, not an adversarial same-user
directory swap during the Git syscall window.

`camol vcs propose-push` takes the common state/run options, `--candidate`,
`--integration`, `--target FILE`, `--request-id ID`, `--expires-at TIMESTAMP`, and
either `--expected-old EXACT_SHA` or `--create-branch`. The proposal is read-only;
it does not probe the destination or infer an expected ref. Its approval window
is at most one hour and its total operation timeout is 1–60 seconds (default 30).

Review and save the proposal, including the destination, exact commit, expected
old ref, and disclosure that reachable commit history will be transferred. Execute
with `camol vcs push --workspace SOURCE --proposal FILE --by OWNER --review-digest
DIGEST --allow-write`, plus common state/run arguments. GitHub additionally requires
`--allow-network`; `--token-env NAME` selects one explicit credential variable if
needed. Placeholders above must be replaced with the actual reviewed identities.

The CLI takes the exclusive controller lock; stop the supervisor first. The
embedding equivalents are `Harness.propose_vcs_push(...)` and
`Harness.push_vcs(proposal, ...)`. Publication refuses active embedded execution.
`camol vcs push-status --request-id ID`, plus common arguments, reads the retained
operation without taking an execution lock or contacting the destination.

## Publication boundary

The backend builds a private object-only repository from the exact integrated
revision. It transfers only that revision's reachable history, not other source
branches, untracked files or current dirty content. It never checks out source
files, invokes source hooks/filters, uses source remote names or credential helpers,
or rewrites source refs/configuration. Git uses a trusted system binary, fixed
protocols, disabled prompts/redirects, bounded output and an operation deadline.
Packed source data is capped at 16 MiB. This is not a hostile Git-object sandbox
or a bound on all expanded ancestral history; larger repositories need a future
streaming/resource-policy implementation.

An existing destination must equal the reviewed old SHA; that commit must be an
ancestor of the integrated revision in the isolated object database. The actual
single-ref push uses an explicit expected-ref lease so a competing writer cannot
silently change the reviewed base. Although Git calls that option
`--force-with-lease`, the harness separately rejects non-fast-forward ancestry;
there is no user option for history rewriting, deletion, mirror-push or tag-push.
Receiver-side hooks and hosting policy belong to the destination, not Camol's
source-hook prohibition.

No token enters a proposal, ledger, argv, repository configuration or retained
child output. An explicitly supplied GitHub credential exists transiently in the
owned Git child's environment as an HTTP authorization header. This is not secret
isolation from another process with the same user/host privileges.

## Outcomes and recovery

- `confirmed`: a push command exited successfully and exact branch readback matched.
- `already_present`: the destination already held the reviewed commit; no push issued.
- `not_dispatched`: preparation, approval, cancellation or expected-ref checks stopped
  before a push attempt. Any local Git preparation/remote read may still have occurred.
- `effect_unknown`: a push was attempted but its complete outcome was not confirmed.

Receipts retain elapsed time, bounded status, exact observed refs and exit code,
not raw command output. A missing final ledger append leaves the intent pending.
Exact retries return that historical record, never reissue a push. A pending or
unknown operation also blocks a new request ID to the same exact destination.
An explicit owner acknowledgment can lift that block for a separately reviewed
new push, as described below. Automatic reconciliation/reset remains absent:
independent remote reads do not retroactively establish push authorship.

Events replay/export with the run. V3 VCS snapshots expose operation history and
the latest outcome, including uncertainty, without changing task gates, usage,
integration receipts or final acceptance. CLI success is 0 only for confirmed or
already-present outcomes; other outcomes return 2.

This implements bounded publication, not full SPEC §19.6: cross-run relationships,
PR creation, ruleset evaluation and live hosted-provider acceptance remain open.

## Owner acknowledgment of uncertain publication

After inspecting the destination and any receiver-side effects, the owner can
choose to permit another exact review without pretending the previous operation
was confirmed or had no effects. This is a human policy decision, not an automated
remote observation, proof of process quiescence, or proof a receiver hook ran once.

Use `camol vcs propose-push-ack --request-id ID --reason TEXT --expires-at TIMESTAMP`,
with the normal state/run arguments, to prepare the decision. It binds the exact
uncertain operation record, target identity, run/plan/owner and rationale. The
approval window is at most one hour. Review the complete JSON and save it, then use
`camol vcs acknowledge-push --workspace SOURCE --proposal FILE --by OWNER
--review-digest DIGEST`, plus the same state/run arguments. Both commands make no
Git or network request. Applying takes the existing exclusive controller lock.

The embedding API exposes `Harness.propose_vcs_push_acknowledgment(...)` and
`Harness.acknowledge_vcs_push(proposal, by=..., review_digest=...)`. Active embedded
execution is refused. `push-status` and V4 `/vcs` snapshots show the separate
acknowledgment while preserving the original missing or unknown receipt. No gate,
usage total or integration outcome becomes green through this decision.

A changed pending result invalidates an unapproved old acknowledgment proposal.
Wrong owners, changed bodies, foreign identities, expired/future proposals and
attempts to claim remote confirmation or automatic retry are rejected. Exact
retries of an already recorded acknowledgment return its historical decision
without another event, even after its original approval window expires.

Acknowledgment does **not** resend the old request. A new request ID still needs
its own proposal, exact review digest, write/network permission, expected-ref check
and non-fast-forward protection. A concurrent old local invocation that has not
yet crossed the dispatch boundary is denied by its next authority check after
acknowledgment. Already-dispatched remote work cannot be revoked by this metadata
change and may still finish; receiver-side effects must inform the owner's decision.
If an authentic old final result arrives later, it remains a separate receipt, not
something inferred from acknowledgment. Reconciliation across runs or other kinds
of external effects remains outside this narrow publication workflow.
