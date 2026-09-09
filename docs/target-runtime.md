# Local target runtime observations

The target registry now has a real local software/host observation path. It measures
the current Camol/Python identity, hostname, OS and architecture, then records an
owner-reviewed target-generation binding and observation interval. This is useful
evidence for future target admission, not a replacement for existing task-specific
readiness checks or proof of remote host identity.

## Commands

`camol target local-profile` reads this Camol process's installed software and host
identity. It requires no run, writes no target event, opens no account or network
connection, and executes no workspace command. The result's canonical `digest` is
the value to review as a local descriptor's `transport.profile_digest`; its transport
kind must be `local`. The descriptor still goes through the explicit
[target adoption workflow](target-adoption.md).

For a running supervisor, use `camol target local-profile --live --state-dir DIR
--run-id ID --plan-digest DIGEST`. This measures the **supervisor host/runtime**,
not the CLI client's interpreter. Supplying those scope arguments without `--live`
is rejected rather than silently measuring a different context. No SSH allowlist
extension or distributed target discovery is introduced.

After adoption, `camol target observe-local` takes the same run/workspace/owner/plan
arguments as target adoption, plus `--generation`, `--adoption-digest`, `--request-id`
and an optional `--ttl-seconds` (integer 1–300, default 300). `--live` uses the
existing supervisor; otherwise the normal exclusive embedding owner is used.
Python callers use `harness.targets.observe_local(...)` with the same named fields.
Use actual reviewed IDs and digests, not the placeholder names above.

`camol target inspect` includes the latest recorded runtime report for each target
generation. It is retained evidence with explicit `started_at`, `finished_at` and
`expires_at`, **not an active refresh or a green readiness indicator**. All reports
remain in the event ledger/export even when a later report becomes the latest.

## Measured facts and limits

The profile uses the existing bounded installed-file measurer. It hashes the resolved
Python executable and package inventory, rejecting nonregular, oversized or changing
individual input files. Public installed software need not have private owner-only
permissions; this does not attest that its bytes are trustworthy. The receipt binds
that profile to the exact adopted run,
plan, generation and adoption digest. Changes to hostname, OS, architecture, Python
or package bytes invalidate the prior profile; review a new adoption generation.
This fingerprint is not hardware attestation, a globally unique machine identity or
proof that owner-declared cloud/provider identifiers refer to this host.

The report includes monotonic measurement duration and `os.cpu_count()` when known.
CPU count is **not** measured utilization, cgroup entitlement, reserved slots or
available capacity. Unknown count remains null. No GPU, memory, sandbox enforcement,
account/model readiness, network policy or provider spending is inferred. Target
`readiness` stays `unproven`, and execution/deletion authority stays false.

The observation is a single `TARGET_RUNTIME_OBSERVED` event. Replay validates exact
fields, digests, owner, active adoption, run/plan, TTL and timestamp ordering. An
existing request ID returns its original report after expiry or retirement without
remeasuring or extending its expiry. Changed request arguments are rejected.
History is bounded to 1,024 observations per run. New requests require an active
local adoption; local measurements cannot be used for `ssh`/`worker_tls` targets.

## Liveness, races and cancellation

Live measurements run as read-only background work, not on the control event loop.
Only one measurement may occupy the supervisor's slot at once. The requesting
control operation has a 10-second wait limit; a timeout or cancellation does not
create a report. A blocking filesystem read cannot be forcibly cancelled by Python:
the slot stays occupied until that read finishes. Its worker only reads public
identity inputs; it never accesses the kernel store or publishes a late report.
Host-level I/O stalls may still delay interpreter shutdown while its thread exits.

After the await, the supervisor checks its run/plan and shutdown state again. The
recorder revalidates the current owner/adoption and compares the freshly read run
cursor at append. Unrelated run progress is allowed; retirement, plan changes and
concurrent mutation after final validation are not. This keeps observation from
starving under ordinary heartbeat activity while preserving atomic admission of the
report. An explicit [SSH runtime observation](target-ssh-runtime.md) now covers a
pinned bridge to an existing remote supervisor. Target-side worker launch and
trusted result reduction remain separate unfinished distributed-worker requirements.
