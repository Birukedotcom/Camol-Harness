# Ergonomics checkpoint verification

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
