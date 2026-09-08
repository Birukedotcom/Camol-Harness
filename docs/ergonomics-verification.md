# Ergonomics checkpoint verification

## Offline box reader full-suite checkpoint

Frozen `644a560575d68da3ecf464dc045a462213e1fa79` passed **847 tests** on
Python 3.12 in **565.622 seconds** and Python 3.9 in **626.407 seconds**.
Together with its prior fresh sdist/wheel and installed-terminal checks, this
clears that development checkpoint's local gate. `codex/v0-next` was fast-forwarded
to it without modifying the user's installed environment or original main checkout.
Later pins/groups and budget-wake changes are not covered by this result.

## Pane organization full-suite checkpoint

Frozen `9c88a9d82cd721fccd168d79ea1a171924bb0326` passed **856 tests** on
Python 3.12 in **651.788 seconds**. Its focused dual-runtime and fresh installed
package checks are recorded below. This is not a full Python 3.9 result for that
commit, nor evidence for the later budget-wake implementation.

## Pane-switcher full-suite checkpoint

Frozen `4df9a3b4037c3c3cee912408f23bdcbcceb413ee` passed the full 834-test suite
on Python 3.12 and Python 3.9. The Python 3.9 run took 761.419 seconds. These
results cover the searchable switcher and ordered terminal input, not the later
offline box reader. All execution used local/fake fixtures; no paid provider,
real model download, SSH target or cloud deployment was used.

## Earlier ergonomics checkpoint

The chronological results below retain their original checkpoint scope.

Exact checkpoint: `41d5824940cb803089194999eaa0de6285c6d22b`.

| Check | Result |
| --- | --- |
| Full local Python 3.9 / Textual 8.2.8 suite | 687 tests passed in 558.865 seconds |
| Full local Python 3.12 / Textual 8.2.8 suite | 687 tests passed in 501.264 seconds |
| Focused inference/host/CLI/history group | 51 tests passed in 27.619 seconds on Python 3.12 |
| Distribution | Built source distribution and rebuilt wheel from that source distribution |
| Fresh installed wheel with TUI/graph extras | Version/help, inference prompt-file option, bridge identity and line boot passed |
| Installed detached supervisor | Exact source-bound start/stop passed from a hostile fixture containing package/sitecustomize import traps; no trap executed and no worker launched |
| Installed terminal | Actual PTY boot, slash palette, Escape, Ctrl+C; exit zero, no traceback |
| Checkout hygiene | Checkpoint clean; original repository/main untouched |

The package smoke test loaded the installed wheel from a separate directory,
not the source checkout. Its terminal capture contained 147,678 bytes. Test
fixtures did not use paid model accounts, download or load real model weights,
attach a real SSH host, or deploy cloud infrastructure.

This checkpoint adds the human-goal V5 draft wizard, exact command/oracle risk
review, one-shot owned-model inference and bounded recent conversation history.
It is not a claim of semantic oracle adequacy, public benchmark performance,
hard containment of hostile descendants, or completion of the entire spec.

The numeric-loopback fixture repair in `a2948d9` was merged afterward. Its 13
host-lifecycle tests passed separately on both runtimes; it changes tests only,
not model-host authority. See [next-wave verification](next-wave-verification.md)
for the previous remote failures and diagnostic evidence. Whole-suite and hosted
matrix results for successor commits must be reported independently.

## Projection checkpoint: 9e5420a41ef124303a036700d2b6d87139aa4873

The unchanged default `python scripts/run_runtime_soak.py --iterations 10 --boxes 8`
passed ten trials on Python 3.12: 170 tasks, five verifier restarts, 5,636 events
and 530 artifacts. The per-trial 120-second ceiling was unchanged. All trials
preserved source and matched exported replay. Summed trial time was 745.343 seconds;
the individual results are in [ergonomics-soak.json](ergonomics-soak.json).
This is recovery/correctness evidence, not a controlled speed comparison with
the older foundation run, an arbitrary-scale guarantee or a live-provider result.

The predecessor `69a9f40c49ffcac28a0e42aa610d7a64267aef18`
[Actions run 34182478937](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34182478937)
passed four of six jobs. Ubuntu 3.12 exposed inference host-ledger read contention
and TUI persistence/navigation races. macOS 3.13 exposed a terminal-daemon stop
test racing an already-closing socket. These failures are tracked repairs, not
waived by local successes. The whole-suite/matrix gate for the integrated repairs
must be recorded against its own commit.

The projection checkpoint's
[Actions run 34182978365](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34182978365)
passed five jobs. macOS 3.12 never started: its GitHub check annotation reports
failed account payments or an Actions spending limit. It has no runner or test
steps, so it supplies no macOS 3.12 test result. No billing settings were changed.
This is an incomplete matrix, not six-platform success or a code-test failure.

## Integration checkpoint: 2dbf2a5

The full Python 3.12 suite passed 752 tests in 601.603 seconds at this exact
checkpoint, including the preflight journal, inference-contention repair and
retention inventory/CLI. The following mixed-provider launch and pane-overview
changes are not covered by that result; their focused tests and remaining
whole-suite gates must be read separately. The shared in-flight hosted-budget
race remains a required runtime repair before complete acceptance.

## Revision and pane-overview checkpoint: fc76dc889a189e118e49ba5d71bf0b78dd670941

The full Python 3.12 suite passed **808 tests in 840.307 seconds** on the frozen
checkpoint, including exact `/revise` review/apply/recover, actual detached
successor execution/final acceptance, mixed launch and read-only pane overview.
The separately developed shared worker-budget repair is not covered by that run.
No live provider, model download, SSH host or cloud deployment was exercised.

## Shared worker-budget repair (isolated development checkpoint)

Frozen checkpoint `96ffb046a6470e2d7b2132cebca8c19b3f6783cb` passed the full
Python 3.12 suite: **821 tests in 863.985 seconds**. This result includes shared
worker admission but does not cover the later switcher/input-order changes.

The repair adds atomic cross-process admission over existing immutable provider
journals, with known/unknown settlement, revision-ancestor charges, strict run/task
policies and no lock held during provider execution. A real two-process fake-Claude
barrier fixture requires overlapping execution and verifies requested reservations
of 20 + 10 cents against a 30-cent run. A separate four-process admission race
cannot allocate more than that shared allowance. No live paid provider was used.

The development adapter/usage/invocation/runtime/revision/retention group passed
86 tests on Python 3.12 (116.833 seconds) and Python 3.9 (125.124 seconds). A later
legacy-unknown accounting guard and its regression passed with all 12 budget tests
on both runtimes (0.770 / 0.871 seconds). An earlier stricter ancestry check rejected
macOS's normal `/var` alias in direct API fixtures; canonical ancestry validation
repaired it while retaining linked-state/lock/journal rejection tests.

These focused results are not a substitute for the frozen successor's full suite
and installed-package checks. Temporary holds currently require explicit resume;
automatic wake and unified live reservation inspection/reconciliation remain
documented product gaps. Provider-side hard billing guarantees remain unavailable.

## Searchable pane and terminal-input wave

The current-run `/switch`/Alt+B picker adds metadata filtering, orchestrator/
attention/worker grouping, 50-row pages and exact session/run/plan/pool selection.
The keyboard tests cover 77 boxes, narrow resize, no matches, draft preservation,
Ctrl+C, a worker named `orchestrator`, and stale plan/pool rejection. No picker
operation grants execution authority or probes a provider.

The real PTY fixture sends `/switch` and Enter as one byte burst, selects a worker,
checks durable view selection without approval or worker state creation, then
detaches with Ctrl+C and exit zero. It reproduced a real race: app-priority Enter
submitted `/s` before the queued `witch` reached TextArea. Submission/newline now
run in the composer's ordered key queue. The test was not fixed by adding typing
delays. Background UI callbacks also enter the app message pump so modal composition
has its active-app context; a threaded `/login` regression covers the same repair.
Normal replies/denials can no longer remain hidden behind the selected worker.

