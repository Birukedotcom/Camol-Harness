# Next-wave verification and portability repair

## Exact checkpoint: 2dd6d5ed0c29020543e126ab1e3223894a7bf587

This checkpoint adds source-bound planning/startup, owned local model hosting
and explicit SSH control. These checks apply to that exact commit, not to later
draft-wizard or inference work.

| Check | Result |
| --- | --- |
| Complete local suite, Python 3.9 / Textual 8.2.8 | 631 tests passed, 473.485 seconds |
| Complete local suite, Python 3.12 | 631 tests passed, 432.719 seconds |
| Packaging | Wheel and source distribution built; source distribution independently rebuilt into a wheel |
| Fresh installed package | Python 3.12 with TUI/graph extras; version, help, SSH bridge identity and line boot/help/quit passed |
| Installed detached supervisor | Exact source-bound startup and stop passed from a hostile fixture containing package and sitecustomize import traps; no sentinel written and no worker launched |
| Installed real terminal | PTY boot, slash palette, Escape and Ctrl+C passed; no traceback, exit zero |
| Source hygiene | Checkpoint checkout clean; original repository/main untouched |

[Actions run 34179691259](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34179691259)
did **not** pass the full OS matrix. Ubuntu 3.9 passed. Cached Python 3.12/3.13
runtimes failed SSH identity checks; macOS jobs also failed selected-Xcode
metadata checks. The local successes above do not override those failures.

## Narrow portability repair after that checkpoint

The two failures conflated public runtime discovery/measurement with private
credential-file trust. Their repairs preserve the separate credential rules:

- SSH bridge identity now streams a bounded hash of regular installed files,
  rejects links/nonregular files and changes during measurement, and does not
  interpret runtime write bits as private-key permissions. The exact Ubuntu
  [image setup](https://github.com/actions/runner-images/blob/ubuntu24/20260831.293/images/ubuntu/scripts/build/configure-environment.sh#L35)
  makes its tool cache world-writable. The result remains an authenticated host
  self-report, not a claim of immutable execution or hardware attestation.
- macOS still requires the system developer selector and its parent to be
  root-owned. Only its bounded, selected Xcode chain beneath Applications may
  also belong to the current host owner; unknown owners and group/world-writable
  runtime directories remain rejected. No broad Applications, home or private
  var grant was added, and ambient DEVELOPER_DIR remains ignored. The runner's
  [Xcode installer](https://github.com/actions/runner-images/blob/main/images/macos/scripts/helpers/Xcode.Installer.psm1)
  installs bundles as the runner while its
  [selection helper](https://github.com/actions/runner-images/blob/main/images/macos/scripts/helpers/Xcode.Helpers.psm1)
  invokes xcode-select with sudo. Runtime discovery does not establish root-only
  package immutability.

Focused repair checks passed on Python 3.9 and 3.12: 20 sandbox tests and 21
SSH/control tests on each, including the four new public-file regressions.
These are local reproductions, not evidence of a successful rerun on hosted CI.
A subsequent exact-commit verification record must report that rerun separately.

## Scope of evidence

All checks used disposable repositories, controlled subprocesses and local
protocol fixtures. No paid model request, public model download/load, real SSH
host, cloud deployment or official coding benchmark campaign was exercised.
No release/tap/license was published. See `full-pass-verification.md` and
`full-implementation-progress.md` for earlier evidence and remaining boundaries.

## Follow-up checkpoint f782f187301819987a0807df75f333f179cd50aa

The exact Python 3.12 local suite passed 636 tests in 438.888 seconds. The exact
Python 3.9 suite ran 636 tests in 472.254 seconds with one failing SSH test: its
0.3-second whole-request timeout sometimes expired before the identity handshake,
correctly returning not-dispatched/rejected instead of the test's expected unknown.
The next repair separates delayed-hello rejection from synchronized post-dispatch
cancellation; it does not weaken the transport's unknown-effect handling.

[Actions run 34180453539](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34180453539)
passed all three Ubuntu jobs and failed all three macOS jobs. Remaining failures
included the selected Xcode's Info.plist/SharedFrameworks read paths, local model
fixture readiness deadlines, an inner-versus-outer HTTP timeout classification
race, and one intermittent real-terminal Ctrl+C timeout. They were not waived.

The follow-up adds only the exact selected Xcode Contents runtime read grant,
strengthens per-connection model credential checks, and records content-free
readback phases plus fixture-only startup/terminal diagnostics. It retains the
original readiness and terminal acceptance deadlines. The known HTTP timeout race
now reports unknown usage consistently whichever timeout fires first.

Two 12-box/25-task stress attempts hit the original 120-second trial deadline,
first alongside the two complete suites and then separately. Neither is a passing
soak. The diagnostic runner now accepts an explicit per-trial observation ceiling
and optional metadata progress; the default remains 120 seconds and kernel
authority is unchanged. A separate 600-second profiled observation showed continued
task completion, but its result and any optimization require their own evidence.

## Checkpoint 0323fe8aef9c62c25bdb55897f204e2a49dfe802

[Actions run 34181559644](https://github.com/Birukedotcom/Camol-Harness/actions/runs/34181559644)
passed all three Ubuntu jobs. Each macOS job ran 641 tests and failed the same
five local model-host fixture loads; the previous sandbox, HTTP timeout and
terminal failures did not recur in this run. Fixture-stage diagnostics identify
`HTTPServer.server_bind → socket.getfqdn` as the stalled phase, before listening.
The child imported successfully within milliseconds but remained in that DNS
lookup beyond the original five-second load deadline.

The following fixture-only repair binds its numeric loopback socket directly
and sets a fixed test-server label without DNS. A real subprocess regression
replaces `getfqdn` with an exception and still observes a loaded owned host.
All 13 host-lifecycle tests passed on Python 3.9 (13.530 seconds) and 3.12
(9.836 seconds). Production readiness checks, credentials, deadlines and listener
identity were not relaxed. Hosted CI confirmation of this repair is separate.

The extended diagnostic 12-box run completed all 25 tasks, with 1,092 events and
77 artifacts in 386.774 seconds. Source remained unchanged and archive replay
matched exactly. This used an explicit 600-second observation ceiling, not the
default 120-second soak acceptance. Profiling attributed 279.56 cumulative seconds
to repeated deep copies of growing state. A performance repair must preserve
detached snapshots, replay semantics and fresh admission/evaluation checks.
