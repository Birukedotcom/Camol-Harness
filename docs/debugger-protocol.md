# Debugger protocol

The debugger is a state machine for restoring or establishing intended behavior. It
is not a persona and should not depend on a worker remembering a skill.

## Required declaration

Every debug case begins with four explicit fields:

1. `observedBehavior` — what the system did, without causal speculation;
2. `targetBehavior` — what it should do, cited to business truth, a prior known-good
   case, or an owner-approved future behavior;
3. `reproduction` — the smallest known procedure that can expose the gap;
4. `requiredEvidenceKinds` — what must be present before the harness can say the case
   is verified.

“Make it work” is not a target behavior. “The worker registered” is not proof that a
real call, request, or user flow works.

## States

```text
OPEN
  -> REPRODUCED
  -> LOCALIZED
  -> EXPERIMENTING
  -> VERIFIED
  -> EVAL_PROMOTED

Any nonterminal state may move to BLOCKED with a typed missing dependency.
A contradicted verification returns to EXPERIMENTING; it does not erase history.
```

The versioned debugger enforces every transition above during ingestion and
replay. An executed red-before outcome must identify the declared target. The
localization chain, bounded experiment, candidate/environment identity, independent
green-after verification, guardrails, and owner-reviewed eval definition are
separate records. A worker-reported observation cannot satisfy verification.

The embedding API is `Harness.debugger` (or `Debugger(orchestrator, run_id)`).
Methods are `open`, `observe`, `reproduce`, `localize`, `experiment`,
`finish_experiment`, `verify`, `contradict`, `block`, `resume`, and `promote`.
`observe` defaults to `HUMAN_REPORTED`; requesting an executed/observed label
without a kernel receipt stores `UNVERIFIED` instead. Producer strings and
caller-supplied result metadata cannot establish a reproduction or promote an eval.
Do not expose the trusted host API directly to a worker.

### Executable reproductions

`camol.debug_execution.reproduction_contract(...)` assembles a reviewable
`camol.debug_reproduction` v1 record. It does not execute or approve it. The owner
must explicitly map exactly one command to the target oracle, and any additional
commands to named guardrails. Each declares its exact argv, workspace-relative cwd,
timeout ceiling and accepted exit codes. There is no inferred shell command,
unreviewed generic judge, or claim that an arbitrary green command proves prose.

The frozen record binds run/case/plan/target, the original source identity, exact
environment values, executable hashes, and the existing `SandboxPolicy`. Call
`freeze_reproduction(case_id, contract, approved_by=owner)` after review. Freeze
is immutable: changing the oracle or baseline requires another case. Credentials
and remote network authority are not supported by this local reproduction version.
The `developer_trusted` backend remains visibly **unsandboxed**; the executor does
not pretend its deny-network declaration is OS enforcement. Other trust tiers
require the existing enforcing backend or fail before launch.

Hardened measurement contracts must make the entire source checkout explicitly
read-only (`SandboxPolicy` v2 `readonly_paths`) and grant no overlapping write root.
Output writes may target only a separately reviewed scratch directory outside the
source and controller state. This prevents transient oracle mutation followed by
restoration, which before/after hashes alone cannot detect. The evidence records
`source_isolation=read_only_source_enforced` for this enforcing path. Unrestricted
developer execution records `hash_checks_only`; a green exit under that mode is
not a claim that a hostile process could not tamper with its own measurement.
The frozen executable likewise cannot be inside a hardened child write grant.

`evaluator_assets` explicitly pins the oracle's workspace files by relative path and
content hash; checks run before each command and after execution. Inline-only
oracles declare `[]`. Review must cover the complete relevant oracle dependency
closure: unchanged argv alone cannot prove that an imported test module or helper
was not changed. Downloading or importing unknown oracle code is not implicit.

Source identity comes from `source_identity(workspace)`: a clean committed Git
revision/tree plus directly hashed tracked-file bytes. Source and executable hashes
are rechecked before execution; source is checked again afterward. Source inventory
does not run `git status`, repository filters, hooks, or imports. Symlinks, submodules,
credential-shaped tracked paths, oversized files/inventories and uncommitted source
are refused for this first version. The identity is not a hermetic image: ignored
files, OS libraries and external services are not attested. Use a dedicated sanitized
fixture with explicitly pinned dependencies when that stronger fidelity is required.