The development projection/controller/TUI/real-PTY/detached-product group passed
81 tests on Python 3.12 (62.093 seconds) and Python 3.9 (69.599 seconds). Subsequent
focused checks cover the final title/page labels and multiline/shortcut guards.
The frozen successor still needs its own full-suite and installed-package gates.
Pins, custom groups, split/grid layouts and durable scoped messaging remain open.

## Pane overview checkpoint: 8234efb

The focused controller/TUI/overview group passed 60 tests on both Python 3.9 and
3.12. [Actions run 34185513122](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34185513122)
started none of its six jobs; the inspected GitHub check again reports account
payments/spending-limit failure. There are no test steps or hosted verification
results for this checkpoint. Local progress does not clear that external gate.

## Exact-run offline box reader (development checkpoint)

The new reader, embedding API, CLI, controller, supervisor, overview, switcher,
product flow, TUI and actual terminal group passes **117 tests** on Python 3.12
in **73.062 seconds** and Python 3.9 in **81.190 seconds**. Twelve new reader
tests cover exact run selection, cold/live cuts, no missing-state initialization,
strict metadata/JSON, byte/row bounds, symlinks/FIFOs, producer and artifact hash
checks, raw withholding, terminal-control escaping, cursor/reassignment/actor-name
collisions, stale-client scope, corrupt-ledger denial and a real local build whose
runner and store are closed before inspection. The embedding API adds an exact
online/offline snapshot parity test.

A fresh source distribution was built, a wheel rebuilt from that distribution,
then installed with `[tui,graph]` into a new Python 3.12 environment outside the
source checkout. **15 checks passed in 15.695 seconds** against that installed
package: the twelve reader tests, embedding parity and two actual PTY tests for
boot, searchable box selection and Ctrl+C. The child terminal processes ran from
the separate package-check directory and imported the installed package, not the
source tree. This does not cover real accounts, models, remote hosts or cloud.

The full-suite result above belongs to predecessor `4df9a3b`; the offline-reader
successor still requires its own full-suite run. Durable messaging, pins/groups
and tiled layouts are not implemented by this observation-only bridge.

## Persistent pane organization (successor development checkpoint)

`/pin` and `/group` now persist owner-private, exact-current-plan display settings
without changing the session schema or kernel authority. The organization,
switcher, controller, session, TUI, actual terminal and offline inspection group
passed **99 tests** on Python 3.12 (**31.798 seconds**) and Python 3.9
(**33.962 seconds**). Eight new organization tests cover idempotence, ordering,
two-client updates, restart, scope changes, unknown targets, malformed/linked/FIFO
records, bounds, lock contention and unchanged execution authority. A new composer
test submits both commands, checks the bottom pin, searches a literal multiword
group, selects its exact box and preserves the unsent draft.

The final corruption-path test caught the footer interpreting a bracketed warning
as Rich markup. It now renders literal text: damaged preferences show an explicit
warning without closing the client or changing its selected box. Final focused
checks passed on both runtimes (the ten-test Python 3.9 check took 1.151 seconds).

A fresh wheel rebuilt from the source distribution was installed with TUI/graph
extras outside the source checkout. **11 installed-package checks passed in 2.626
seconds**, including all eight organization tests, the composer/corrupt-footer
test and two actual PTY tests for boot, box search and Ctrl+C. No model, provider
account or remote host was used. Whole-suite runs at frozen predecessor
`644a560575d68da3ecf464dc045a462213e1fa79` remain separate pending evidence;
they do not cover this successor. Tiling and durable messaging remain open.

## Within-lease provider-budget waits (development checkpoint)

The final budget-wait, shared admission, native Claude/Codex adapter and usage
group passed **52 tests** on Python 3.12 (**23.145 seconds**) and Python 3.9
(**24.620 seconds**). Seven new tests cover replay/clear identity, stale/self/foreign
subjects, exact physical invocation ownership, cancellation, recoverable versus
known-exhausted holds, exact pending ceilings and a real local N-box execution
loop with fixture budget reservations. That fixture completes with one attempt
per task, no duplicate invocation IDs, matching replay and visible wait metadata.
It uses ProcessAgentAdapter with injected budget reservations, not a paid model.

An earlier broader 93-test runner/resilience/adapter/view group passed on both
runtimes before the final exact-intent and physical-invocation hardening. A
53-test capacity/UI/actual-terminal group also passed on Python 3.12. These are
bounded integration checks, not a substitute for the final frozen full suite.

The final source distribution was rebuilt into a wheel and installed outside the
source checkout. **19 installed budget/admission checks passed in 14.725 seconds**,
including the multi-process reservation race and full local wake-up fixture.
The initial ad-hoc stdin-based package test could not bootstrap multiprocessing
children; rerunning from an actual guarded script fixed the test launcher. No
product assertion was waived. No live provider/account/cloud request was used.

Pending waits preserve the current lease and get no payment intent until fresh
atomic admission succeeds. Exact invocation IDs must be published by the active
adapter after reservation; merely matching a logical turn after a restart does
not establish ownership. Unknown, unowned, busy-lock and exhausted cases remain
operator attention. V6 renewal of an expired rate debit for a proven-unlaunched
call remains a documented integration gap and must not be called completed.

## Completed predecessor suites and rate-deferral successor

Frozen pane-organization checkpoint `9c88a9d82cd721fccd168d79ea1a171924bb0326`
passed **856 tests** on Python 3.12 (**651.788 seconds**) and Python 3.9
(**623.388 seconds**). Its previously recorded fresh installed-package/PTY checks
also passed. The `codex/v0-next` installation branch was fast-forwarded to this
checkpoint; no user-installed environment was modified.

Frozen budget-wake checkpoint `ac9f2b0b20317d6cbcea1d030e63b7bd9ab4e00e`
passed **863 tests** on Python 3.12 (**582.402 seconds**). Its Python 3.9 full
suite remains a separate running check. Neither predecessor proves the successor.

The isolated `codex/v0-rate-deferral` successor closes the documented V6
pre-intent budget-wait/rate-window interaction with a new audited debit identity,
not a refund or automatic extension. The **71-test** budget/capacity/native
adapter/usage group passed on Python 3.12 (**76.413 seconds**) and Python 3.9
(**80.680 seconds**). After the final explicit budget-wait-code guard and binding
assertion, all **16 budget-wait/capacity-runtime tests** passed again on Python
3.12 (**62.834 seconds**) and Python 3.9 (**66.072 seconds**).

The new real local V6 N-box fixture holds budget past the old rate window,
completes at the human final-acceptance gate with one attempt per task, preserves
every debit and exactly replays deferrals. Negative replays reject removed
deferrals, foreign IDs, stale leases, boolean turns and changed wait digests.
A controlled-clock test proves that the old debit is not refunded before expiry,
unready supply still blocks replacement, and a broker commit followed by local
publication loss recovers exactly one replacement receipt after coordinator
reconstruction. Missing admission bindings and duplicate deferrals are denied.

Initial test development exposed fixture mistakes: its nested process wrapper
authorized twice rather than once like the native adapter, and the V6 gate model
needed a larger frozen packet envelope than the V4 demo. Those fixture inputs were
corrected before approval; no runtime evaluator or budget enforcement was bypassed.
The test's logical clock also now advances before publishing a changed supply
observation, preserving the broker's immutable observation identity rule.

A fresh source distribution was rebuilt into a wheel and installed with TUI/graph
extras in an independent virtual environment. **28 installed-package tests passed
in 62.445 seconds**, covering budget waits, all capacity-runtime tests and atomic
provider admission. The launcher asserted that Camol was imported from the
installed `site-packages`, not the source checkout; `camol --help` also passed.
No live provider credentials, paid requests, model weights or remote hosts were
used. Full-suite checks of this successor remain pending.

These remain local protocol/execution proofs. Actual hosted-model behavior, paid
benchmarks, complete box messaging/tiling, and public-release acceptance remain
open; the full goal is not complete.

## Lease-scoped mailbox development checkpoint

