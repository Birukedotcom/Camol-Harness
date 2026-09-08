# Usage and execution accounting

`camol.usage.usage_report(events)` is an embeddable, deterministic report over a
single run's event ledger. It contains no prompt, command, credential, or transcript
bodies. It reports token and provider cost totals and task, box, model, phase, tool,
command, and evaluator breakdowns. The source digest identifies the exact ledger
input.

Measurements distinguish three sources:

- `provider_observed`: validated provider token counts and billed microdollars;
- `worker_reported`: estimates returned by a generic process worker; and
- `unknown`: interrupted or malformed provider output with no trustworthy bill.

Unknown cost and tokens remain unknown. Budget accounting reserves the frozen
invocation ceiling while the report separately exposes that conservative charge;
reserved values are never called actual spending. Claude's cached input counts are
included once in total input and also broken out independently. Dollar values are
stored as integer microdollars without a mutable local pricing table.

Every new provider receipt is bound to run, task, worker, lease, turn, and invocation.
Identical receipt replay is deduplicated; conflicting measurements for the same
invocation fail closed. A successful agent turn records its usage invocation ID so
the turn and provider receipt cannot count the request twice. Failed provider calls,
overspend, wrong model resolution, malformed agent JSON, and transport failures
retain usage evidence even when no agent completion is accepted. Unverified
worker-supplied `model_usage` items cannot impersonate a provider bill.

`accounted_tokens(state)` and `provider_cost_used(state, task_id=None)` expose the
same accounting for scheduling. The first includes unconsumed/failed receipt charges
on top of recorded turn usage. The second returns microdollars, including unresolved
reservations. These are budget counters, distinct from observed measurement totals.

Duration is summed invocation time, not elapsed run time; parallel work can make the
sum greater than wall time. Tool requests/results count as one logical call when a
provider supplies correlation IDs. Missing per-tool timings remain null. Existing
ledgers without receipts remain readable and report their incomplete coverage;
Camol does not retroactively invent bills or timings.

## Shared hosted-worker admission

Claude and hosted Codex reserve against one run-wide balance before launching a
new process. Admission takes a short cross-process owner-state lock, reads the
existing immutable invocation intents/outcomes, merges their identities with
trusted event-ledger receipts, and atomically publishes the new intent. The lock
is released before execution; multiple boxes can overlap. A 30-cent run with a
pending 20-cent call can allocate at most 10 cents to the next call, regardless of
the stale balance a scheduler previously read.

Run/plan and per-task budget policies cannot disagree across hosted workers. A
known bill replaces its reservation, including actual provider overspend. An
unfinished intent retains its full ceiling across process restart; a terminal
unknown bill blocks new hosted launches. Cached successful results do not reserve
or launch again. Local Codex/OSS calls do not consume this hosted envelope.

The runner includes verified revision-ancestor ledgers and their journals. A
successor cannot discard an unreported ancestor charge or inherit a known-zero
cost from an uncertain outcome. Standalone adapter embedders must supply the
trusted `budget_baselines` for existing event history and complete revision
lineage; a model-supplied context packet is never a billing baseline. Reuse the
same persistent owner state directory; copying only a runbook is a new run, not
recovery of the old budget.

The scan is bounded (32 MiB of records across the selected lineage; 20,000 entries
per run) and refuses corrupt, linked, inconsistent or unavailable journals. A
known-exhausted allowance, unknown charge or busy accounting lock becomes visible
operator attention without launching a provider or consuming another provider
attempt. Pending intents must remain unresolved and reserve the exact allocated
ceiling; a factory cannot record a smaller reservation or pretend it is settled.

An allowance held by exact invocations still owned by this runner can now wait
within its current lease. The ledger records `PROVIDER_BUDGET_WAITING` with each
blocking run/task/worker/lease/turn and `PROVIDER_BUDGET_WAIT_CLEARED` before recheck.
The task stays `running` with a visible `runtime_wait=BUDGET_RESERVED`; overview,
box views and the switcher expose that substate. Existing heartbeats/readiness
renewal continue. No additional attempt, turn or payment intent is consumed by
the wait itself. Completion of a blocking invocation triggers fresh context,
readiness and atomic budget admission, not an assumption that settlement was green.

