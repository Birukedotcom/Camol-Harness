# Camol full implementation and adversarial review

Goal started 2026-09-07 from `codex/product-v0` at `d6cbfae`.
Implementation branch: `codex/v0-full-pass`. The authoritative scope is `SPEC.md`
and the milestone dependencies in `docs/v0-build-plan.md`. The spatial visualizer
remains post-v1 as explicitly specified.

The frozen foundation checkpoint is `c8fb3d1`, pushed to `codex/v0-full-pass`.
Its exact local suites passed 551 tests on both Python 3.9 and 3.12; a further
68-task/eight-box recovery soak passed. The remote matrix passed Linux but exposed
a macOS selected-Xcode sandbox path failure. See
[the exact-commit verification record](full-pass-verification.md), including
packaging, terminal tests and the explicit fixture/live-validation boundaries.

The next wave is isolated on `codex/v0-next`: seed-assisted V5/V6 proposals,
source-bound client/daemon/runner approval, local model-host lifecycle, SSH-bound
control transport, strict external JSON ingestion, and the macOS runtime-path fix.
The exact 2dd6d5e checkpoint passed 631 local tests on Python 3.9 and 3.12 plus
fresh installed-package/terminal checks. Its remote matrix exposed portability
failures; see [the next-wave verification record](next-wave-verification.md).
A passing foundation result must not be used as verification of uncommitted code.

The ergonomics wave adds a source-bound natural-goal creation wizard, exact
command/oracle risk review, one-shot owned-model inference, bounded recent history,
and a cancelled TUI refresh repair. Focused tests include a real two-box build with
human task/final gates and authenticated loopback inference/crash fixtures. Its
exact `41d5824` whole suites passed 687 tests on both Python 3.9 and 3.12,
plus a fresh installed wheel/terminal check. See
[the ergonomics verification record](ergonomics-verification.md); hosted matrix
acceptance remains separate, and the later fixture/performance changes need
their own results.

The projection checkpoint `9e5420a` passed a new 170-task/eight-box, five-restart
soak (5,636 events, 530 artifacts, source unchanged, replay equal). Its remote
matrix passed five jobs; GitHub billing prevented the sixth job from starting.
Subsequent integration adds durable provider-preflight accounting, cancellable
bounded native probes, contention-aware read-only host checks, and a read-only
retention inventory/CLI. Whole-suite verification follows those merges.

An independent mixed-launch review reproduced a run-wide budget race: concurrent
hosted calls saw the same remaining committed balance. Atomic admission now uses
the existing immutable invocation journals under a cross-process lock, retaining
pending ceilings, deduplicating known bills and carrying ancestor charges.
Focused and whole-suite results for this repair must be read at its own checkpoint;
per-invocation caps and provider overrun caveats alone did not fix the race.

## Acceptance ledger

The `codex/v0-target-inventory` follow-up adds versioned offline GCP inventory
decoding, scoped identity review and partial-result reporting for SPEC §19.5.
It feeds the existing owner adoption flow without authenticating or executing a
target. [Inventory boundaries](target-inventory.md) retain the remaining live
discovery, worker admission and execution requirements.

The [native acceptance checklist](native-acceptance.md) maps M5–M7 evidence to the
actual current tests. The opt-in provider smoke only checks capability/model/cost;
it is not the full build/refine/recover/human-acceptance gate. Executing the shipped
native example's verifier confirms its normal-newline behavior without a provider
call; its runbook and spending limits remain unchanged.

