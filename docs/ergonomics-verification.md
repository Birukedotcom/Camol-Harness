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
