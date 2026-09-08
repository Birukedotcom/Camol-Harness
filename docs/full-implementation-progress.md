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

The user supplied tmax and smux as pane/delegation references. The pinned source
review and command-by-command implementation status are in
[pane orchestration](pane-orchestration.md). A shared metadata-only `/overview`
and standalone command now expose boxes, task dependencies and attention. The
current-run searchable switcher is now implemented with grouped metadata, paginated
keyboard selection and stale-plan rejection. Tiled layouts, custom groups/pins and
the durable scoped message bridge remain implementation gates; adding a visual
pane never creates execution authority.

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
| Box inspection and build workflow | In progress | Overview and exact scoped switcher; read-only detail views. Durable offline box reads, custom groups/pins, split/grid layouts and scoped messaging remain open |
| Capacity / heterogeneous N boxes | Implemented; integration ongoing | Shared broker, fresh observed/owner-declared pools, suspect reservations, real two-run single-slot fairness/cancellation and broker-cursor wake; V6 |
| Watchers and correctable evidence | Local scheduled source implemented | Durable approved polling, normalized JSONL source, daemon restart, interleaving/expiry/cursor/cancellation tests; live cloud source adapters pending |
| Repository dependency graph | Implemented; adversarial tests ongoing | Safe static Python/packaging/npm/Docker/runbook scans, evidence-linked impact/path/cycle queries; JSON/DOT/GraphML |
| Local model lifecycle | Download foundation; owned loader in next wave | Exact owner manifests and pinned bytes; one-shot owned llama.cpp lifecycle with local protocol fixtures. No actual model/inference or planner/worker handoff proof |
| Evaluator compiler / benchmark campaigns | Core protocols and offline SWE-bench adapter implemented | Explicit V5 mapping; pinned 3-arm campaign, real gold/no-op fixtures, crash/cleanup reconciliation, protected official-grader protocol; live public trials pending |
| Remote targets / workflow profiles | SSH control attachment in next wave | Pinned host/bridge/target/run control with durable uncertain outcomes; no distributed worker adoption, provisioning or live cloud/voice proof |
| Public packaging / portability | In progress | Wheel/sdist built; independent Python 3.12 wheel install+line CLI passed; Homebrew resource hashes verified; OS CI/license/retention remain |
| Extended final verification | Pending | Repeated DAGs, failure/refinement, process interruption, package and UI tests |

## Review findings repaired (final integration verification pending)

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
- Current pane metadata can be read from a stopped run's ledger, but box details
  still use the supervisor or this client's stale cache. Durable offline box
  inspection, task-qualified dependency evidence/selection, custom pins/groups,
  split/grid layouts and scoped durable box communication still need their gates.
- V5/V6 imported plans support kernel gates; interactive Codex/OSS launch-policy
  acknowledgement is implemented. A real fake-Codex subprocess test covers terminal
  import through detached build/evaluation/integration and final human acceptance.
  Mixed-provider UI launch is implemented with exact manifest review and durable
  deduplicated preflights and atomic shared worker-budget admission. Integrated
  verification, automatic wake after a temporary budget hold, and unified live
  reservation inspection/reconciliation remain open.
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

This ledger is a progress record, not a claim that the entire specification is
implemented. Current limitations remain open until backed by the named gate.