The `codex/v0-usage-activity` review repairs reporting collisions between boxes that
reuse local invocation/tool IDs, rejects conflicting duplicates, and surfaces
missing/invalid/regressing command timing instead of reporting measured zeros.
Unbound activity remains explicitly incomplete. Provider billing/reservations and
token accounting are unchanged; see [activity coverage](usage-accounting.md#scoped-activity-counts-and-timing-coverage).

The isolated recovery sequence follows the pane/peer/cancellation checkpoints:
ordinary archive boundary, encrypted captured-workspace recovery, then
`codex/v0-run-recovery` for owner-reviewed completed-run evidence and code.
The predecessor workspace product `b5121a40dd7de1af4c3728fa679dac81096c67aa`
passed 1,037 full-suite tests on Python 3.12 and 3.9 (three optional skips on 3.9).
Those results are not a gate for the subsequent run-recovery additions. See the
[checkpoint verification record](ergonomics-verification.md) for exact coverage.
Main and the user installation remain unchanged; isolated implementation is not
the same as a released or globally installed product.

The `codex/v0-worker-delivery` follow-up implements a durable, authenticated
lease-bound evidence outbox/inbox with atomic receipt recovery, backpressure and
offline CLI inspection. Its real child-process and kernel-lease tests exercise the
delivery boundary, not remote execution. Owner enrollment, confidential transport,
target-side launch and serialized kernel promotion remain required; see
[worker delivery scope](worker-delivery.md).

The `codex/v0-worker-enrollment` follow-up adds reviewed per-lease stream/key
commitments, public enrollment/revocation events, offline CLI and owner API access,
and kernel-write-serialized receipt ingestion. This closes the arbitrary embedding
callback's lease-mutation race for users of that service, not the distributed
executor gap. It neither promotes worker claims into kernel results nor adopts a
machine; see [enrollment boundaries](worker-enrollment.md).

The `codex/v0-worker-tls` follow-up adds opt-in encrypted evidence delivery with
CA/name/leaf-pin validation, pre-handshake connection limits, cancellation and an
explicit CLI flush. Real socket and child-process tests do not prove distributed
execution. Supervisor listener integration, durable transport telemetry, target
admission/launch and kernel promotion remain open; see [TLS scope](worker-tls.md).

The `codex/v0-worker-import` follow-up captures received worker reports into run
history with an atomic page/cursor/request event and exact retry receipts. Box
inspection, offline CLI and export can retain these reports without retaining
private keys. They remain unverified, including artifact-shaped and billing claims;
see [capture boundaries](worker-import.md). Trusted result promotion remains open.

The `codex/v0-worker-gateway` follow-up adds optional supervisor-owned TLS listening
and bounded report pumping under an exact owner-approved policy. Live local control,
expiry/revocation checks, restart material validation and V2 gateway-bound capture
are implemented; no listener/poll task runs by default. See [gateway scope](worker-gateway.md).
Remote target execution, provisioning and trusted result reduction remain open.

The `codex/v0-target-adoption` follow-up adds run-scoped, owner-reviewed target
identity adoption/retirement, exact provider and transport-profile bindings, replay
and paginated inspection through Python, offline CLI and live local supervisor
control. [Target adoption boundaries](target-adoption.md) distinguish this registry
from authenticated host discovery, worker readiness, launch and fleet-wide salvage;
those distributed-execution gates remain open.

The `codex/v0-target-runtime` follow-up measures actual local installed/runtime
identity and records expiring generation-bound reports via Python, CLI and live
supervisor control. Background measurement preserves control responsiveness and
revalidates owner/run/plan/adoption before append. [Runtime observation limits](target-runtime.md)
distinguish observed CPU count from capacity and local self-observation from remote
attestation or task readiness. Remote execution remains unfinished.

The user supplied tmax and smux as pane/delegation references. The pinned source
review and command-by-command implementation status are in
[pane orchestration](pane-orchestration.md). A shared metadata-only `/overview`
and standalone command now expose boxes, task dependencies and attention. The
current-run searchable switcher is now implemented with grouped metadata, paginated
keyboard selection and stale-plan rejection. Exact-run offline box inspection and
persistent current-plan pins/groups are implemented. Focus/split/paginated-grid
metadata monitoring is implemented; live native peer-tool acceptance remains a
gate. The turn-scoped Python peer-tool interface supports explicit embedding
adapters with replayable reads; native transport is opt-in, not enabled by default.
Admitted peer calls also have content-free start/finish telemetry, replay-bound
results, explicit unknown outcomes and per-task/operation timing profiles.
An explicit per-turn Unix endpoint and bounded MCP stdio relay now exercise
cross-process peer communication. Schema3 Codex profiles now opt into exact
admitted runtime/socket/environment access and per-turn registration/cleanup;
real fake-CLI builds pass. Schema4 Claude profiles now have a separately explicit
restricted-mode peer tier, including worker-visible capability and pre-completion
(not pre-inference) handshake limits. Schema5 adds bounded streaming startup,
owned-handshake/status checks and launch reauthorization before prompt dispatch;
controlled protocol/build tests are not native compatibility or billing proof.
Native live acceptance remains pending.
Existing profiles keep peer tools disabled.
The `/delegate` view exposes declared compatibility without readiness claims;
new-work review reuses the exact stopped-owner revision approval and separate
launch. Model-assisted linked successor generation now accepts a reviewed V5/V6
seed, binds stopped-parent metadata before planning, rejects concurrent source
advancement and feeds the candidate into that exact revision review. It does not
automatically expand authority, approve a revision, or reassign running leases;
live reassignment and unrestricted autonomous plan expansion remain open.
The durable
lease-scoped mailbox now has owner CLI, embedding, worker delivery/consumption,
idempotency, rejection records and offline inspection. Interactive sends/replies,
inbox panes and private uncertain-send recovery are implemented; adding a visual pane never
creates execution authority.

Each row needs implementation, negative-path tests, replay/recovery evidence, and
usable Python/CLI entry points. A fixture cannot establish live provider maturity.

| Area | Status | Evidence / next gate |
| --- | --- | --- |
| Existing V0 baseline | Passed | Original 325 tests passed; expanded suite/reviews continue |
| Lease liveness and long-run recovery | Implemented; local soak passed | Reproof preserves original fence identity; 170-task/eight-box soak, verification restart and process cancellation tests |
| Repeated interactive projects | Implemented; regression tested | Separate run ledgers; reconcile stopped daemon; public `/import` two-run E2E |
| Usage and tool observability | Implemented; integration ongoing | Failed-call usage, unknown reservations, deduped accounting, content-free profile/hotspots and bounded JSONL logger; no invented CPU/memory |
| Debugger protocol and experiment ratchet | Real execution implemented; adversarial tests ongoing | Reviewed argv/source/executable/environment binding; sandboxed red/green/guardrail receipts; counterexample inbox |
| Embeddable Python harness API | Implemented; extended integration ongoing | `Harness` executes isolated N-box fixture, locks ownership, resumes, revises and exports; failure cleanup and bounded event cursors tested |
| Invariants, obligations, thresholds, final acceptance | Implemented V5; integration ongoing | Explicit evaluator mappings, candidate+integration gates, exact owner acceptance; 5 E2E gate tests passed |
| Plan amendment / migration | Conservative mode implemented | Exact owner-approved linked successor, atomic source seal, all tasks reverified, inherited costs, self-contained lineage export; public API E2E passed |
| Real provider execution | Claude/Codex/local adapter code implemented; live gates pending | Explicit capability tiers, frozen profiles and crash-safe invocation intents; no live paid/inference validation |
| Box inspection and build workflow | Implemented bounded local workflow; live gates pending | Overview, scoped switcher, offline evidence, pins/groups, tiled metadata monitoring and durable mailbox; embedding and opt-in native peer transport; bounded seed-assisted delegation into exact human revision review. Native live acceptance, live lease reassignment and unrestricted autonomous plan expansion remain open |
| Capacity / heterogeneous N boxes | Implemented; integration ongoing | Shared broker, fresh observed/owner-declared pools, suspect reservations, real two-run single-slot fairness/cancellation and broker-cursor wake; V6 |
| Watchers and correctable evidence | Local scheduled source implemented | Durable approved polling, normalized JSONL source, daemon restart, interleaving/expiry/cursor/cancellation tests; live cloud source adapters pending |
| Repository dependency graph | Implemented; adversarial tests ongoing | Safe static Python/packaging/npm/Docker/runbook scans, evidence-linked impact/path/cycle queries; JSON/DOT/GraphML |
| VCS integration lineage | Same-run relationships, prospective impact and explicit GitHub observations implemented | Replay/export, exact code identity, seven relations, `/vcs`, bounded branch/PR/check/review reads; actual push receipts, approval-policy interpretation, cross-run objects and Git mutations remain open |
| Local model lifecycle | Download foundation; owned loader in next wave | Exact owner manifests and pinned bytes; one-shot owned llama.cpp lifecycle with local protocol fixtures. No actual model/inference or planner/worker handoff proof |
| Evaluator compiler / benchmark campaigns | Core protocols and offline SWE-bench adapter implemented | Explicit V5 mapping; pinned 3-arm campaign, real gold/no-op fixtures, crash/cleanup reconciliation, protected official-grader protocol; live public trials pending |
| Remote targets / workflow profiles | Pinned SSH control, read-only terminal monitor and owner mailbox relay implemented | Exact host/bridge/target/run scope, durable uncertain mutations and content-free RPC usage; no distributed worker adoption, provisioning or live cloud/voice proof |
| Retention / evidence and code recovery | Inspection, ordinary archive hardening and encrypted recovery implemented in isolated branches | Independent workspace and completed-run/ancestry reconstruction; exact review and no resume/cleanup authority. Operational control/accounting adoption, key lifecycle, expiry and safe teardown remain open |
| Public packaging / portability | In progress | Wheel/sdist built; independent Python 3.12 wheel install+line CLI passed; Homebrew resource hashes verified; OS CI/license/retention remain |
| Extended final verification | Pending | Repeated DAGs, failure/refinement, process interruption, package and UI tests |

## Review findings repaired (final integration verification pending)

- The static graph crawl could execute a repository-supplied `git` through PATH
  and parse a hard link to outside content; both were reproduced with disposable
  fixtures. The isolated graph boundary now uses trusted system Git, live output
  bounds, descriptor-anchored reads and end-of-inventory identity checks. Its
  focused/integration tests do not prove the unimplemented runtime graph overlays.
  The managed-source follow-up adds an explicit per-crawl hard-link opt-in with a
  policy-bound warning; archive/recovery readers retain their single-link rule.
- Long calls now reprove admission before the effective authorization expires;
  failed reproof cancels, salvages and records a typed wait without widening grants.
- Completed interactive sessions reconcile stored/daemon status; each new plan
  has a distinct ledger and `/import` binds source revision before launch.
- Provider failures preserve incurred usage; unknown spend remains explicitly
  unknown and conservatively reserved. Cancellation handling remains under review.
- Debug verification now requires the full experiment protocol and independent
  executed target/guardrail evidence, not evidence-kind labels alone.
- Adversarial Git review reproduced a clean-filter execution from an ostensibly
  read-only status command. Central Git conversion/config hardening and raw source
  identity probes now pass hostile clean/process/smudge/included-config regressions.
- A Python 3.12 sandbox failure exposed an interpreter symlink-chain/read-root gap;
  bounded runtime paths fixed it without granting the full home directory.
- Observer results are now rebased only when their own watch is unchanged, so a
  busy worker cannot accidentally starve a watch. Late scheduled observations fail
  closed at ingestion and replay; repeated approval cannot refill the poll budget.
- Packet-directory worker write access exposed a control-evidence integrity risk;
  separate untrusted result slots now keep trusted invocation/usage records outside
  write grants. Actual sandbox overwrite/unlink traps pass.
- Hardened verifier/debugger measurement uses read-only source plus separate scratch,
  rejecting transient oracle mutation and parent rename. `developer_trusted` is
  explicitly unenforced; before/after hashes alone are not equivalent protection.
- Worker Git inspection uses a private shallow snapshot of the approved baseline,
  not the shared repository metadata. Real sandbox tests permit status/diff and
  scratch while denying shared configuration, private metadata writes, other-box
  objects and Git mutation. Its exact manifest is part of admission.
- Trusted Git operations disable replacement refs as well as executable callbacks;
  a replacement-ref fixture can no longer substitute code behind an approved SHA.
- Legacy provider bills now reconcile with budget counters and diagnostic profiles;
  missing measurements and timing stay explicit. Duplicate legacy/versioned receipts
  are counted once only when their invocation identity and observations agree.
- Journal parsing rejects duplicate keys and ambiguous metadata before advancing a
  cursor. Replay checks the dedicated observer actor separately from owner approvals.
- Benchmark review found that reconciling an in-flight trial could corrupt replay.
  Exclusive execution ownership, transactional transition validation, explicit
  crash recovery and known-overspend failure reconciliation now have regressions.

Initial local soak: 3 disposable four-box runs, 27 tasks total, one verification
restart, unchanged source checkout and exact archive replay for every run. A larger
10-run/eight-box campaign passed all 170 tasks with five verification restarts,
5,598 events, unchanged source checkouts and equal replay in every run (563.252
seconds total). The exact report is `full-pass-soak.json`. No live paid-model or production cloud
validation has been performed in this pass; fixture results do not establish it.

Expanded whole-suite checkpoint: 408 tests passed in 191.543 seconds on Python 3.9
before the latest debugger executor, campaign, revisions and capacity additions.
A later Python 3.12 checkpoint ran 453 tests and found the sandbox runtime-path
failure plus an outdated schema compatibility expectation. Both have targeted fixes;
the whole suite must be rerun after the current additions. Always read the final
exact-commit verification record before treating this development branch as ready.

A subsequent Python 3.9 whole-suite checkpoint passed **506 tests in 370.028
seconds**. This is a development checkpoint, not the final exact-commit result:
the strongest oracle isolation changes, profiler and public-suite adapter work
were still integrating while it ran.

The next Python 3.12 development checkpoint ran 532 tests in 358.820 seconds and
failed 15 scenarios. They exposed a mismatch between raw integration-check hashes
and the normalized/redacted evidence consumed by gates and debugger receipts.
After using one normalization, 41 focused runtime/gate/Codex/capacity/revision
tests passed on Python 3.12. A further 67 Git/workspace/probe/SWE tests and 21
sandbox/private-Git/evidence tests passed. A fresh 27-task/four-box recovery soak
also passed in 104.689 seconds; final whole-suite checks still follow this snapshot.

## Remaining product and validation gates

- The legacy manual grill still produces a V4 runbook. Seed-assisted proposals
  preserve reviewed V5/V6 authority. The new `/grill --draft` path proposes V5 from
  human intent with fixed worker/scope/limits and exact review/approval. Semantic
  oracle adequacy and unrestricted autonomous plan construction are not established.
- Pane metadata and bounded box details can now be read from a stopped run's exact
  ledger, including by a newly opened client. Durable offline inspection is
  implemented. Exact current-plan pins and custom display groups now persist across
  clients and reorder/search worker navigation without changing authority.
  Native peer integration is implemented at explicit capability tiers; its real
  provider compatibility and live cross-pane acceptance still need their gates.
  Interactive mailbox
  commands and retained/live inbox panes now use a private immutable outbox.
  Split/grid metadata tiles retain the orchestrator composer and scope-checked
  detail selection; they do not mirror arbitrary terminal sessions. The
  owner-side mailbox and worker packet/receipt core are implemented with local
  execution, real socket/CLI, replay/export and negative-path tests.
- V5/V6 imported plans support kernel gates; interactive Codex/OSS launch-policy
  acknowledgement is implemented. A real fake-Codex subprocess test covers terminal
  import through detached build/evaluation/integration and final human acceptance.
  Mixed-provider UI launch is implemented with exact manifest review and durable
  deduplicated preflights and atomic shared worker-budget admission. Integrated
  verification and unified live reservation inspection/reconciliation remain open.
  Temporary budget holds now wake automatically only for exact invocations still
  owned by the same runner, without another attempt or payment intent. Unknown,
  stale, exhausted or busy-lock cases remain explicit operator attention.
  V6 pre-intent budget deferrals now bind a distinct successor rate debit in the
  ledger and recheck the complete broker policy without refunding the old debit.
  Expired calls without that proof still fail closed. Long-window local execution
  and broker/local-ledger publication recovery are covered by focused tests;
  whole-suite integration and live provider proof remain separate gates.
  `/revise` now reviews, applies and recovers a stopped source-bound ProductV3/V4/V5
  session through the kernel revision service, with independent execution approval
  and real detached successor build/final-acceptance tests. Legacy ProductV1/V2
  source-baseline migration remains unsupported rather than fabricated.
- Local download receipts are not model loading, GPU residency, inference readiness
  or an air-gap guarantee. The narrow owned loader now has a separately approved
  one-shot inference bridge with protocol fixtures; actual models/hardware and
  automatic planning/worker handoff still need implementation or live proof.
- Public benchmark adapter protocol tests are not public coding-suite performance.
  Official pinned datasets/images and a budget-enforcing executor still need live
  environment validation; no paid trial has been authorized/run here.
- Remote worker authentication/distribution, live cloud/voice profiles, complete
  retention enforcement, multi-platform CI evidence and the owner's public license
  decision remain open. The spatial visualizer is explicitly post-v1.
  The local executor now requires a pinned placement probe for nonempty V6
  placement, separately from pool matching, and rechecks before launch. A remote
  label cannot silently execute a remote-required task locally; region stays
  unproven. This closes a reproduced admission defect, not remote distribution.
  A separate read-only remote terminal monitor now inspects an already-running
  supervisor through the pinned SSH bridge, with overview, paginated box selection,
  bounded evidence and explicit stale-state handling. Local bridge and keyboard
  tests back this inspection path; actual remote-host acceptance remains open.
  The SSH bridge now relays explicitly allowed owner-side mailbox observation,
  inbox and message operations. An embeddable observe/review/send helper binds
  exact target-profile and lease scope, while the existing remote kernel enforces
  deduplication/fencing. Lost post replies remain unknown until explicit operator
  reconciliation. This is not autonomous cross-host worker authentication.
  A separate private content-free RPC audit now records validated remote reads
  and mutations, including monitor polling, with local latency and protocol byte
  measurements. Offline pagination/aggregation cannot clear mutation uncertainty
  or establish provider billing. Unknown measurements remain null. The fixed
  record ceiling stops new calls; complete archival/retention remains open.
- Recorded completed-run evidence/code recovery does not close operational restore
  or retention. Independent archive reconstruction cannot authorize reuse of old
  leases, credentials, budgets, remote effects, or removal of the original stores.
- Same-run VCS relationships support owner review and prospective rerun sets.
  Explicit GitHub observations now retain matching/moved refs, PR identity, checks
  and reviews, with durable intent and no fallback to old green after failure.
  These do not transfer green gates, infer push authorship or approval policy, or
  automatically watch remote changes. Full §19.6 remains open; see [VCS scope](vcs-lineage.md).

This ledger is a progress record, not a claim that the entire specification is
implemented. Current limitations remain open until backed by the named gate.