Authorize one invocation with
`authorize_execution(case_id, execution_id, source, approved_by=owner,
experiment_id=None)`, then `await execute(case_id, execution_id, ArtifactStore(state_dir))`.
The returned evidence IDs follow the frozen command order. The original red run
uses `experiment_id=None` and must match the frozen baseline. Candidate execution
must name the current bounded experiment, whose `candidate_digest` is
`canonical_digest(source_identity(workspace))` and whose `environment_digest` is
`environment_digest(contract)`. Guardrail names must match the frozen command set.
No authority is inferred from a worker's proposed experiment.

Every consumed authorization is recorded before spawn and is never automatically
retried. Commands use the ordinary sandbox executor and durable invocation records
under `state_dir/packets/debug-CASE_HASH/` (logical IDs are never path components);
that directory belongs to the trusted controller, never the child's write grant.
Policies overlapping the controller state are rejected. State/artifacts remain
outside the source checkout. Receipts retain actual
argv/cwd, backend/policy, exit status, timing, and redacted stdout/stderr artifacts.
Their target/guardrail verdict is derived only from the reviewed exit-code oracle.
Replay validates receipt identity, exact argv and source, and all resulting evidence
references. Raw worker metadata cannot forge this path.

`collect_execution(...)` recovers evidence from an already completed receipt,
without running any command again. Cancellation, timeout, source mutation, or
incomplete capture produce an interrupted attempt rather than a green result.
If the process died before finishing a receipt, no result is inferred; after the
supervisor has reconciled orphan processes, the owner can use
`reconcile_execution(..., artifacts, approved_by=owner, resolution=...)` to acknowledge
that unknown attempt. Live executor/child processes prevent reconciliation.
Attempt and time ceilings remain consumed; another attempt needs new authorization.

### Counterexample inbox

All existing failed candidate evaluations remain permanent
`COUNTEREXAMPLE_RECORDED` entries. `Debugger.inbox()` and `/debug inbox` expose them
with matching original verifier evidence references, without interrupting ordinary
refinement or claiming a cause. The inbox is nonblocking; task/evaluator gates still
reject the failed candidate normally.

`activate_counterexample(..., approved_by=owner, contract=reviewed_contract)`
atomically creates a blocking debug case, links the exact original rejection and
verifier evidence, and freezes the reviewed reproduction. It does not call that
old worker failure a freshly executed debug reproduction, and does not invent a
localization chain. Explicit local execution establishes the red-before step.

## Evidence bundle

Capture at the boundary where behavior is decided, not only at the UI:

- exact commands, exit codes, duration, cwd, and sanitized environment identity;
- model request, system/context messages, tool schemas, completion, and tool calls;
- full relevant transcript with timestamps and actor labels;
- source revision, dirty-state digest, dependency/runtime versions, and deployment
  identity;
- input fixtures and output artifacts with hashes;
- code/config diff for the candidate;
- focused verification results and broader guardrail results;
- the worker's claim and the verifier's adjudication as separate events.

The event ledger stores metadata and hashes. Sensitive or large bodies belong in a
retention-scoped artifact store.

## Localization discipline

Facts and hypotheses are different event payloads. A useful localization chain is:

```text
symptom -> first divergent event -> deciding boundary -> violated contract -> cause
```

For Sarah, this prevents a conversational symptom from being mislabeled as a model
quality problem when the first divergence is a stale session generation, an
incompatible semantic question/command schema, endpointing, or deployment traffic.

## Experiment contract

Each experiment declares:

- one hypothesis;
- one bounded change;
- the reproduction it will rerun;
- the expected evidence delta;
- guardrails that must not regress;
- a rollback reference;
- cost/time/attempt bounds.

Changing the prompt, runtime, fixture, and judge together invalidates causal learning.
If several changes must ship together, the experiment should explain the coupling.

## Verification and eval promotion

A case may become `VERIFIED` only when all declared evidence kinds exist and the
target behavior has been observed at the appropriate fidelity. A case may become
`EVAL_PROMOTED` only after a versioned eval definition is linked to the case.

Promotion records:

- the original observed behavior;
- target behavior and authority;
- the reproducer/fixture;
- the oracle or judge;
- relevant environment pins;
- evidence hashes for red-before and green-after;
- ownership and review status.

The live debug archive stays ephemeral when it may contain sensitive data. A recorder
derives a sanitized, reviewed regression fixture from it. The system under test must
not invent its own expected outcome.

## Hill-climb promotion

A candidate is promotable only if:

- at least one named measurement improves by its minimum meaningful delta;
- no measurement exceeds its allowed regression tolerance;
- required repetitions complete under the same relevant environment identity;
- the debugging/eval ratchet has captured any newly discovered failure family;
- an authorized reviewer accepts the behavior change.

Measurements remain a vector. A large latency improvement cannot numerically erase a
safety regression, and a quality score cannot erase a missing terminal outcome.
