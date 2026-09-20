# Seed-assisted model proposals

`/propose` bridges planning dialogue to a reviewable V5/V6 candidate. It is a
bounded refinement/decomposition of a reviewed seed, not unrestricted prose-to-code
authority and not an automatic claim that a generated plan is correct.

From the repository's clean committed root, select an existing planning provider,
then explicitly request one proposal:

```text
/model claude:fable
/propose --from /absolute/path/reviewed-seed.json Clarify and implement the agreed outcome
```

`reviewed-seed.json` is a complete executable V5 or V6 runbook. Provider worker
adapters must embed their exact `profile_snapshot`; a path to an unfrozen profile
is insufficient. No schema migration or new worker profile is inferred. Manual
mode makes no model request, and startup never invokes this command implicitly.

## What one request does

The client discloses one planning-only invocation of an explicitly selected,
supported provider. The transport has a 120-second request timeout; this is not a
hard dollar cap or a proof of one billable API request. A CLI may internally make
multiple requests; provider request count and missing cost remain unknown. Recorded
token counts are retained. There are no worker tools, execution, automatic model
downloads, approval, or automatic retries in this operation.

The currently supported proposal transports are:

- Claude CLI: a read-only `--safe-mode --help` capability check must expose the
  required controls before the planning invocation. The invocation adds
  `--tools ''`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, and
  `--setting-sources ''` to safe mode, disabled slash commands and a one-turn limit.
  Missing flags fail closed before inference. Runtime code within the reviewed
  source workspace is refused. The installed CLI and its administrative policy
  remain trusted; Camol does not extract or forward its OAuth tokens itself.
- An explicitly selected numeric-loopback chat endpoint: Camol sends no tool
  definitions, sets `tool_choice: "none"`, and rejects returned tool calls without
  executing them. The owned HTTP transport follows no redirects, consults no proxy
  environment, and shuts down its socket on cancellation/deadline; the request
  thread closes its response/connection before returning. Numeric-loopback
  connect/TLS setup uses a short bounded timeout, with up to 2.5 seconds allowed
  for cancellation cleanup. Endpoint code is user-controlled, not sandboxed by this client;
  a local endpoint is not proof of air-gapped inference or weight identity.

Codex's existing normal conversation and worker integrations remain available, but
`/propose` refuses Codex because this adapter does not have a verified all-tools-off
control. A read-only sandbox still allows reads; disabling the documented shell
feature is not proof that all tool surfaces are absent. The
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents those controls separately. A future no-tools API adapter requires its own
explicit credentials and capability contract, never extracted CLI OAuth credentials.
The OpenAI API planning transport is not implemented by this slice.

The provider receives the normalized seed, the human's exact goal, a source-identity
record, and the existing bounded planning dialogue. Repository contents are not
implicitly uploaded by Camol's proposal request. Operational notices, status output, prior proposal JSON and
slash commands are excluded from dialogue. At most 20 dialogue entries of 4000
characters each are retained; the complete seed request must fit the planning
transport's 12000-character message limit. Oversized seeds fail before a call,
instead of silently sending a truncated contract. Seed files are bounded regular
UTF-8 JSON files, not pipes or devices.

The provider must return exactly one JSON object:

```json
{"questions": ["Which additional oracle should the owner approve in the seed?"]}
```

or `{"runbook": ...}` containing a complete V5/V6 runbook. Questions do not replace
or approve an existing plan. Answer in dialogue or edit the reviewed seed, then
explicitly invoke `/propose` again. Markdown fences, unknown response fields,
duplicate object keys, non-finite numbers and oversized output are rejected.

## The authority boundary

The returned candidate must preserve the seed's schema version, run identity,
completion requirements, rules, workers, provider profiles, capacities and resource
ceilings. Its objective becomes the human's exact requested goal. All original task
and step identities remain present. Existing commands, evaluation argv/cwd,
capabilities, required evidence and evaluator assets cannot change. Acceptance
conditions, completion requirements and dependencies cannot be removed.

The model may refine task goals/instructions, add acceptance conditions or
dependencies, and propose bounded decomposition. New tasks may use only exact
seed-declared commands/evaluators and available capabilities, retaining the union
of seed evidence requirements and evaluator assets. V6 resource requirements must
match an existing seed request. Total task attempts, work-command occurrences and
attempt-weighted evaluator invocations cannot increase. There are at most 64 tasks
and 128 steps. A request needing new commands, permissions, worker profiles, tests
or budget must become questions asking for an updated reviewed seed.

Existing invariants, obligations, mappings and gate thresholds remain normative.
New invariants need explicit evaluator/obligation mappings. Every proposed invariant
requires human review, every task gate requires human approval, and final acceptance
remains human. Passing a command does not turn a model-generated prose mapping into
an established fact. Semantic scope and mapping quality still require owner review;
these structural checks are not a semantic proof that model instructions are safe.

## Human review, source identity and execution

A successful response becomes an unapproved product-plan v3 candidate, visible in
full through `/plan`. Its digest binds the kernel runbook, source identity, seed
digest, request/response identities and planning-call receipt. V1/V2 product plans
remain readable without rewriting their digests.

Use `/approve FULL_PLAN_DIGEST` after reviewing all commands, scope and invariant
mappings. A bare `/approve yes` cannot approve a model-generated candidate. Only
then may `/run` use the existing explicit provider policy/spend and exact readiness
gates. The new state directory is not initialized and no supervisor is started
during proposal generation. Actual task execution pauses for the human state gates
and finally `/accept`.

The client pins the clean committed source plus directly hashed tracked bytes,
checks source and seed again after the provider responds, and checks source on
approval and launch. The exact source binding travels through detached startup,
is frozen in the ledger before approval, and is checked at admission and launch
and on resumed execution. Actual workspace materialization uses the approved
revision, not a later mutable source HEAD. Reattachment checks the supervisor's
source binding, not just the runbook digest. An `assume-unchanged` flag cannot hide a changed tracked file.
Symlinks/submodules, uncommitted source, credential-shaped tracked paths and overly
large source inventories are refused by the current source-identity implementation.
Ignored runtime inputs, OS libraries and remote services are not hermetically pinned.

Cancellation, malformed output, changed source/seed, or an out-of-envelope proposal
retains recorded usage and a rejection record without replacing the prior candidate.
Private `proposal-events.jsonl` records request/outcome and exact identities;
`planning-calls.jsonl` records usage separately. The redacted transcript retains the
request and bounded unapproved response. A local endpoint may finish a request after
client cancellation; absent usage remains unknown rather than zero.

There is no in-flight amendment UI in this slice. Proposing a replacement while a
run remains active is refused. Revising an approved/running executable plan requires
the separate explicit revision protocol, not a model message or `/btw` note.