Predecessor evidence finished during this wave: `ac9f2b0b20317d6cbcea1d030e63b7bd9ab4e00e`
passed its Python 3.9 whole suite (**863 tests, 634.117 seconds**), in addition
to the Python 3.12 result above. Integration and `codex/v0-next` were fast-forwarded
to that checkpoint; the user's installed environment remains untouched.
`f3f3828bf424ce1cae6e4965474d9f79b1515fc3` passed its Python 3.12 whole suite
(**865 tests, 604.402 seconds**); its Python 3.9 suite remains a separate check.

The isolated `codex/v0-box-mailbox` wave adds the versioned mailbox service,
authenticated CLI/control calls, embedding API, bounded packet data, worker
consumption, rejected-send records and offline inspection. An early **60-test**
mailbox/runner/native-adapter/box-inspection/supervisor group passed on Python 3.12
(**106.992 seconds**) and Python 3.9 (**115.869 seconds**). The later twelve-test
mailbox group, including real socket/CLI and worker/export checks, passed on
Python 3.12 (**36.095 seconds**) and Python 3.9 (**38.242 seconds**), before the
final no-head-of-line-blocking case and installed-CLI import-path check.

The actual local worker receives and explicitly consumes an owner message, runs
the declared commands and reaches completion with one attempt per task. Its
intentionally malformed outbound messages produce visible rejections rather than
retries of successful build work. Replay and verified export reproduce both
message and rejection state. A separate real CLI subprocess observes an active
box, posts through the private socket, retries the same request, reads one inbox
record, and is denied after force-stop. That check does not claim its queued
message was consumed; the worker/export fixture proves consumption separately.

Adversarial checks cover missing/changed observations, foreign lease/plan/cursor,
worker impersonation, wrong-recipient acknowledgments, event actor tampering,
missing/duplicate delivery, expiry/late receipts, bounded pending queues, invalid
message kinds/control characters, reassignment and context-budget backlogs.
The archive test initially treated the verifier's `(manifest, events)` return as
a dictionary; its caller was corrected and now explicitly replays the verified
events. No product check was waived.

See [mailbox boundaries](box-mailbox.md) for remaining TUI, fresh peer-tool,
remote and delegation integration. This is not a full V0 completion claim.

The final **13 mailbox tests** passed on Python 3.12 (**38.401 seconds**) and
Python 3.9 (**40.811 seconds**), including the no-head-of-line-blocking packet
selection case and subprocess import selection tied to the loaded Camol package.

A source distribution rebuilt into a wheel was installed with TUI/graph extras
in an independent environment. **25 installed-package mailbox/box-inspection
checks passed in 47.423 seconds**, including the real CLI socket test and actual
worker/export test. The parent, supervisor and CLI subprocess imported installed
Camol rather than the source checkout. This checkpoint used only temporary local
repositories and deterministic agents: no paid model, credentials login, live
remote host or downloaded model weights. Whole-suite successor verification is
still pending and must not be inferred from predecessor results.

## Interactive mailbox and immutable send recovery

The frozen rate-deferral checkpoint `f3f3828bf424ce1cae6e4965474d9f79b1515fc3`
also passed its Python 3.9 whole suite: **865 tests, 677.755 seconds**. Both
integration and `codex/v0-next` were fast-forwarded to it after its two full-suite
and installed-package gates. The user's installed environment was not changed.
The frozen mailbox-core checkpoint `d7e12221c48fb936a07ed58326e546a78681cf37`
passed **878 tests on Python 3.12 in 678.028 seconds**. Its Python 3.9 whole-suite
check remains separate and pending at this checkpoint.

The isolated `codex/v0-mailbox-ui` wave adds `/message`, `/reply`, `/inbox`,
`/outbox`, explicit exact-intent retry, a live/retained inbox pane and private
immutable send records. Plan amendments reject old requests instead of silently
retargeting them. A response lost after the real mailbox commit is recovered by
a reconstructed client without another observation or duplicate message.

The **84-test** mailbox-UI/TUI/controller/session/real-terminal group passed on
Python 3.12 (**46.735 seconds**) and Python 3.9 (**49.819 seconds**). It includes
foreign/corrupt/linked records, changed plan and reply lease, cancellation before
and after saving, malformed live scope, retained offline inspection, literal
message markup, draft preservation, foreign acceptance identity and bounded
storage. New intents reserve space for eventual acceptance files even when their
responses are lost; saturation does not prevent inspection or exact retry.

Further adversarial review found that an array/object message kind could raise
an unexpected `TypeError` rather than a rejected-send `ValueError`. The core now
validates that kind before set membership and validates acknowledgment IDs before
dictionary lookup. The **13-test mailbox group** passed on Python 3.12
(**38.879 seconds**) and Python 3.9 (**41.229 seconds**). After adding the final
malformed-acknowledgment replay assertions, the two affected tests passed again
on Python 3.9 (**5.155 seconds**) and in the installed-package group below.

A fresh source distribution was rebuilt into a wheel and installed with the
TUI/graph extras in a separate environment. **20 installed-package tests passed
in 35.299 seconds**: all eleven mailbox UI cases, both malformed-message/replay
cases, the composer/inbox test, and all six real-terminal/CLI tests. The parent
asserted an installed `site-packages` import; CLI child processes ran from that
package root rather than the checkout. Native-login visibility uses a fake local
provider script, not an actual account login. No paid provider, downloaded weights
or live remote host was used.

The UI successor's complete suite is still a separate running gate. Automatic
fresh peer-observation tools, remote mailbox transport, tiled layouts and reviewed
task-delegation UI remain open. This is not a completed V0 or public-release claim.

## Native tiled-monitor checkpoint

The isolated `codex/v0-tiled-monitor` successor to `fac94d1` implements client-only
focus/split/grid monitoring, while preserving the orchestrator transcript and
composer. Tiles use one metadata projection with bounded responsive pages and
the existing scope-checked selection path. They are not replicated PTYs, live
connectivity proof, or a new authority channel.

The **89-test** layout/TUI/controller/switcher/organization/real-terminal group
passed on Python 3.12 (**24.370 seconds**) and Python 3.9 (**27.486 seconds**).
Adversarial cases cover 77 projected boxes, page clamping, frozen plan changes,
corrupt ledgers, command/refresh contention, literal markup, invalidated tiles
after resize, queued input retaining its original identity across page refresh,
and preserving an unsent draft through refresh, selection, Escape and focus mode.
The existing real PTY navigation test now enters grid mode before opening the
switcher, selecting a box and exiting with Ctrl+C.

Two early test failures were fixture errors: command history legitimately changes
when `/layout` is entered, and the test attempted an invalid plan/digest pair
instead of a valid amended plan. Assertions now check the unchanged authority
fields and use a correctly rehashed amended plan. A synthetic 77-box view also
needed a matching refresh fixture rather than being replaced by the real
three-box fixture's observation. No product gate was waived.

A source distribution rebuilt into a wheel was installed with TUI/graph extras
in an independent environment. Its initial **28 installed-package checks** passed
in **4.933 seconds**. Visual inspection of the installed client's actual Textual
render found a cramped help line; the header was shortened and column sizing
adjusted to use the monitor width. The affected **13 layout/terminal tests** then
passed on Python 3.12 (**4.805 seconds**) and Python 3.9 (**5.263 seconds**).
The rebuilt final wheel passed all **28 installed-package checks again in 4.936
seconds**, including real PTY grid/switch/exit behavior. A second rendered view
confirmed the header, tiles and unsent composer were visible without overlap.
Parent and CLI child processes imported the installed package. The initial
render helper accidentally selected an old package in another environment and
failed before rendering; the verified render used the fresh installed environment.

Only local temporary fixtures and synthetic metadata were used. The 77-box test
does not claim 77 simultaneously running agents. No paid model call, actual
provider login, remote deployment or model-weight download occurred. Earlier
mailbox/UI whole-suite runs remain independent pending checks; this successor
still needs its own full-suite gate. Automatic peer tools, reviewed delegation,
remote execution, task-qualified dependency proof and public-release gates remain
open. The complete V0 goal is still active.