Only the current runner's in-memory ownership of those exact active invocations
permits this automatic wait. Persisted pending files, an old PID, a lease from
another run or a prior process lifetime are not live proof. Self-dependencies,
unowned pending charges and unknown terminal outcomes require reconciliation.
The active adapter must have published that exact invocation ID after reserving
its intent; matching only run/task/lease/turn after a restart is insufficient.
Cancellation clears the wait annotation, not the financial reservation. A resumed
task clears the previous annotation and rechecks; it does not inherit prior live
ownership. Busy-lock retry and unified reservation inspection remain separate gaps.
For V6 provider-rate capacity, the atomic budget denial carries the exact
pre-intent invocation binding. While the runner owns the blocking invocations,
it records `CAPACITY_CALL_DEFERRED`, binding the old rate debit, active budget
wait, task/worker/lease/fence/turn and a deterministic successor ID. The next
admission uses that new ID and reassesses supply and the rolling window. The old
debit is retained even if its window has not expired; this is deliberately
conservative and can cause an additional rate wait. A successful rate reservation
clears its capacity-wait annotation. This is a new admission, not a refund or an
extension of a receipt whose invocation might already have launched.

An expired ID without that recorded deferral still fails closed. The trusted
runner/adapter admission boundary supplies the no-intent evidence; this is not
an independent observation of a remote provider. A marker showing that an intent
already exists denies automatic deferral. Publication of the successor debit is
idempotent if the broker commits before the run event; a crash before the deferral
itself is durable does not invent the missing proof and may require reconciliation.
Local fixtures cover V4 wake-up and a V6 wait crossing the old rate window, not
live hosted-model behavior.
There is no automatic refund, unknown-charge reconciliation or journal deletion.
Retain the `packets` tree and budget lock with the run's recovery state.

These are allocation guarantees within Camol, not an account-level billing cap.
Claude's client stop can overrun, and hosted Codex has no hard dollar control.
Preflight and planning envelopes remain separate. Event-only usage reports do
not include an intent whose observation has not yet reached the event ledger;
the admission journal still charges that intent. A unified live reservation
inspection/reconciliation UI remains a separate implementation gate.

Finished debugger reproduction/guardrail receipts contribute command counts and a
separate `debugger_duration_ms` sum, deduplicated by case and execution identity.
Their derived test-result evidence does not count the same subprocess twice, and
local debugger execution is not provider spend. Shared-capacity call reservations
are reported as reservation counts, not bills or actual provider HTTP requests.

Linked plan revisions preserve cumulative source budget charges separately as
`inherited_accounted_tokens` and `inherited_accounted_cost_usd_micros`. The ancestor
ledger remains the detailed source; `inherited_unknown_usage` preserves its
uncertainty. A revision does not zero the budget or invent a known amount for an
unknown provider outcome.

Planning conversation usage is retained separately from worker-run ledgers in
owner-only per-project planning-call records. `/usage` reports planning and
`/usage run` inspects the selected durable run. The separation prevents model
planning spend from being silently presented as worker execution spend.

## Investigation and log sinks

`camol profile --db RUN_DB` validates/replays the ledger, then produces a
content-free investigation report: task cost hotspots, retries, typed wait counts,
completed-step and succeeded-task denominators, and builder/verification-to-acceptance
lifecycle envelopes. `camol.diagnostics.profile_run(events)` provides the same Python
interface. It never guesses a CPU or memory measurement, treats missing denominators
as null, flags clock regressions, and marks unfinished intervals as right-censored.
An envelope can include human waiting and integration. It is not exclusive compute
time or evidence that reducing a component would save that entire duration.

`camol logs --db RUN_DB --after SEQUENCE --limit 1000` streams bounded JSONL event
metadata for an external logger. Each record includes event/run/subject identities,
sequence, timestamp and the exact event digest, but no command arguments, prompt,
output or user prose. Save the last emitted sequence in your own sink and request
the next page; retrying a page is safe if the sink deduplicates by `(run_id, seq)`.
`camol.diagnostics.event_metadata(event)` is the host-side formatter. Event-store
`iter_events(..., limit=...)` uses a SQL-bounded cursor instead of materializing the
rest of a long run. No telemetry is uploaded or external logger configured for you.

For full authorized tool evidence, use `camol events`, `/box ... tools`, and the
content-addressed export. Metadata-only output is not an additional raw retention
channel. Planning ledger linkage, process CPU/memory sampling and provider-internal
HTTP timing remain explicit missing coverage; a successful model CLI invocation
does not expose every internal provider request.

Use profiles to select a concrete hypothesis, then compare fixed tasks/conditions
through the benchmark campaign. The profiler deliberately makes no automatic
savings, causation, quality or promotion claim.
