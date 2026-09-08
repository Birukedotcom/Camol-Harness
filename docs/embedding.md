# Embedding Camol in Python

`Harness` uses the same approved runbooks, workspaces, readiness checks, adapters,
evaluator and durable event stream as the terminal client. The terminal is optional.

```python
from pathlib import Path
from camol import Harness

with Harness(Path("/absolute/project"), Path("/absolute/camol-run-state")) as run:
    proposed = run.prepare(Path("/absolute/runbook.json"))
    # Present proposed to the human. Pass their actual approval and exact digest.
    run.approve(by="project-owner", digest=proposed["plan_digest"])
    result = run.run()  # In an async application: await run.run_async()
    print(run.usage())
    print(run.events(after_seq=0))
```

Construction and preparation do not execute agents. An unapproved plan cannot run.
State lives outside the repository. Use a different state directory for each run;
reopening an existing directory resumes the same plan. The embedded owner and the
detached supervisor share a leader lock, so a second writer cannot take over.

`run.pause()` requests a drain at the next task boundary. Cancelling `run_async()`
waits for runner cancellation, salvages active work and revokes its leases. The
caller should await cancellation before closing the context manager. A waiting run
returns a typed state so the host application can arrange a wakeup. A completed run
reopens without executing its tasks again.

Events use monotonic sequence cursors. Consumers can persist the last sequence they
processed and use `run.events(after_seq=cursor)` after reconnecting. Their delivery
logic remains separate from the authoritative execution loop.

Use `run.debugger` to open and inspect debug cases and drive the full experiment
protocol documented in `debugger-protocol.md`. `run.export(destination)` writes a
portable ledger and artifact archive for replay verification.

Schema V5 plans return `awaiting_acceptance` after their task and integration gates
pass. Show the outcome to the human, then call
`run.accept(by="project-owner", outcome_digest=...)` with that exact approval.
For an individual critical gate, use `run.approve_gate(task_id, by=...,
assessment_digest=...)` and resume execution. Approval of the wrong digest never
changes the ledger. Earlier runbook versions keep their original semantics.

Read-only tools can use `ReadOnlyEventStore` from `camol.store` while the owner runs.
It refuses a missing database and cannot append. CLI equivalents are:

```text
camol events --db /absolute/camol-run-state/camol.sqlite3
camol usage --db /absolute/camol-run-state/camol.sqlite3
camol debug --db /absolute/camol-run-state/camol.sqlite3
camol watchers --db /absolute/camol-run-state/camol.sqlite3
```

Usage separates provider observations, worker-reported estimates, and unknown
consumption. Unknown requests reserve their bounded allowance for budget decisions;
the report never describes that allowance as a measured bill. Invocation duration
is summed work time and may exceed elapsed wall time when boxes run concurrently.

## Observation plugins

`run.watch(spec, approved_by=...)` freezes a `WatchSpec`. A resumed host uses
`run.watcher(watcher_id)`. Call `await watcher.poll(fetch, observer_receipt)` with an
async, read-only source callback accepting `(query, cursor)` and returning
`{"observations": [...], "next_cursor": ...}`. The receipt must match the pinned
source schema, parser, fixture, expected event classes and correlation keys and be
fresh before and after polling. The host owns source authentication and execution;
a receipt supplied by an untrusted worker is not authenticated evidence.

The ledger commits an accepted batch and cursor atomically, suppresses duplicate
source event revisions, and refuses concurrent stale cursor writes. Empty polls,
timeouts, unknown schemas and source failures remain `waiting`. Only a terminal
event for the exact approved correlation filter completes the watch. Conflicting
same-revision content produces `conflict` and preserves both its audit event and
the last accepted cursor. A completed watch can be explicitly reopened by its
owner; a duplicate old terminal event cannot complete the reopened watch.

For daemon-compatible scheduled observation, use `run.observers()` to configure an
exact owner-approved `camol.watch_schedule`, then await `tick()` or
`run_until_settled()`. The standard source is a bounded local normalized JSONL
journal. See [durable-observers.md](durable-observers.md) for source identity,
poll budgets, plugin registration, expiry, restart and cancellation requirements.
Manual `Watcher.poll` cannot bypass an existing schedule's active-attempt authority.

Source callbacks must provide non-secret cursor handles. Camol refuses metadata
that redaction would alter instead of silently corrupting its resume position.
Only hashes and identity metadata enter the observation ledger. Sensitive raw
content needs a separately approved, retention-scoped artifact channel.

This API is host-scheduled. A callback is Python code, not a persisted process;
after restart the embedding application re-registers the source and calls `poll`
again. Built-in GCP/voice source plugins and daemon scheduling are separate work.