## Embedding peer tools and outbox path review

Completed predecessor results: mailbox core `d7e12221c48fb936a07ed58326e546a78681cf37`
passed **878 tests on Python 3.9 in 696.327 seconds**, alongside its earlier
Python 3.12 full-suite result. Mailbox UI `fac94d1` passed **890 tests on Python
3.12 in 657.031 seconds**; its Python 3.9 run remains separate. Tiled monitoring
`d8856da97f5e075c5c005ca89428839e0f645fd1` passed **897 tests on Python 3.12 in
713.347 seconds**. These are not whole-suite results for the successor below.

The isolated `codex/v0-peer-tools` wave adds an explicit Python embedding adapter
factory and turn-scoped `PeerTools`. Successful list/observe/own-inbox reads are
durable and recomputed during replay; read-before-send requires the same caller's
recorded observation. Objects close when the owning adapter returns, raises or is
cancelled. Unsupported controls, stale/revoked turns, expired observations,
changed targets, impersonated actors, changed results/digests/types, duplicate
requests and bounded-read exhaustion are rejected. Reads preserve task state and
token counters; no model call occurs inside the tools.

An initial eight-test peer group passed in **26.571 seconds**. The expanded
**52-test** peer/mailbox/UI/API/inspection group passed on Python 3.12
(**116.010 seconds**) and Python 3.9 (**122.028 seconds**). After adding the public
factory path, **15 peer/API tests** passed on Python 3.12 (**38.699 seconds**) and
Python 3.9 (**41.097 seconds**). Final cancellation, false-valued callable factory
and outbox traversal assertions passed in a three-test Python 3.9 check
(**15.305 seconds**) and the installed check below.

The actual local subprocess build uses an explicitly supplied embedding adapter
wrapper to observe peers during its owned turns, finishes with one attempt per
task, closes every tool object and reproduces the read history in verified export
and offline box inspection. A separate public `Harness(adapter_factory=...)`
build verifies the factory survives runner preparation and that a legitimate
callable with a false boolean value is still used. Cross-worker message tests
separately prove exact worker sender/recipient bindings and queued-versus-consumed
semantics; the build fixture does not claim autonomous hosted-model tool use.

Review also found that the broad logical identifier validator permits slash
characters, while private outbox request IDs are filenames. Outbox reads and
writes now require a single filename component before touching a record; tests
reject traversal and slash/backslash forms even when the rest of the intent is
valid. Native UI-generated UUID request IDs remain compatible.

A fresh source distribution rebuilt into a wheel was installed with TUI/graph
extras in an independent environment. **21 installed-package tests passed in
67.618 seconds**, covering all peer tests, all mailbox UI cases and the public
embedding build/approval/export/reopen case. The launcher asserted an installed
`site-packages` import. No paid provider request, real account login, downloaded
model weights or remote host was used. Complete successor suites remain a separate
gate. Native CLI tool transport, failed-tool-attempt telemetry and the broader
remaining spec gates are explicitly listed in [peer-tools.md](peer-tools.md).

## Peer-call telemetry checkpoint

Completed predecessor gates: mailbox UI `fac94d1` passed **890 tests on Python
3.9 in 699.406 seconds**; tiled monitor `d8856da` passed **897 tests on Python
3.9 in 699.479 seconds**. Peer tools `d4c17af` passed **906 tests on Python 3.12
in 662.252 seconds**; its Python 3.9 full suite is a separate running gate.

The isolated `codex/v0-peer-telemetry` successor records admitted tool starts and
success/error/interruption finishes, tying success to a durable matching read or
message. Lost finishes remain unknown even when the message committed. Retries
cannot erase that uncertainty, refresh a receipt or duplicate the message. The
content-free profile groups attempts and observed monotonic elapsed time by task
and operation; unmeasured durations are null, not zero or invented provider cost.

An early **42-test** peer/telemetry/inspection/diagnostic/usage group passed on
Python 3.12 (**61.596 seconds**) and Python 3.9 (**65.385 seconds**). After the
final typed-retry and correlation guards, the **17-test** peer/telemetry/diagnostic
group passed on Python 3.12 (**53.383 seconds**) and Python 3.9 (**57.103 seconds**).
A fresh source distribution rebuilt into an installed wheel passed those same
**17 tests in 68.710 seconds**, including an actual local subprocess build and
the public profile CLI. Parent and child imports selected the installed package.

Adversarial cases include logging failure before dispatch, a committed message
with a lost finish, revocation during interruption, original-error preservation,
forged result/actor/timing/reuse fields, 128 attempts across reconstructed tool
objects, Boolean-versus-integer retry arguments, and invalid correlation IDs.
An intermediate profile test incorrectly selected the first sorted operation
instead of the send row; its fixture was corrected, not the product assertion.
Closed/revoked callers and malformed identities are rejected before admission and
do not append telemetry into unauthorized runs. Those transport-security logs
remain future native/remote integration work.

No paid model request, real login, model-weight download or remote host was used.
This successor still requires its own complete suites. Native peer transport,
reviewed delegation and the remaining V0 acceptance gates are not claimed complete.

## Delegation compatibility and review entry point

The isolated `codex/v0-delegation-review` successor to peer telemetry `5277220`
adds a read-only `/delegate` task/box compatibility view and an entry point to the
existing stopped-owner revision review. Declared matches are not readiness or
reserved capacity. No independent approval, automatic dispatch or live migration
mechanism was added. Main and the user's installed environment were untouched.

The initial **32-test** delegation/revision/overview group passed on Python 3.12
(**50.353 seconds**) and Python 3.9 (**53.281 seconds**). The final **9 delegation
tests** passed on Python 3.12 in **13.275 seconds**, including a real detached
successor build followed by exact human acceptance and a Textual composer check.
The expanded **34-test** group passed in a fresh Python 3.9/Textual 8.2.8
environment in **67.154 seconds**. A source distribution rebuilt into a wheel and
installed with TUI/graph extras passed **22 installed-package checks in 31.345
seconds**; these include delegation, revision commands and the public overview CLI.
The final **41-test** delegation/Textual/line-client group also passed on Python
3.12 in **31.625 seconds**.

Negative cases cover 77 declared boxes, bounded pages, busy-but-compatible workers,
missing capabilities, unmet dependencies, corrupt ledgers, duplicate/invalid
options, terminal-control text, unchanged plan/approval/source, live owner locks,
wrong approval digests and separate launch. The initial live-owner test incorrectly
mocked a nonexistent helper; it now holds a real Harness owner lock and checks the
actual denial. No product check was bypassed.

One intermediate Python 3.9 nine-test run failed to import Textual: the older
temporary environment was found missing Textual and its venv configuration, so a
new unique environment was created. The cause of that filesystem change was not
established. The successful fresh-environment result above supersedes that failed
focused check; the older running full suite is not silently treated as a passing
gate. Complete telemetry and delegation suites remain separate checkpoints.
No paid provider request, real login, model-weight download or remote host was used.

The older peer-tools Python 3.9 full run subsequently finished **906 tests in
750.942 seconds with 33 errors**. Every reported error was a missing
`textual.drivers.linux_driver` import from the damaged temporary environment.
It is a failed gate, not a passing compatibility claim. Telemetry's full Python
3.9 suite is running in the fresh environment; delegation has the successful
focused fresh-environment and installed checks recorded above.

## Local peer transport and MCP relay checkpoint

Subsequently completed predecessor gates: telemetry `5277220` passed **913 tests
on Python 3.12 in 680.149 seconds** and **913 tests on fresh Python 3.9 in 721.077
seconds**. Delegation `eff1624` completed **922 tests in 713.590 seconds with one
error** on Python 3.12. That error was a real `FileNotFoundError` race inspecting
`host.sqlite3-journal`: SQLite removed the temporary sidecar between the existence
check and `lstat`. It is not a passing gate or an environment waiver.

