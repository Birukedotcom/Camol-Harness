# Supervisor and detachable control

Status: implemented in V0 for one local control-plane process and Unix-domain clients.

Camol's terminal process is a client. Bare `camol` starts or reattaches through the
same protocol; `camol start` launches a non-interactive session, waits for
an authenticated control socket, prints its PID and returns. The supervisor then owns
the SQLite event stream, scheduling, workers, process reconciliation, and shutdown.
Closing the invoking terminal does not stop it. A completed or blocked supervisor
also remains alive and inspectable until `/stop` or `camol ctl stop`.

## Start and attach

The source repository must be clean and the state directory must be outside both the
repository and its Git common directory.

```bash
python3 -m camol start RUNBOOK --workspace REPOSITORY --state-dir STATE_DIRECTORY
python3 -m camol ctl status --state-dir STATE_DIRECTORY
python3 -m camol ctl boxes --state-dir STATE_DIRECTORY
python3 -m camol ctl approve --state-dir STATE_DIRECTORY --by HUMAN_ID
```

Omit `--approve-by` to preserve the required human plan gate. Supplying it to `start`
is the non-interactive equivalent of approving the exact stored plan digest.

The control files are under `STATE_DIRECTORY/control`:

| File | Meaning |
|---|---|
| `camol.sock` | local Unix-domain control socket, mode `0600` (normally here) |
| `control.token` | random bearer token, created by the supervisor, mode `0600` |
| `leader.lock` | advisory single-writer lock |
| `leader.pid` | current supervisor PID, mode `0600` |
| `supervisor.log` | detached process output |

If the full socket path would exceed the platform's AF_UNIX limit, Camol uses
`/tmp/camol-UID-HASH/camol.sock` in an owner-only `0700` directory. The hash binds the
absolute state directory; the control token and every authoritative record stay under
the state directory.

The socket protocol is versioned JSON-lines and token-authenticated. V1 preserves
`camol ctl`. V2 adds request IDs, strict parameter objects, the exact plan/runbook,
bounded event reads after a sequence cursor (including bounded long polling), and
bounded read-only per-box views. V0 deliberately
does not expose it over a network. Remote workers will need mutually authenticated,
lease-bound transport rather than forwarding this local bearer token.

## Lifecycle controls

```bash
python3 -m camol ctl drain --state-dir STATE_DIRECTORY
python3 -m camol ctl resume --state-dir STATE_DIRECTORY
python3 -m camol ctl stop --state-dir STATE_DIRECTORY
python3 -m camol ctl force-stop --state-dir STATE_DIRECTORY --by HUMAN_ID
```

`drain` admits no new work and waits for active work to reach a safe boundary.
`resume` re-enables scheduling. `stop` is graceful drain-then-exit. `force-stop` is an
explicit destructive control action: it terminates proven process groups, captures a
content-addressed salvage receipt for every active task worktree, persists the receipt,
then revokes each lease fence. It does not remove the worktree.

## Crash and replay rules

During active execution the runner periodically heartbeats and obtains fresh admission
proof. `LEASE_AUTHORIZATION_REFRESHED` extends the effective expiry without changing
the original packet's fence digest or lease epoch. Every refresh preserves the exact
authority, sandbox, evaluator, runtime, workspace identity, and reserved budget; only
fresh observations and the workspace's current dirty state may change. The receipt
cannot outlive any required probe or provider capability. An expired active lease
cannot be silently revived. Failed refresh cancels its process, retains salvage, and
creates a typed wait instead of leaving an idle scheduler labeled running.

Unexpected driver errors remain visible as `mode=operator_attention` and a redacted
`last_error` in supervisor status. The control socket remains usable for inspection
and explicit resume or stop.

Every agent subprocess runs in its own process group and has a packet-bound invocation
record containing PID, PGID, OS start fingerprint, arguments digest, policy digest,
and terminal state. On restart:

- a dead invocation is marked `orphan_dead`; a packet-bound provider result can be
  consumed without issuing another provider request;
- a live invocation places the supervisor in `orphaned`; `resume` is refused;
- `force-stop` rechecks PID, PGID, and process-start identity immediately before
  signalling the group;
- an unfinished external effect becomes `EFFECT_UNKNOWN`, never silently failed;
- an unknown effect needs provider readback before it can become confirmed or
  rejected, and its idempotency key never authorizes a blind duplicate request.

Remote-effect intent is persisted before the external call. Its raw body is never in
the event stream; an unredacted canonical digest preserves identity while logged
targets and later outcome/readback data are redacted and digest-only.

## Current boundary

The supervisor is the intended sole writer for a state directory, but V0's historical
one-shot mutation commands are not yet locked out at the filesystem boundary. Do not
run `camol run`, `camol init`, or `camol approve` against the same database while a
supervisor owns it. The local lock, authenticated control path, recovery records, and
fenced event transitions are implemented; remote control-plane availability and
distributed consensus are later work.
