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

`natural-language turn -> Jev observations -> deterministic route policy -> bounded context + response contract -> existing model adapter -> output checks -> response`

Keep `/cancel`, approvals and active grill answers on their current explicit
paths. In `camol/app.py`, the free-text branch in `_handle` currently invokes
`_call_planning`; insert the opt-in routing boundary there. Jev should be a
separate decision provider, not another conversational selection in
`conversation.parse_selection`, because it cannot produce a ConversationReply.

Proposed modules:

| Module | Responsibility |
| --- | --- |
| `camol/jev.py` | Bounded HTTPS transport, strict response validation, model identity, cancellation and usage receipts |
| `camol/prompt_routing.py` | Versioned questions, deterministic route selection, abstention and response contracts |
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
| Relation to current task | Choice: continue, revise, new, unclear | Preserve or explicitly revise the task contract; never silently replace it |
| Information sufficiency | Choice: enough, missing, unclear | Continue only with enough context; otherwise ask a targeted question |
| Reasoning demand | Score with explicit mechanical/local/multistep levels | Select only among owner-configured available models within budget |
| Requested answer form | Choice: brief_answer, explanation, plan, artifact, unclear | Apply a deterministic response contract; explicit user format wins |

Each question must explain its own meaning and refer directly to relevant state
fields. Questions are independent; compose their answers in code. The API's map
keys name outputs but are not sent to the underlying model, so names like
`intent` or `needs_context` cannot substitute for real question instructions.
[API reference](https://docs.typesafe.ai/api).

Start with classification plus output contracts. Add retrieval filtering only
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

1. Implement offline request/response contracts and fixture tests first.
2. Run an opt-in shadow evaluation: record Jev's recommendation while retaining
   current routing. Establish a fixed token/cost cap before making hosted calls.
3. Compare against current planning on owner-labeled prompts: route precision,
   follow-up continuity, abstention, constraint compliance, useful answer length,
   actual usage, latency, and task success. Cover typos, “yes/do it,” mixed goals,
   prompt injection, stale context, model outages and contradictory instructions.
4. Enable limited low-risk routing only after agreed thresholds pass. Evaluate
   upgrades against the pinned dataset before changing model or policy versions.
5. Add output assessment and then context reranking as separate measured changes.

No accuracy, latency, cost savings or readiness claim is made from vendor demos.
The first release should prove that route failures cannot widen existing authority
and that classification improves the owner's actual workflows.
