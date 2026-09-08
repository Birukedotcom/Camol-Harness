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
