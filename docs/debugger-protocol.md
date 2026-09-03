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

The executable slice currently enforces `OPEN -> VERIFIED -> EVAL_PROMOTED`. The
intermediate states will be added with experiment leasing and review.

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
