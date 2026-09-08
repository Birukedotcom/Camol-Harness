# Durable process-turn allocation

The process adapter now allocates a turn durably before dispatch. Previously a
process could modify its workspace, exit without a valid structured result, and
be launched again for the same turn. A real counter-writing process reproduced
two effects. Lost results are now explicit uncertainty, not permission to repeat.

This protects local process execution and supplies a necessary primitive for a
future target dispatcher. It is **not** distributed worker admission, transport,
source transfer or trusted remote result reduction.

## Identity and lifetime

Within the selected control-state directory, the allocation key is run/task/turn.
The immutable intent binds run, task, agent, lease and fence digest, turn number,
workspace, adapter/configuration and sandbox-policy digest, and actual frozen
packet bytes. A different lease or configuration cannot replace that turn's packet
or authorize another launch. A later normally accepted turn has a different number.

Before reading or preparing shared packet files, the adapter takes a nonblocking
exclusive process lock for that turn. The lock is held through result collection.
This prevents competing controllers from racing packet writes before the durable
intent exists. Lock files are private, owner-controlled, regular, and neither
symlinked nor multiply linked.
OS process death releases the lock but does not remove the intent.

The intent is published atomically and fsynced immediately before dispatch, after
the launch-authority callback. It contains hashes and identity, not argv text,
environment values or provider credentials. Intent readers reject foreign identities,
linked/nonprivate files, malformed records and oversized data. The configuration
digest is not an attestation of executable bytes or ambient environment: separate
readiness and runtime checks still apply.

An allocated intent alone proves neither process launch nor termination, task
success, provider spending or resolved external effects. A crash between allocation
and launch remains conservatively unknown. This is at-most-once allocation within
the retained ledger, **not exactly-once external execution** or cross-host fencing.

## Recovery behavior

- A valid, controller-collected cached result for the exact frozen packet can be
  reused without launching or invoking the launch-authority callback again.
- Missing or invalid results after allocation raise `ProcessTurnUncertain`.
  The runner records available command evidence, retains/salvages work, revokes the
  lease and exposes `EFFECT_UNKNOWN`; it does not treat this as ordinary automatic
  adapter retry. Native adapters retain their separate provider-invocation journals.
- Reopening the controller checks unresolved allocations before admission or a new
  lease. It preserves `EFFECT_UNKNOWN` without incrementing attempt counts, even
  though the previous in-memory pause set no longer exists.
- A launch-authority callback that fails or is cancelled before allocation can
  leave an immutable `process_turn_unlaunched` receipt. That proof permits a fresh
  authorization attempt, including a new lease, without claiming any process effect.
  If recording this proof fails, a later retry requires reconciliation.
- Legacy packets/invocations without a valid cached result or explicit unlaunched
  proof are retained for review. Corrupt packets/results are not deleted to make
  a retry possible. Valid historical cached results remain readable.
- Cancellation preserves cancellation and the intent. Sandboxed execution uses its
  existing bounded process cleanup; an intent is not proof all descendants died.
  The legacy unsandboxed adapter's containment limits are not changed here.

Inspect the task's waiting reason and command evidence through `/overview --attention`,
the box evidence/tool views, or the Python event API. Private allocation files live
under `packets/RUN/TASK/turn-NNN.process-intent.json`; pre-launch failure proofs use
the same prefix with packet-hash and `.unlaunched.json` suffixes. The process lock
file is not a completion receipt. Do not delete allocation files to force a rerun.

Reconcile retained work and any external effects before an owner-approved new run
or plan amendment authorizes further work. Restoring a trusted cached result needs
its exact subject and packet; an untrusted worker-output file is not a substitute.
No new automatic reconciliation, journal reset, teardown or operational state
restore command is introduced. Ordinary evidence export is not authority to resume
an unresolved invocation. Provider costs outside instrumented adapters remain
unknown; no missing structured result is treated as proof of zero spending.

## Verification scope

Tests exercise real process effects, two independent controller processes, different
leases/configurations, cached-result recovery/loss, corrupt legacy data, pre-launch
denial, cancellation, and a full admitted run followed by reopen. The fixture uses
ordinary local file effects, not provider accounts, cloud changes or remote workers.
Distributed dispatch still needs authenticated target-side admission, source and
artifact transfer, global lease/revocation enforcement and trusted result promotion.
