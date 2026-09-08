# Capability preflight accounting

A capability probe can consume account quota even when the client times out or
returns an unusable answer. Worker invocation accounting does not cover that
request. Camol now writes an exclusive, fsynced intent in the selected external
state directory **before** the model request and an immutable content-free
outcome afterward.

```text
camol provider-preflight --profile profiles/models/claude.json \
  --workspace /absolute/source --state-dir /absolute/state \
  --operation-id first-capability --max-usd-cents 10 --accept-spend

camol preflight-status --state-dir /absolute/state
```

The operation binds the exact profile, target, requested argv/prompt digest,
workspace and per-request dollar reservation. Omitting `--operation-id` selects
one deterministic initial operation for that profile/target/workspace; it is not
permission to spend repeatedly. Repeating a successful operation returns the
same still-fresh receipt without another model request. It does not extend the
receipt lifetime. A stale receipt needs a newly named, explicitly approved probe.
A known failed operation is never automatically repeated under the same ID.

Missing response/cost, timeout, interruption, or caller death leaves uncertain
spending. That holds **all new capability requests in the same state directory**,
including requests under different operation IDs. An old cached green receipt
does not bypass this hold during admission. There is deliberately no automatic
reset, deletion or unknown-to-zero conversion. Reconciliation needs a separate
owner-reviewed protocol; do not delete the ledger to make readiness green.

Known charges survive provider errors, wrong model identity and overspend.
Missing token measurements remain null, not zero. `preflight-status` reports
known probe charges separately from unresolved reservations and exits 2 when
held. It is read-only and does not create a missing state directory. It does not
claim to include worker, planning or unrelated account costs. This is still a
per-request probe ceiling, not a new combined worker-plus-planning budget.

## Execution and storage boundaries

- Local help inspection must report the supported all-tools-off controls before
  any model request. The probe passes empty tools, empty strict MCP configuration
  and empty setting sources; it uses a disposable neutral cwd, never source cwd.
- Workspace/state executables are rejected. The installed runtime is still
  trusted host software; flag introspection is not a security proof of its code.
- Native subprocess output is bounded to 1 MiB total, input to 4 KiB, with an
  absolute request deadline and owned-process-group cleanup on interrupted or
  oversized requests. Success is a direct CLI result, not proof that background
  helpers or remote compute ceased. Hostile descendants escaping with `setsid`
  are not claimed to be contained by this mechanism.
- The ledger retains operation IDs, digests, timestamps, finite usage and typed
  reasons. It never retains raw provider output, errors, prompts or credentials.
- `provider-preflights` and its lock/records must be private owner-only files.
  The selected state and its ancestry must be current-owner/root controlled;
  non-sticky group/world-writable parents are refused. Trusted sticky temporary
  directories are allowed. These are POSIX ownership/mode checks, not protection
  against the same account owner deliberately replacing or deleting its state.
  Symlinks, FIFOs, hardlinked records, unknown files and changed identities fail
  closed. Unknown temporary files after a filesystem crash are a hold, not
  automatically disposable content.
- The capability cache is derived only after the successful outcome is durable.
  A cache-publication failure can recover from that outcome without spending.
  A legacy non-private `provider-capabilities` directory is rejected rather than
  silently changing the owner's permissions.

All automated evidence uses injected results or small local subprocess fixtures.
No live account request or dollar-cap behavior has been validated in this pass.

The focused provider/host-admission/Claude-worker/conversation group passed 50
tests on Python 3.9 and 3.12 before the final null-subtype regression was added.
Independent review reproduced and then verified fixes for provider error markers
masquerading as success, conflicting/multiple model identities, partial cached
token counts, prior-green cache bypass of an unknown hold, and writable state
ancestry. Full integration and hosted CI are separate gates.