The isolated `codex/v0-peer-transport` successor removes that check-then-stat race:
sidecar absence is allowed, while existing symlinks, unsafe permissions and other
inspection errors remain rejected. A deterministic removal fixture and unsafe
sidecar checks cover the fix; the main database/root remain mandatory.

This successor also adds the explicitly owner-issued per-turn Unix socket and
bounded MCP stdio relay documented in [peer tools](peer-tools.md). No existing
provider profile, grant, sandbox policy or default adapter is widened. Real child
processes exercise initialization, list/observe/send, exact retries, revocation,
wrong-token rejection and clean EOF shutdown. An independent **MCP Python SDK
1.30.0** client negotiated the protocol and invoked a real tool through stdio and
the local socket. That SDK is test-only, not a Camol runtime dependency.

Early transport tests exposed a shutdown deadlock: waiting for the Python 3.12
server to close before cancelling accepted clients delayed shutdown until their
read timeout. Endpoint closure now cancels/closes owned clients before awaiting
server closure. Tests also exercise replaced socket cleanup refusal, malformed
frames, duplicate keys, response identity/type/size tampering, connection limits,
and actual reply loss after a durable operation. The latter preserves the original
result and records connection delivery as unknown, never a second message.

The initial transport/MCP/model-host group passed **27 tests on Python 3.12 in
34.625 seconds**. With the optional SDK test added, Python 3.9 ran **28 tests in
35.303 seconds, OK with one explicit SDK-only skip**. The expanded final
transport/MCP group passed **16 tests on Python 3.12 in 33.216 seconds**, including
the independent SDK. The same final **16-test** group ran on Python 3.9 in
**29.957 seconds, OK with one SDK-only skip**. A fresh source distribution rebuilt
into a wheel, installed with TUI/graph extras, passed **30 installed-package
transport/MCP/model-host tests in 35.638 seconds**, including the independent SDK.
Parent and child imports selected the installed package. Full successor suites
have been started separately; focused and installed passes do not substitute for
their results.

No hosted-model call, account login, downloaded model weights, real remote worker
or cloud deployment was used. Native Claude/Codex registration and exact frozen
sandbox/runtime grants remain separate integration gates. There is no automatic
native peer-tool support or complete-V0 claim at this checkpoint.

### Native transport OS-boundary follow-up

The separate `codex/v0-peer-sandbox` successor to `3cb1c97` adds a real macOS
Seatbelt fixture without changing product code or sandbox policy. A denied-network
worker cannot dispatch; an explicitly network-permitted worker with the exact
socket/runtime read roots reads its bound scope but cannot unlink the owner socket.
The fixture passed on Python 3.12 in **2.458 seconds** and Python 3.9 in **2.981
seconds**. This is not a Linux enforcement result, and it does not grant networking
to any existing frozen profile. The native profile/admission/runner integration
sequence is recorded in [peer tools](peer-tools.md).

The installed-wheel OS-boundary check also passed in **2.232 seconds**. Transport
commit `3cb1c97b94bd3c0d92f2ae6f3c1bb89a3fd68ff2` subsequently passed the complete
**939-test suite on Python 3.12 in 738.745 seconds**, including the independent
MCP SDK, and **939 tests on Python 3.9 in 795.326 seconds, OK with one SDK-only
skip**. The sandbox follow-up adds only the separately verified test and docs;
its product code is identical to that full-suite checkpoint. These results do
not cover the newer uncommitted native-provider integration.

## Opt-in native Codex peer checkpoint

The isolated `codex/v0-native-peers` successor adds schema3 profiles that freeze
the exact four-operation local peer policy. Legacy profiles remain unchanged.
Admission binds the trusted relay/runtime, private socket read path and capability
environment names; the runner owns and revokes each turn's endpoint. The generated
configuration is per invocation, not an account-wide edit. Claude registration
and real model-directed peer coordination remain open.

Adversarial checks exposed a Python 3.9 package-discovery failure under the real
macOS sandbox. Loading the exact trusted package file fixed it without granting
the enclosing repository/state directory. Write grants overlapping any trusted
runtime path are rejected in either direction. Retained transport contents block
launch without deletion or reuse. Cleanup failure after a successful provider
reply now retains observed usage before refusing task completion; cancellation
preserves its original evidence. Cached successful results reopen no endpoint
and reserve no new spend. Deliberately echoed capability values are absent from
retained event, database, artifact and argv data in the fixtures.

The final focused native/probe/Codex/provider/peer/admission group passed **74 tests
on Python 3.12 in 62.079 seconds** and **74 tests on Python 3.9 in 63.812 seconds,
OK with one optional installed-Codex-parser skip**. Python 3.12 included the
explicitly selected installed Codex's read-only `mcp get` parser check. This is
configuration acceptance, not server initialization or actual model tool use.
Both full suites are separate gates and were still running when this record was
written. The later installed-package test uses the imported runtime's paths for
write-overlap assertions, not a source-checkout path pretending to be installed.

Earlier failed checks were not waived: the initial Python 3.9 native sandbox
fixture exposed the real import failure above. A later missing-`--strict-config`
fixture incorrectly expected the generic waiting code to equal its underlying
probe denial. Its corrected assertion requires zero attempts/spend and the
retained `POLICY_DENIED` probe result; production admission was not relaxed.

No hosted inference, account/config edit, model download, real remote worker or
cloud deployment was performed. Exact native startup failure, shell-environment
exclusion and model-directed multi-box messaging still require live acceptance.

### Installed package and simultaneous relay follow-up

Product checkpoint `a11eb4535f81e1f91f6859e3692730d332ef6feb` was rebuilt as a
source distribution and then a wheel, installed in a fresh Python 3.12 environment
with TUI/graph extras and the test-only MCP SDK. **28 installed-package tests passed
in 37.031 seconds**, including native builds, lifecycle, Codex parsing, MCP and
the macOS socket boundary. Parent and child imported the installed package.
The artifact predates only the documentation and test-path corrections; its
product modules match this checkpoint.

A separately added three-relay fixture passed on Python 3.12 in **2.400 seconds**
and Python 3.9 in **3.118 seconds**. Each simultaneously open native invocation
uses its own endpoint and capability, lists its own caller identity, observes the
next box and sends a message. Identical local retry IDs remain caller-scoped;
retries produce three messages total, each in its correct inbox. Revoking one
lease denies its relay while another remains usable. Replay matches and all
transport parents are cleaned up. These are three actual relay subprocesses
under owner-side fixture leases, not three autonomous hosted models or a new
full-build acceptance claim.

The first fleet fixture incorrectly passed the whole audited read record as the
recipient; dispatch correctly denied it. It now extracts the unchanged inner
observation, and the MCP response-envelope documentation makes that distinction
explicit. No product permission or receipt validation was weakened.

This extra fixture was added after full-suite discovery and is verified separately;
do not include it in the already-running full-suite test count.
The same fixture also passed against the installed wheel in **2.354 seconds**.

### Complete native checkpoint results and parser-fixture correction

The native product checkpoint completed **951 tests on Python 3.9 in 787.093
seconds, OK with two skips** (optional installed-Codex parser and MCP SDK).
Python 3.12 completed **951 tests in 732.062 seconds with one failure**. This is
not a passing Python 3.12 gate. The failed repository-graph fixture assumed the
stdlib TOML parser had not previously been imported. Native configuration tests
had imported the trusted parser; Python correctly reused it without executing
the repository's shadow module. The trap sentinel remained absent.

The cold-parser attack fixture now explicitly isolates the two parser cache
entries, and an additional warm-parser fixture requires safe reuse of the trusted
stdlib parser with no sentinel execution. Graph product code is unchanged. The
corrected **9-test graph group passed on Python 3.12 in 1.686 seconds** and on
**Python 3.9 in 1.660 seconds with one stdlib-version skip**. The full Python 3.12
gate must be rerun; these focused results do not erase the earlier failure.

## Explicit Claude peer tier checkpoint

