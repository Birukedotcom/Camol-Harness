# Jev integration into Camol

Status: researched implementation proposal, 2026-09-19. No Jev API request,
credential setup or live classifier validation has been performed. Runtime hooks
below are proposed, not shipped features.

## What Jev provides

Jev is TypeSafe AI's decision model. It returns closed-set choices, rubric scores
and yes/no probabilities rather than generating answers. Its primitives are
`Choice`, `Score` and `Noul`; multiple independent questions can share a request.
This makes it suitable for selecting a handler before a generative model runs.
[Official introduction](https://docs.typesafe.ai/introduction),
[intent-routing pattern](https://docs.typesafe.ai/patterns/intent-routing).

Use the versioned `jev-1.13.0` initially, subject to account availability. The
current price is $0.042 per million input tokens with free output; published
limits are 64k tokens overall and 32k for state plus the longest question.
Version aliases move, so freeze the resolved model with each evaluated policy.
These are vendor specifications, not measured Camol performance.
[Models](https://docs.typesafe.ai/models).

## Proposed request path

`explicit commands -> existing command engine`

`natural-language turn + observed task state -> intent and mechanism proposals -> deterministic eligibility checks -> bounded procedure + context + response contract -> executor -> independent evidence checks -> response`

Keep `/cancel`, approvals and active grill answers on their current explicit
paths. In `camol/app.py`, the free-text branch in `_handle` currently invokes
`_call_planning`; insert the opt-in routing boundary there. Jev should be a
separate decision provider, not another conversational selection in
`conversation.parse_selection`, because it cannot produce a ConversationReply.

Proposed modules:

| Module | Responsibility |
| --- | --- |
| `camol/jev.py` | Bounded HTTPS transport, strict response validation, model identity, cancellation and usage receipts |
| `camol/prompt_routing.py` | Hierarchical intent/mechanism questions, deterministic eligibility, abstention and response contracts |
| `camol/mechanism_catalog.py` | Versioned procedures with distinguishing criteria, preconditions, allowed effects and required evidence |
| `camol/context_selection.py` | Assemble allowed task state and evidence; optionally rerank retrieved candidates |
| `camol/response_validation.py` | Mechanical checks first; optional Jev semantic observations; bounded repair |
| `tests/test_prompt_routing.py` | Offline policy/transport fixtures plus opt-in labeled model evaluation |

Store a dedicated decision-call record alongside planning usage in
`camol/session.py`. Do not pretend Jev belongs to the existing provider enum or
that a classifier receipt is a kernel approval. Bind request/state/question
hashes, policy and model versions, turn ID, selected route, reason codes,
probabilities, confidence, latency, input/output usage and unknown outcomes.
Use the existing private persistence/redaction conventions; keep API keys and
raw provider bodies out of ordinary diagnostic logs.

## State and atomic questions

Send the latest input verbatim, explicit current goal and constraints, relevant
recent turns, task/plan identifiers, and available handler descriptions. Keep
source fields distinct from application policy. Do not classify “do it” without
its conversational referent or let a summary replace the user's original goal.
Structured state is supported directly by the API.
[State documentation](https://docs.typesafe.ai/concepts/state).

Initial policy, to evaluate rather than assume correct:

| Observation | Primitive | Camol interpretation |
| --- | --- | --- |
| Requested workflow | Choice: explain, research, plan, implement, debug, review, provision, mixed, unclear | Choose a proposed workflow; implementation/provisioning still enters reviewed planning |
| Required mechanism | Choice among eligible, specifically described procedures plus insufficient_evidence and other | Propose a procedure whose preconditions are checked in code; do not infer authorization |
| Relation to current task | Choice: continue, revise, new, unclear | Preserve or explicitly revise the task contract; never silently replace it |
| Information sufficiency | Choice: enough, missing, unclear | Continue only with enough context; otherwise ask a targeted question |
| Reasoning demand | Score with explicit mechanical/local/multistep levels | Select only among owner-configured available models within budget |
| Requested answer form | Choice: brief_answer, explanation, plan, artifact, unclear | Apply a deterministic response contract; explicit user format wins |

Each question must explain its own meaning and refer directly to relevant state
fields. Questions are independent; compose their answers in code. The API's map
keys name outputs but are not sent to the underlying model, so names like
`intent` or `needs_context` cannot substitute for real question instructions.
[API reference](https://docs.typesafe.ai/api).

Start with mechanism selection plus procedure and output contracts. Add retrieval filtering only
after routing is measured: Jev can score retrieved candidates, but critical goals,
constraints and referenced evidence must remain pinned regardless of its scores.
The provider has a passage-classification cookbook that can inform this later
experiment. [RAG passage classification](https://docs.typesafe.ai/cookbooks/classifying_rag_passages).

## Policy, uncertainty and output checks

Treat every model answer as evidence, not authority. Validate allowed labels,
finite probabilities, distribution normalization, model identity, schema and
request binding before using it. Never translate a `provision` label into a cloud
allocation; route it into the existing approval-bound planning workflow.

Choice/Score confidence summarizes the shape of their distributions. It is not a
separate proof of correctness. Noul has no separate confidence field. Fit per-route
thresholds using held-out labeled prompts; do not ship an arbitrary universal
threshold copied from an example. [Confidence](https://docs.typesafe.ai/confidence).

For low confidence, missing context, mixed intent or API failure, use an explicit
policy: ask for clarification or retain the already-selected planning-only model.
Never silently escalate spend, rewrite an approved plan, or invoke tools. Manual
mode and disabled routing make no Jev calls. Freeze one route per turn; invalidate
cached decisions when any relevant task state or policy changes.

Before displaying a final answer, code checks schema, required fields, length and
valid evidence references. An optional second Jev call can evaluate separate
questions about relevance, scope and support against the original request,
contract, candidate answer and supplied evidence. That signal can trigger one
budgeted repair or an explicit unresolved result. It is not proof of truth and
must not replace executable evaluators for code. Strict output-gated mode buffers
the candidate; unvalidated tokens cannot already have been shown as accepted.

Jev's documented limitations include literal interpretation, numeric reasoning,
large irrelevant inputs, adversarial content and inconsistent related judgments.
Keep arithmetic and structural invariants in code. No component here guarantees
semantic zero drift. [Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

## Paper-driven refinement: route the mechanism

The owner supplied *Mechanism-level routing failure in LLMs over Lean-verified
algebraic structures*, Cázares, Zhang and Ma, arXiv:2607.04534v1 (2026-07-05).
The paper is an architectural reference, not a Jev evaluation. Its experiment
classifies the certificate family behind already-verified mathematical statements;
it does not generate proofs or show that adding a classifier solves routing.

### Findings relevant to Camol

Table 4 (p. 6) reports Set A mechanism accuracy rising from 80.3% to 90.9% for
gpt-oss-120b, and 68.2% to 81.8% for Llama 3.3 70B, when the prompt receives
mechanism-bearing Lean metadata. Llama's verdict accuracy remains 95.5% in both
conditions: a correct verdict does not establish correct mechanism selection.

The dominant error confuses a specific Chinese-remainder construction with the
broader ring-equivalence type. Sections 6-7 (pp. 8-10) distinguish insufficient
mechanism cues from persistent confusion between adjacent or differently granular
labels. Some specific cases legitimately fit the broader label while missing the
benchmark's required level. Table 5 also includes three correct-to-wrong Llama
transitions: extra cues do not monotonically improve every case.

Interpretation limits matter: Set A has 22 distinct items and Set B only six.
The three temperature-zero/seed-zero repeats measure backend stability, not
independent examples. The reported ceiling is protocol-specific. A2 cues often
name the expected certificate family; the result demonstrates the usefulness of
supplied mechanism metadata, not autonomous discovery of that metadata. Jev was
not among the evaluated models. General software tasks may have several valid
procedures, unlike a corpus with an assigned certificate-family label.

### Proposed transfer to the harness

The primary route should identify an operational procedure with explicit evidence
obligations. Model size and answer length are downstream decisions. The broad
intent `debug` is useful for finding candidates but is not a complete route.

| Request or observed situation | Proposed specific mechanism | Distinguishing evidence and completion check |
| --- | --- | --- |
| CI cannot import an encrypted-recovery dependency | repair_test_environment | Dependency declaration, installed-package evidence and failed import; rerun the affected tests |
| Restart soak interrupts workers before all outputs are durable | repair_verifier_boundary_fixture | Cancellation location plus process/result records; verifier-restart soak and independent no-duplicate-effects tests |
| A worker launch timed out after dispatch | reconcile_uncertain_dispatch | Retained request/process identity and result receipts; no automatic replacement or replay without resolving the uncertainty |
| A requested change alters frozen task scope | propose_plan_revision | Current plan digest and changed requirements; separately reviewed successor plan |

These entries illustrate catalog design, not automatic diagnoses or live handlers.
The classifier must be able to report that the distinguishing evidence is absent.
A closed enum cannot manufacture information or make the selected mechanism true.

Each catalog entry should contain a stable ID, parent intent, explicit required
granularity, distinguishing conditions and counterexamples, input contract,
machine-checkable preconditions, allowed effects, context sources, procedure
steps, required output/evidence, evaluator, stop conditions and version digest.
The application filters out unavailable/unauthorized effects before dispatch;
Jev only proposes among candidates. Retain the original request alongside the
proposal, evidence references and actual eligibility result.

Bind each adopted procedure to the current task and state version. At meaningful
boundaries, such as a new user constraint or contradictory tool result, reevaluate
whether it still applies. This is not token-by-token steering. Changing procedures
requires an explicit recorded transition; changes to approved authority continue
to require existing plan approval. Never let previous classifier output stand in
for newly observed evidence.

Final checks have separate purposes: valid response structure, appropriate
mechanism, permitted effects, and verified task outcome. A Jev relevance score
cannot certify tool execution or replace the existing deterministic evaluator.
Procedural invariants can reject invalid transitions even when fluent text sounds
plausible; arbitrary semantic faithfulness still needs empirical evaluation.

### Test the mechanism-routing hypothesis

Use independently reviewed cases with a required label level, admissible mechanism
set, evidence manifest, expected actions, and outcome checks. Do not derive the
reference labels from Jev itself. Retain ambiguity where multiple mechanisms are
valid, and label underspecified cases for clarification rather than forced choice.

Compare on the same held-out cases:

1. Current generative orchestrator with available context.
2. Jev receiving only the user prompt and catalog.
3. Jev receiving the prompt, catalog and actual task/tool observations.
4. An explicitly oracle-assisted diagnostic condition with the correct mechanism
   cue, kept separate from deployable performance estimates.

Include missing, stale and misleading cues, hierarchy confusions, continuations,
misspellings and ambiguous requests. Report per-mechanism confusion matrices,
parent-versus-leaf errors, abstention, paired wrong-to-correct AND correct-to-wrong
changes, downstream task success, policy violations, tokens and latency. Split and
count by distinct task families, not repeated identical runs. Record whether an
improvement comes from additional evidence, a better taxonomy, or the classifier.
A larger reasoning model is not the automatic remedy for missing evidence or
poorly defined labels.

## Transport and activation

Call `POST https://api.typesafe.ai/v1/systemone` with bearer authentication and
JSON containing `model`, `state` and `questions`. Responses include model,
answers and usage. Use `TYPESAFE_API_KEY` as an environment reference, never a
committed value. [Quickstart](https://docs.typesafe.ai/introduction/quickstart).

Camol supports Python 3.9; the official `typesafe-sdk` requires Python 3.10+.
Prefer a small standard-library HTTP adapter to preserve Camol's existing
compatibility, or isolate an optional SDK path to supported runtimes. Validate
TLS, disallow credential-bearing redirects/arbitrary caller endpoints, bound
response bytes and elapsed time, and connect cancellation to the request.
[SDK requirements](https://docs.typesafe.ai/introduction/quickstart).

If the SDK is chosen, explicitly control its timeout/retries. It supports
`RetryPolicy(max_retries=0)`; its logging may expose request/response bodies.
The harness should own attempt accounting and any bounded retry policy. A timeout
after dispatch retains unknown usage; do not assume a repeated billable call is
free or exactly-once. [Client configuration](https://docs.typesafe.ai/sdk/python/api/clients/sync),
[retry controls](https://docs.typesafe.ai/sdk/python/api/retries).

Hosted activation sends the selected prompt/context to TypeSafe. Define the
allowed data boundary before connecting Enrollment Hub material. The model docs
state customer requests are not training data; enterprise ZDR is a separate
arrangement, not a default local-only or zero-retention guarantee.
[Data handling](https://docs.typesafe.ai/models).

## Rollout and proof

1. Define the mechanism catalog and independently labeled cases, then implement
   offline request/response contracts and fixture tests first.
2. Run an opt-in shadow evaluation: record Jev's recommendation while retaining
   current routing. Establish a fixed token/cost cap before making hosted calls.
3. Run the evidence/label-granularity experiments below. Compare against current
   planning on owner-labeled prompts: route precision,
   follow-up continuity, abstention, constraint compliance, useful answer length,
   actual usage, latency, and task success. Cover typos, “yes/do it,” mixed goals,
   prompt injection, stale context, model outages and contradictory instructions.
4. Enable limited low-risk routing only after agreed thresholds pass. Evaluate
   upgrades against the pinned dataset before changing model or policy versions.
5. Add output assessment and then context reranking as separate measured changes.

No accuracy, latency, cost savings or readiness claim is made from vendor demos.
The first release should prove that route failures cannot widen existing authority
and that classification improves the owner's actual workflows.
