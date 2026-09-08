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