The separate `codex/v0-claude-peers` checkout adds the schema4 tier documented in
[Claude peer integration](claude-peer-integration.md). Existing Claude profiles
keep safe mode and remain peer-disabled. The new tier explicitly names restricted
settings, managed host policy, worker-visible capability variables and handshake
verification before completion, not before inference. These limitations are
included in the exact profile/launch review; no existing authority is widened.

Seven new tests passed on **Python 3.12 in 13.068 seconds** and on **Python 3.9
in 14.162 seconds**. A subsequent expanded group passed **101 tests on Python
3.12 in 80.412 seconds** and **101 tests on Python 3.9 in 88.714 seconds**, each
with one optional installed-Codex parser skip. The group includes legacy Claude,
Codex, native peers, transport, probes, profiles and launch manifests. Full-suite
and installed-package gates remain separate.

The new fixture initially imported the local-target helper from the wrong module;
after correcting that import, two build cases exposed a fixture variable collision
between the worker's final result and the relay's initialization response. Keeping
those records separate repaired the fixture without weakening the adapter's
unknown-field rejection. Failure/cleanup paths retain provider-observed usage;
the deliberately missing relay handshake refuses task completion.

Installed Claude 2.1.263 was inspected with help-only commands, including the
proposed flags. No prompt or print mode was supplied to that real executable.
This proves available option syntax, not settings enforcement, OAuth viability,
MCP initialization, model use or a pre-inference readiness guarantee. All provider
execution and capability receipts in the seven new tests are controlled fixtures.

The exact product modules at `07e8463392ae75d45cfdf4cc39525d668634d82b` were built
into an sdist, rebuilt into a wheel and installed into a fresh Python 3.12
environment with TUI/graph extras and the test-only MCP SDK. **44 installed-package
tests passed in 45.965 seconds**, including both providers' peer fixtures, cached
recovery, three-relay routing, the independent MCP SDK, and cold/warm parser
shadowing checks. Both parent and child selected the installed package. The full
Claude-successor Python 3.9 suite is running; the full Python 3.12 native-predecessor
rerun is a separate process. Neither unfinished gate is reported as passing.

The Claude predecessor full Python 3.9 run subsequently passed **960 tests in
833.316 seconds with three skips**. The native predecessor Python 3.12 rerun
stalled during Textual timer shutdown in the revision-handoff UI test; after
read-only process sampling, it was interrupted with SIGINT (exit 130). It is not
a passing full gate. The isolated revision-handoff test passed in 1.311 seconds;
that does not dismiss the shutdown race. Follow-up inspection found Textual's
worker wait converts cancellation of its caller into `WorkerCancelled`, which
the view refresh catches as ordinary supersession. A dedicated repair and
regression are required before claiming this full gate.

## Schema5 staged-startup checkpoint

The isolated `codex/v0-startup-gate` branch adds owner-controlled initialize/status
exchange before task-prompt dispatch, without changing schema4 semantics. Strict
profile/version matching, independent owned relay authentication, immediate launch
reauthorization and content-free phase/input-attempt evidence are implemented.
See [the policy and limitations](claude-peer-integration.md).

The initial 37-test startup/peer/sandbox group passed on Python 3.12 in 27.110
seconds and Python 3.9 in 29.721 seconds. Additional lifecycle tests then passed
15 tests in 11.652/12.926 seconds. The first detached-pipe regression passed on
both runtimes and exposed why process wait must be bounded together with pipe
drain. After extending that protection to staged cancellation and outer timeout,
the 38-test startup/sandbox group passed in **23.801 seconds on Python 3.12** and
**26.310 seconds on Python 3.9**. A subsequent owner-revocation test and the final
expanded/installed/full gates are separate from these counts.

An expanded command mistakenly named nonexistent `tests.test_profiles`; its
90-test runs ended with a loader error on both runtimes and are not passing gates.
The corrected group selects `tests.test_providers`, plus launch/probe contracts.
Fixture providers, local relay calls and OS sandbox tests do not establish real
Claude protocol compatibility, account readiness, hosted billing or live tools.

The corrected expanded group passed **147 tests on Python 3.12 in 110.756
seconds** (one optional installed-Codex parser skip) and **147 tests on Python
3.9 in 115.487 seconds** (that parser and the optional MCP SDK skipped). This
includes the final owner-revocation test and staged refusal/cancellation/timeout
pipe-lifetime changes. Product checkpoint: `cacfb252b0dac20b58eea0d0b3d7bb27f345c5ae`.
An initial installed-package command likewise misspelled the graph module as
`test_repo_graph`; its 46-test run ended with a loader error, not a passing gate.
The corrected command selects `test_repository_graph`.

## Preserve terminal refresh cancellation

The isolated `codex/v0-tui-cancellation` successor fixes the full-suite shutdown
race described above. Each fleet, selected-box and tiled-monitor refresh waits
through a shielded owned waiter. Worker supersession still returns harmlessly;
cancellation of the refresh's caller is re-raised after settling that waiter,
rather than being reclassified by Textual. This works on Python 3.9 without
depending on `Task.cancelling()`. No supervisor/build cancellation is introduced.

The 33-test terminal group passed on Python 3.12 in **19.050 seconds** and Python
3.9 in **20.575 seconds**. The initial actual-timer regression hung when deliberately
restoring the old wait behavior; that isolated diagnostic was terminated. It was
replaced with a bounded real-worker callback assertion: cancellation must raise,
not return normally. With the old wait injected, this regression fails in
**0.192 seconds** with `CancelledError not raised`. The repaired callback checks
all three refresh paths, while the existing supersession check remains passing.
Final focused and whole-suite/package results follow separately; this does not
retroactively make the interrupted native-predecessor full run pass.

Final focused cancellation/supersession checks passed **3 tests in 0.351 seconds
on Python 3.12** and **0.366 seconds on Python 3.9**. The startup product at
`cacfb252b0dac20b58eea0d0b3d7bb27f345c5ae` was built sdist-to-wheel and installed
with TUI/graph extras and the independent test-only MCP SDK. The corrected
installed group passed **54 tests in 85.590 seconds**, including both native
provider fixtures, actual macOS sandboxing, relay protocol checks and graph
parser shadowing. The real Codex command was used only for its read-only config
parser test; no model invocation was made.

The terminal repair product at `0cd4b3e1540ab20abc0886e5c8c6490df660466a` was
separately built sdist-to-wheel and installed into another fresh environment.
**52 installed-package tests passed in 41.156 seconds**, covering the full terminal
group and all staged-startup tests. Both package runs imported Camol from their
installed `site-packages`, not the source tree.

The canonical full suites for that combined product are now running on Python
3.12 and 3.9 with verbose names and a per-test traceback diagnostic. An initial
driver used `discover("tests")`, giving different module identities from the
repository's ordinary discovery; both runs were intentionally interrupted before
completion and restarted with `discover(".")`. Those abbreviated-discovery
attempts are not full acceptance results. The active full gates remain pending.
Both feature branches were pushed under the owner's Git identity; main and the
user's installed Camol were not modified. Native live-provider acceptance and the
remaining product/spec gates are still open.

## Model-assisted linked delegation

