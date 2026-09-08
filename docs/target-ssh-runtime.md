# Authenticated SSH runtime observation

`camol target observe-ssh` connects an adopted SSH target to a measured remote
supervisor runtime, through the existing pinned SSH bridge and authenticated
supervisor socket. It does **not** turn that supervisor into a distributed worker,
bootstrap a daemon, transfer a checkout, provision a machine or grant a lease.

## Review and use

1. Configure the existing remote supervisor and SSH bridge. The client profile
   binds host keys, a key-file reference, bridge software, the remote target-policy
   digest, remote run/plan and remote owner.
2. Explicitly add `target-local-profile` to **both** bridge and client allowed
   commands. It is supported but not in the default read-command set. Update and
   review the policy/profile digests; existing profiles gain no implicit permission.
3. Use the existing human-reviewed adoption workflow with `transport.kind = "ssh"`
   and `transport.profile_digest` equal to the exact `SSHTarget.digest()`. A changed
   profile needs new reviewed adoption, not mutation of the old generation.
4. Run `camol target observe-ssh --help`. Supply the normal exact local workspace,
   state directory, run, plan, owner, generation and adoption digest, plus
   `--ssh-profile` (reviewed profile JSON), `--request-id` and `--allow-network`.
   `--live` uses the existing local supervisor; otherwise the normal exclusive
   embedding ownership lock applies.

Python: `await harness.targets.observe_ssh(generation, ...)`, with named
`adoption_digest`, `request_id`, `by`, `ssh_profile` (dict), `state_dir`,
`allow_network=True`, and optional `ttl_seconds`. Standalone
`camol remote request --command target-local-profile` can read the remote profile
under the same policies but does not record a local adoption observation.

No login, private-key ingestion, automatic account discovery or model call is
added. OpenSSH reads its approved key reference. Camol stores no key bytes;
reviewed host and credential-file **references** remain visible in event history.
Profiles/reports are checked for protected material before publication.

## What the evidence proves

`TARGET_SSH_RUNTIME_OBSERVED` binds local run/plan/owner and adopted generation to
the exact SSH profile, remote run/plan/policy, nonce-bound response/request identity,
response digest, and measured hostname/OS/architecture/Python/Camol file identities.
The remote supervisor's software must equal the reviewed bridge software. A bridge
and daemon using different Python/package bytes are rejected; measuring the bridge
does not substitute for measuring the daemon.

Existing transport safeguards include strict host-key checking, no agent forwarding
or shell interpolation, bounded framing, and content-free RPC usage records.
Protocol bytes and durations are not network bills, tokens or reserved capacity.
This authenticates the configured SSH endpoint and records supervisor
self-observation. It does not attest software trustworthiness, hardware, GCP/provider
ownership, available CPU/GPU resources, sandbox/account readiness or reserved slots.
Target `readiness` stays `unproven`; execution/deletion authority stays false.

`camol target inspect` includes `ssh_runtime_observation`. Replay/export retain and
validate scope, response/runtime identities, owner, times and authority limits.
TTL is an integer 1–300 seconds; the SSH request has a 20-second operational deadline.
There is no automatic refresh. Displaying a report does not extend its expiry.

## Races, cancellation and history

The supervisor permits one SSH observation at a time while ordinary status/drain
controls remain available. The Python registry has a per-instance busy guard;
separate registry instances still rely on kernel append fencing. Current
owner/run/plan/adoption and supervisor shutdown state are rechecked after transport.
Final append compares the current event cursor. Retirement, terminal state or plan
changes cannot publish a late report.

Cancellation settles the SSH client's existing local process/pipe cleanup and
cannot publish later. It issues no remote stop: the remote read may finish after
disconnection. RPC failure/uncertainty remains recorded without inventing a kernel
observation. There is no automatic retry.

An exact existing request returns its original historical report—even after expiry
or retirement—without reconnecting or requiring new network opt-in. Changed arguments
are rejected. A lost append response can therefore be retried without remeasuring
or fabricating freshness. SSH observation history is bounded to 1,024 records per
run, separately from local observations; automatic archival/deletion is not added.

Tests use real bridge subprocesses, supervisor sockets and a child CLI with a fake
SSH executable, not an external machine. Live remote-host acceptance, target-side
worker admission, source/artifact transfer, dispatch, fencing and trusted result
reduction remain unfinished distributed-execution requirements.
