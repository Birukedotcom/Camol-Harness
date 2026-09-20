# Provisioning and prompt routing: readiness assessment

Status: proposed integration; not a claim of live provisioning or drift-free inference.
Assessment date: 2026-09-19.

## Current baseline

The consolidated harness contains local execution, approval-bound plans, durable
leases/events, capacity and budget accounting, target adoption, SSH observations,
source handoff, target-local preparation and worker evidence receipt protocols.
It does not yet join these into authenticated remote allocation, admission,
dispatch and accepted task completion. `camol.targets.descriptor` explicitly
accepts only adopted targets; `target_preparation` explicitly grants no provision,
controller admission or model-launch authority. A green suite is necessary but
cannot establish live infrastructure readiness.

## Reference system

The owner's MAGI reference maps to the current Buckeye Sandbox tooling in the
local Omni capability guide. Enrollment Hub specialists use that native factory;
general workers use the separate Omni factory. Camol should integrate an existing
factory behind a provider adapter instead of duplicating its credentials, network
policy and lifecycle inventory.

Inspected evidence:

- Enrollment Hub local tracking revision `8a99eab0caed45f82022db83ddf5939e5f9e5286`,
  `sandbox/sandbox.contract.json` and
  `docs/runbooks/staging-sandbox-architecture.md`.
- Local Omni capability guides: `capabilities/buckeye-sandbox/README.md`,
  `capabilities/omni/README.md` and `capabilities/exe-dev/host/README.md`.

These are source observations, not a live factory health or provisioning check.
The inspected Enrollment Hub machine contract selects the integration branch,
while some runbook text still says staging. Resolve and record the approved exact
source contract and SHA at integration time; do not silently use either prose
branch name as execution authority.

The reference lifecycle separates allocation, guest bootstrap, network enrollment,
terminal readiness, development readiness, requested-agent authentication and real
model inference. A terminal-ready machine is not proof of task readiness. The
factory derives owner identity, retains uncertain allocations, and uses stable
request IDs. Terminal panes are views of sessions, not machine identities.

## Provisioning acceptance before claiming support

1. Freeze the provider, authenticated owner, repository/source SHA, agent profile,
   allowed resources, maximum worker count, expiry and budget in a reviewed request.
2. Journal a stable allocation request ID before calling the factory. Bind the
   returned factory/worker identity and lifecycle evidence to that request. Inspect
   the same allocation after a timeout; never allocate a replacement implicitly.
3. Observe exact source, authenticated transport, agent authentication and runtime
   prerequisites independently. Convert only verified evidence into controller
   admission, capacity reservation and a fenced lease for the approved task.
4. Dispatch once into an owned, available session. Human takeover suspends agent
   input. Ambiguous delivery stays unknown until reconciled; it is not a retry cue.
5. Run a bounded task, retain its evidence, deliver the result, run the frozen
   evaluator and integrate only accepted output. Confirm a real model response;
   fixtures and process health do not establish account/model availability.
6. Exercise interruption, stale identity, duplicate delivery, exhausted budgets,
   recovery and final archive verification. Disposal is separately scoped and
   must not remove an unrelated or incompletely archived worker.

The first live acceptance should use one approved disposable worker and a small
repository fixture, with an explicit budget and disposal policy. No worker was
allocated or existing Enrollment Hub environment changed for this assessment.

## Proposed prompt contract

The owner's term “JEV” is unresolved. Do not pick a library, invent an expansion,
or call an implementation JEV until its intended meaning is established.

The desired behavior can be specified independently:

`input -> classification proposal -> policy validation -> bounded context -> selected handler -> response validation -> accepted response or clarification`

- Preserve the original input and bind the route to its digest, session/turn and
  policy version. Do not replace the source request with a lossy classifier summary.
- Route explicit commands deterministically. For natural language, classify intent,
  target, requested effect, missing information and required output. Support multiple
  intents and abstention; ambiguous inputs must not gain execution authority.
- Separate classification from authorization. A model's suggested route cannot
  grant tools, provision workers, approve a plan or increase a spending limit.
- Give each route an owner-defined contract: allowed handlers/tools, relevant
  context sources, output schema, token/cost/time limits and acceptance checks.
  Token limits require the selected provider's token accounting; character counts
  must not be presented as exact token counts.
- Select context by the current contract and evidence references. Detect overflow,
  stale evidence and contradictions explicitly instead of silently truncating the
  user's goal or forwarding the entire accumulated transcript.
- Validate structured results before downstream use. Deterministic constraints can
  enforce schema, allowed actions, identifiers, length and evidence membership.
  They cannot prove factual correctness or semantic faithfulness of arbitrary prose.
- Withhold unvalidated content from action consumers. If text is streamed before
  complete validation, label it provisional and prevent tools from consuming it.
  Repair attempts must have a finite budget; otherwise return a typed failure or
  ask for the missing information. Preserve actual usage even on failed attempts.

## Measurable routing acceptance

Use a versioned, held-out corpus of the owner's representative prompts with
owner-reviewed intents, allowed effects and desired answer formats. Include
misspellings, follow-ups, multi-intent requests, contradictory instructions,
quoted prompt injection, stale session context and out-of-distribution requests.
Measure route precision/recall per class, unauthorized-effect rate, appropriate
abstention, constraint compliance, semantic task success, unnecessary answer length,
latency and actual token/cost usage against the current planning-only conversation.
Agree thresholds before calling the change an improvement; do not assert a
semantic no-drift guarantee from schema validity or a classifier confidence score.

## Implementation order

1. Repair consolidated CI and run the full platform matrix and recovery soak.
2. Qualify the factory adapter through the lifecycle acceptance above.
3. Resolve JEV and implement the route contracts plus offline replay/evaluation.
4. Connect routing to the terminal/embedding entry points with a small bounded
   live acceptance. Keep infrastructure effects behind existing owner approval.