The isolated `codex/v0-delegation-proposals` successor adds `/delegate --propose`
with an explicit reviewed successor seed, reason and goal. It uses one disclosed
no-tools planner call, binds stopped-parent state before the call and checks it
again under the owner lock before publishing the impact review. The parent
session and approval remain unchanged until exact `/revise apply`; execution is
still separate. Model output cannot widen the seed's command/resource/evaluator
authority. See [the command and limits](plan-revisions.md#model-assisted-delegation-revisions).

The existing 47-test proposal/revision/delegation group passed on Python 3.12 in
85.915 seconds. The first new eight-test run had two fixture defects: it expected
`failed` for a cancelled planning call, and queried a nonexistent `gate` control
command instead of the existing `acceptance.pending_gates` projection. Neither
required weakening product behavior. The corrected eight-test group passed in
25.446 seconds on Python 3.12 and 26.701 seconds on Python 3.9.

The final **13-test** group passed in **35.631 seconds on Python 3.12** and
**37.055 seconds on Python 3.9**. It covers actual terminal submission from a box,
durable review recovery after client restart without a new call, live-owner and
dirty-source refusal before planning, exact source-cursor races, changed seeds,
authority escalation, invalid effect policy and cancellation both before and
after model response. The model itself is controlled: no paid account was used.
One real detached successor build traverses exact revision approval, human task
gates and final acceptance, leaves source unchanged and replays both linked runs.
Existing full-suite gates are for the predecessor product, not this new feature;
expanded and fresh installed-package results remain separate.

The expanded predecessor-fixture group passed **57 tests in 123.878 seconds on
Python 3.12** and **130.088 seconds on Python 3.9**. The final three preflight
checks were added afterward and are covered by the 13-test result above, not
included retroactively in 57. Product commit
`af24c97a2e5db20022f4b2eaa1b1d346b6bc035f` was built sdist-to-wheel and installed
with TUI/graph extras into a fresh environment. **19 installed-package tests
passed in 46.648 seconds**, including the terminal proposal and real detached
successor build. Both changed product modules match the installed files byte for
byte; no existing user installation was upgraded.

An additional final-journal fault test passed separately on Python 3.12 in
**1.181 seconds** and Python 3.9 in **1.264 seconds**. If the final proposal-journal
write fails after the review was published, the UI reports the error, retains
observed planning usage, leaves the parent approved and unchanged, and `/revise`
recovers the exact review without another model call or implicit approval. This
extra test is not included in the earlier 13/19-test counts. The final journal
may lack its completion receipt; recovery does not fabricate that receipt.

## Full-gate findings: command waits and partially detached views

The combined startup/terminal predecessor completed **981 tests on Python 3.12
in 801.838 seconds with one error**, and **981 tests on Python 3.9 in 898.119
seconds with one error and three skips**. Neither is a passing full gate.

The Python 3.12 error came from the revision-handoff test waiting for all Textual
workers, including superseded background refreshes. Tests now wait for the exact
`commands` group, actual owned command persistence, and the existing visible
revision assertions. Command failures still propagate. A separate regression
requires command completion while an unrelated real refresh remains running.
The initial 34-test terminal group passed in 19.497 seconds on Python 3.12 and
21.479 seconds on Python 3.9 before the additional teardown repair below.

The Python 3.9 error was a product race: a late box refresh saw a retained
transcript widget after teardown had already removed its context header. Box
rendering now resolves all required widgets before changing selection or any
display state. Missing header/rail views are disposable, not execution failures.
A regression removes the header during the actual awaited box-read boundary
and requires no partial replacement or client crash. This supplements, rather
than replaces, the real-worker cancellation and supersession tests.

### Native status observations (2026-09-08)

Read-only commands observed Codex CLI 0.146.0 reporting ChatGPT authentication and
Claude Code 2.1.263 reporting logged-in `claude.ai` authentication. No credentials,
email, account identifiers or raw auth-file contents were recorded. Codex's
[`login status` reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
and [authentication guide](https://learn.chatgpt.com/docs/auth) describe this as an
authentication-mode observation, not model entitlement, quota or task readiness.
Claude's installed `auth status --help` confirmed the read-only JSON interface.
No login flow, logout, paid inference or provider configuration mutation ran.
Permission for bounded live provider builds has been requested and is not assumed.

Retention was also re-audited against SPEC §9: the inventory implementation does
not enforce expiry, archive/restore recovery data or authorize purge. Those remain
explicit implementation work, not a verification-only checkbox.

The combined repaired terminal/delegation group passed **58 tests on Python 3.12
in 66.634 seconds** and **58 tests on Python 3.9 in 72.783 seconds**. Product
checkpoint `c083aaa1a60faffe9f1cae552d4ec7b3478e0083` was then built sdist-to-wheel
and installed with TUI/graph extras into a fresh Python 3.12 environment.
**49 installed-package tests passed in 50.604 seconds**, including the complete
terminal group and all linked delegation-proposal tests. The install was selected
from `site-packages` before loading source fixtures.

The canonical full-suite rerun for this combined product passed **997 tests on
Python 3.12 in 845.603 seconds**, including the native Codex parser fixture.
Python 3.9 passed **997 tests in 919.908 seconds with three optional skips**.
The failed 981-test predecessor results remain recorded above; the repaired
checkpoint's passing run does not retroactively change them. The isolated
branch is pushed under the owner's identity; main and the user's installation
remain unchanged.

## Portable archive boundary (2026-09-08)

Recovery work exposed unsafe prerequisites in the ordinary ledger archive. A
read-only baseline experiment loaded the predecessor implementation from Git and
confirmed it accepted a symlinked archive root, a symlinked blob ancestor, and
duplicate event JSON keys. The repaired boundary rejects those fixtures, special
files and hard links; pins descriptor-relative member access; bounds bytes, event
counts and aggregate JSON structure; and writes private, exclusive archive files.
It also rejects path-bearing v2 lineage IDs before file access.

Export now derives its inventory/redaction/lineage from one captured encoded
snapshot. A caller mutation fixture cannot substitute a later artifact inventory.
The CLI projects the verified event snapshot directly instead of rereading an
archive with a potentially different manifest. No new archive format, restore
authority, raw-salvage backup, Git-object backup or deletion permission is claimed;
see [the archive contract](archive-boundary.md).

The initial expanded group passed **85 tests in 92.412 seconds on Python 3.12**
and **97.408 seconds on Python 3.9**, including runner, revision, API, CLI and
retention coverage. Further empty-identity/private-output and caller-mutation
checks were added afterward. The final archive/artifact/revision group passed
**38 tests in 14.679 seconds on Python 3.12** and **15.611 seconds on Python 3.9**.
The initial 32-test attempt had one assertion expecting a more specific error
message than the identifier validator returns; rejection itself was correct. That
fixture was corrected, not the safety predicate weakened.

Product checkpoint `d9a6fe0dd1315b3fca0e77e3afdd5d7e92ba2ac7` is isolated on
`codex/v0-archive-boundary`, committed under the owner's identity. Fresh installed
package verification is separate from these source results. The predecessor's
997-test full gates passed as recorded above. Neither the predecessor nor the smaller archive groups establishes
a full gate for this new checkpoint.

The final checkpoint was rebuilt sdist-to-wheel and installed with TUI/graph
extras in a disposable Python 3.12 environment. **50 installed-package tests
passed in 77.065 seconds**, including archive attacks, ordinary artifacts, linked
revision replay, actual local runner completion and embedding API export.
`camol` was imported from `site-packages` before adding source test fixtures, and
all three changed product modules matched their source bytes. Example runbook
validation and `git diff --check` passed. The original main checkout remains clean
at `cca1b4bdfe222db7be37621157fe21aa4bbe4517`; the user installation was not changed.

The archive-boundary checkpoint subsequently passed **1017 full-suite tests on
Python 3.12 in 1011.428 seconds**, including the explicit native Codex parser test.
That result is for the archive checkpoint, not the new recovery implementation.

## Encrypted captured-workspace recovery (2026-09-08)

The isolated `codex/v0-workspace-recovery` branch now implements explicit
`camol recovery keygen|export|verify|restore` and matching Python entry points.
The versioned encrypted capsule binds an exact raw-salvage opt-in policy and
preserves the selected base/head Git histories, tracked binary patch and captured
untracked bytes. Export first reconstructs independently; verify reconstructs in
private scratch; restore reconstructs in a new private plaintext repository.
No existing source refs, state stores or user installation are changed. Full run
recovery, cleanup/adoption authority, retention expiry and salvage-v1 coverage
gaps remain open; see [workspace recovery](workspace-recovery.md).

The first 15-test run had two fixture failures: a raw setup `git add` invoked the
deliberately configured fsmonitor before the product ran, and a missing gitlink
directory represented removal rather than a captured submodule. Setup now arms
callbacks only after capture (and asserts no sentinel exists before export), and
the gitlink fixture represents an actual present submodule. No safety predicate
was weakened. The corrected group passed in **18.915 seconds on Python 3.12** and
**20.164 seconds on Python 3.9**.

The expanded group passed **75 tests in 57.596 seconds on Python 3.12** and
**60.608 seconds on Python 3.9**. After adding strict Git object checking, the
recovery/archive/CLI group passed **49 tests in 37.565 seconds on Python 3.12** and
**39.465 seconds on Python 3.9**. After batching raw Git object reads, the 19-test
recovery group passed in **25.862 seconds on Python 3.12** and **27.393 seconds on
Python 3.9**. A separate 200-file binary regression passed in **2.083 seconds**
and **1.877 seconds**, respectively, requiring exactly two object-reading Git calls
per reconstruction, not per file.

The tests prove recovery with source/state directories unavailable, unpushed
history and dirty/untracked bytes, SHA-1/SHA-256 object formats, unrelated-ref
exclusion, wrong-key/tamper refusal before output creation, explicit policy and
content bounds, linked/control-path refusal, inert executable/symlink restoration,
callback/hostile-environment isolation, actual CLI key/export/verify/restore, and no
key or raw fixture content in output. All cryptographic keys and content were
synthetic; no user recovery archive, credential or model call was created/read.

Product `b26b7c2` plus path-alias hardening
`b5121a40dd7de1af4c3728fa679dac81096c67aa` is the current recovery checkpoint.
The strengthened path fixture passed on Python 3.12 in **0.625 seconds** and
Python 3.9 in **0.712 seconds**. Both full-suite runs for this checkpoint are active
with verbose output and bounded stall diagnostics. Their results are not yet
established, and the smaller groups do not constitute those full gates.

The final product was rebuilt sdist-to-wheel and installed with TUI, graph and
recovery extras into a fresh Python 3.12 environment. **56 installed-package tests
passed in 49.884 seconds**, covering all 20 recovery tests, the ordinary archive
boundary, linked revisions and embedding API. `camol` was loaded from
`site-packages` before source fixtures, and all three changed product modules were
byte-checked against the final source. Main remains clean and unchanged; only the
isolated branch and disposable verification environments were modified.

The workspace-recovery full runs subsequently completed: **1,037 tests in
824.518 seconds on Python 3.12** (explicit native Codex parser included), and
**1,037 tests in 901.239 seconds on Python 3.9**, with three optional skips. These
are gates for the workspace predecessor, not the later run-recovery implementation.

## Completed-run evidence/code recovery (2026-09-08)

The isolated `codex/v0-run-recovery` branch adds a replay-derived, exact-owner
review and export of the completed run's ordinary evidence, complete revision
ancestry, every recorded captured workspace and every accepted commit. Independent
reconstruction works without source/state directories. It does not restore a
mutable operational run, credentials, accounting journals, effects or authority;
see [run recovery](run-recovery.md). Ordinary redacted ledger members are not
additionally encrypted; raw code parts and the coverage manifest are encrypted.

The first focused module run passed 21 tests but included seven inadvertently
imported API tests; the fixture import was corrected, not counted as new coverage.
The aggregate-budget test initially altered a plan-bound constant and only tested
coverage mismatch. It now exhausts the same budget object between real restored
parts and requires the second reconstruction to refuse. The expanded group then
passed **72 tests in 100.964 seconds on Python 3.12** and **111.990 seconds on
Python 3.9**.

A subsequent adversarial review found that export had not pinned ordinary ledger
members through the final callback/publication window. Export now verifies the
complete ledger snapshot, pins each member through the owning descriptor, and
rechecks before publishing the sealed manifest. A regression mutates the event
file after verification while returning an unchanged review; publication refuses
without a root manifest. That test passed in **35.406 seconds** including the real
completed four-task fixture setup. Final expanded, installed-package and full
gates are recorded separately when complete.

The final expanded group passed **73 tests in 113.630 seconds on Python 3.12**
and **120.399 seconds on Python 3.9**, including the publication-race regression,
aggregate reconstruction budget, exact review, every recorded code part, complete
linked ancestry, source-independent restoration, actual CLI round trip and the
predecessor archive/recovery/revision cases. No user source was archived and no
provider/model call ran. Fresh installed-package and whole-suite verification
remain separate from these focused results.

Product checkpoint `fc855ffe04c3ce2b8b50de7fa4e0bcc75d1ee071` is pushed on
`codex/v0-run-recovery` under the owner's identity. A fresh sdist-to-wheel build
installed with TUI/graph/recovery extras passed **71 installed-package tests in
126.143 seconds**. Camol was imported from `site-packages` before source fixtures;
all five changed product modules matched the source bytes. Tests cover the new
run recovery, workspace recovery, archive boundary, revisions and embedding API.
Installed recovery help and example validation also passed. A separate wheel
installation with no extras imports the base harness and run-recovery module,
shows CLI help, and refuses encryption with an actionable dependency message.

Both full-suite runs for this product checkpoint have started with verbose logs
and per-test stall diagnostics. They are not yet passing gates. No main checkout,
user installation, account configuration or hosted model was changed by these
checks. The active acceptance ledger retains the remaining implementation and
live-validation gates.

## Repository graph read-only boundary (2026-09-08)

The next isolated branch is `codex/v0-graph-boundary`. Two initial adversarial
fixtures failed against its predecessor: PATH resolution executed a repository
`git` trap, and an outside-file hard link became Python graph evidence with
`STATIC_OBSERVED` status. All inputs were synthetic disposable repositories;
neither fixture read user secrets or ran a provider.

The repair restricts inventory Git to system plumbing with a private environment,
no allowed protocols/lazy fetch, bounded live output capture, and existing callback
overrides. File reads use the already tested descriptor-anchored reader, refusing
links/special files and rechecking accepted file identities before returning.
Normal skipped-file cases remain incomplete observations rather than fabricated
runtime readiness. See [the graph contract](repository-graph.md).

The initial repaired graph group passed **11 tests in 1.794 seconds on Python
3.12**. With directory/FIFO swaps, late content mutation, callback/environment
traps, missing Git and shared output-limit checks, the expanded group passed
**48 tests in 14.255 seconds on Python 3.12** and **14.876 seconds on Python 3.9**
(one optional stdlib TOML skip). The final protocol-permission regression brings
the group to **49 tests in 15.407 seconds on Python 3.12** and **15.994 seconds on
Python 3.9**, with the same optional skip. The group includes graph scanners,
preflight cancellation/accounting and actual graph/revision CLI coverage.

These are focused source results. Installed-package and whole-suite gates for
this new checkpoint remain separate; running predecessor suites cannot establish
them. Main and the user installation remain unchanged.

Product `f760eabd931b1a660aada014534b2363df8783da` is committed and pushed
under the owner's identity. Its fresh sdist-to-wheel installation passed
**49 installed-package tests in 14.953 seconds** with TUI/graph/recovery extras.
The installed package was selected before source fixtures and both changed
product modules matched their source bytes. Python 3.12 and 3.9 full suites for
this exact product have started with verbose logs and stall diagnostics; they
remain unverified until terminal results are recorded. The earlier run-recovery
full suites are separate jobs, not substitutes for this checkpoint's gate.

The first own-repository smoke returned one node and an incomplete observation:
this managed checkout's ordinary files have two hard links. That is the strict
policy refusing content, not a successful complete graph. This exposed the need
for an explicit reviewed source policy for managed hard-link layouts; it must not
silently weaken archive readers or pretend link ownership is known.

An independent disposable `--no-hardlinks` clone then produced **387 nodes and
2,772 edges**, with five explicit dynamic-import warnings. Running the predecessor
scanner code against that same disposable clone produced the identical snapshot
content identity. The changed reader preserves ordinary-file graph semantics;
it does not resolve dynamic imports or establish runtime readiness. No worktree
files were rewritten to remove links, and all graph databases/output remained in
the disposable verification directory.
