# Goal → reviewed V5 plan

The guided creation path does not require a hand-written JSON seed. It produces an
unapproved executable candidate, not an assurance that a generated plan or evaluator
is correct. Legacy `/grill GOAL`, `/import`, and seed-assisted `/propose --from ...`
remain unchanged.

```text
/model claude:fable
/grill --draft Build the bounded feature we discussed
```

The selected planning provider must support the existing no-tools proposal adapter:
capability-checked Claude CLI or a user-selected loopback chat endpoint. This does
not add a new provider or bypass provider-owned authentication. Codex remains
available for normal conversation and explicit worker profiles, not no-tools draft
generation. Startup, the wizard, and envelope confirmation make no model call.

## Guided decisions

Answer the six questions in the ordinary composer:

1. Observable completion outcomes.
2. Exclusions and effect boundaries.
3. Invariants that survive retries, failures, and integration.
4. Observable counterexamples/oracle ideas, or explicitly `unknown`.
5. Exact worker/runtime selection.
6. Resource ceilings.

Worker selection is explicit, separate from the planning model:

- `claude @camol/claude-fable-5-1` freezes the existing bundled profile.
- `codex /absolute/path/profile.json` or `codex-oss /absolute/path/profile.json`
  freezes a compatible existing profile, including its declared limitations.
- `process --sandboxed COMMAND {packet} {result}` freezes a process argv with the
  existing hardened local tier. The kernel refuses unavailable sandbox backends.
- `process --trusted COMMAND {packet} {result}` deliberately selects the weaker
  host tier. It requires a separate exact run-policy acknowledgement before launch.

Profiles are read as data, not executed. Their effective token/turn/cost policy is
bounded by the chosen profile and the human limits, then embedded verbatim in the
creation envelope. No model can replace that profile or select another runtime.
`{workspace}` names the isolated task checkout; workers start in their individual
box subdirectory. Evaluators can explicitly select `cwd: workspace_root`.

Limits use `boxes=N concurrency=N turns=N tokens=N cost_cents=N timeout=N tasks=N
attempts=N`, or `defaults`. Every default is displayed before confirmation.
Process workers use `cost_cents=0`, which means no provider accounting adapter is
selected—not proof that an arbitrary external program cannot spend money. Provider
dollars retain the profile's distinction between enforced caps and reservations.

## Separate approvals

The completed wizard displays the full source-bound creation envelope and digest.

```text
/draft
/draft confirm ENVELOPE_DIGEST
/propose
```

Confirmation permits planning inside those boundaries only. `/propose` discloses
one no-tools planning invocation, its 120-second request deadline, bounded context,
and unknown internal provider request count/cost. It does not execute commands,
start workers, download models, or retry automatically.

The model may propose new task and evaluation argv, unlike the seed-refinement
mode. Run controls, exact agents/profiles, source, human requirements and scope
rules stay fixed outside its output. The current creation mode emits V5, not an
implicitly populated V6 capacity policy. It permits at most the reviewed task count
and attempts, with additional hard bounds on steps and commands.

An unresolved oracle or missing requirement mapping becomes visible questions.
Answer each in the composer; no extra model call follows automatically. The revised
envelope needs a fresh exact confirmation before explicitly invoking `/propose`
again. Coverage entries bind the exact human predicate to proposed invariant IDs
and explain a possible falsification. They are proposals, not observed evidence.
Exact outcome mappings retain unconditional `eventually` predicates. Human
invariants retain unconditional, global `preserved` predicates bound to every task
gate; a model cannot quietly narrow a cross-run requirement to one task or add a
conditional precondition. This records required scope, not proof of continuous
temporal observation between the actual evaluator checkpoints.

For a complete candidate:

```text
/plan
/review
/review PRODUCT_PLAN_DIGEST
/approve PRODUCT_PLAN_DIGEST
```

`/review` displays worker argv/profiles, new commands, evaluator cwd/argv, source,
limits, effects/isolation limitations, and invariant-to-oracle rationale. The human
must inspect the complete `/plan`, challenge inadequate checks, and reject scope
expansion. Bare `yes` cannot approve a goal-created plan. Review acknowledgement
does not create evidence or authorize execution by itself.

Only then does `/run` perform its existing readiness/provider handshake. The weaker
host tier additionally requires `--accept-draft-policy PRODUCT_PLAN_DIGEST`:

```text
/run --accept-draft-policy PRODUCT_PLAN_DIGEST
```

For hosted Claude, add the existing `--accept-spend --worker-cents N` flags. For
Codex/OSS, add the exact provider-policy acknowledgement shown by its existing
launch flow (hosted Codex also requires `--accept-spend`). Strong local process
plans use `/run` without provider flags. No lease is issued before kernel admission.

Every V5 state gate pauses for `/gate TASK DIGEST`; the integrated outcome finally
requires `/accept OUTCOME_DIGEST`. `/boxes` and `/box ID` inspect the N-box fleet.

## Guarantees and limits

The source pin is checked across client approval, detached launch, kernel admission,
resume and reattachment. Generated candidates and their exact envelope, request,
response, usage and review identities persist across terminal restarts. Malformed,
escalating or source-drifted responses do not become approved work.

Deployment, uploads, downloads and credential mutation are not authorized by this
wizard. This is **not a hard no-egress guarantee** on a `developer_trusted` profile:
hosted inference needs network, existing hosted profiles can permit unrestricted
egress, and the weaker outer host tier does not enforce arbitrary-program file
boundaries. Those limits are prominently disclosed and explicitly acknowledged;
string-screening command names is not a security boundary. Use a supported hardened
process tier when OS-enforced local isolation is required, or do not launch.

Ignored source inputs and remote services are not hermetically pinned. Semantic
oracle adequacy, business truth and consequential ambiguities remain human
responsibilities. No runtime or account is marked ready by drafting a plan. There
is no in-flight amendment UI or automatic external-action authorizer in this slice.
