# Frozen foundation verification

This record applies to commit
`c8fb3d1163f0be78c1257349a8d9650cde77fa42` on `codex/v0-full-pass`.
Later work on `codex/v0-next` must pass its own checks; it does not inherit these
results merely because it builds on that commit.

## Exact-commit checks

| Check | Result |
| --- | --- |
| Complete local suite, Python 3.9 / Textual 8.2.8 | 551 tests passed, 389.676 seconds |
| Complete local suite, Python 3.12 | 551 tests passed, 353.707 seconds |
| Runtime soak | Four eight-box runs; 68 tasks; two verifier restarts; all completed |
| Soak ledger / artifacts | 2,281 events; 212 artifacts; exact exported replay for every run |
| Source preservation | Source checkouts unchanged in every soak run |
| Soak duration | 355.241 seconds total |
| Packaging | Wheel and source distribution built; source distribution independently rebuilt into a wheel |
| Fresh wheel installation | Python 3.12, base imports, version/help and line CLI boot/help/quit passed |
| Installed optional UI | TUI/graph extras installed; real PTY boot, slash palette, Escape and Ctrl+C exited successfully without traceback |
| Git hygiene | Original checkout/main untouched; foundation checkout clean after commit |

The exact soak trials were:

| Trial | Verifier restarted | Tasks | Events | Artifacts | Seconds |
| --- | --- | --- | --- | --- | --- |
| 0 | No | 17 | 565 | 53 | 82.257 |
| 1 | Yes | 17 | 566 | 53 | 88.983 |
| 2 | No | 17 | 576 | 53 | 91.400 |
| 3 | Yes | 17 | 574 | 53 | 92.601 |

## Remote portability result

[Actions run 34178233154](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34178233154)
passed all three Ubuntu jobs (Python 3.9, 3.12 and 3.13) and failed all three
macOS jobs. The common failure was the real sandboxed Git inspection test:
Apple's Git shim could not read the CI runner's selected Xcode directory under
the sandbox policy. Therefore this commit does **not** have an all-green OS
matrix. The narrowly scoped runtime-path fix and a fresh matrix belong to the
next checkpoint, not to this result.

## Evidence boundaries

The tests used disposable repositories, controlled subprocesses, local protocol
fixtures and synthetic workloads. They did not spend on hosted models, download
or load a public model, connect to a real remote SSH host, deploy cloud resources,
or run an official public coding benchmark campaign. Those remain separate
owner-authorized compatibility and performance gates.

Homebrew resources were hash-checked and the formula syntax checked, but Homebrew
was not installed on this machine; no brew install/test/audit was performed. No
release tag, public tap or license choice was published.

The sandbox is also not complete hostile-descendant containment. A controlled
macOS fixture demonstrated that a child can leave its process group and continue
briefly after its parent exits. It could modify its old worker checkout, but
could not modify the frozen evaluator, captured candidate, integration checkout
or original source in that fixture. Reuse of a task checkout on a later retry
is consequently a residual interference risk. A process-group kill is not an
OS-enforced guarantee over every possible descendant.

See `full-implementation-progress.md` for the broader implementation ledger.
These are verified foundation results, not a claim that the entire specification
or every provider/workflow is complete.
