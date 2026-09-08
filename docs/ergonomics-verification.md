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
