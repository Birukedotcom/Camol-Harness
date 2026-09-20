# Optional supervisor worker gateway

The supervisor can now own the [TLS evidence listener](worker-tls.md) and pump
received reports into [box/run history](worker-import.md). It is **off by default**:
an unconfigured supervisor creates neither a network listener nor a gateway polling
task. Enabling it grants evidence reception and unverified capture only, not remote
worker execution, task completion, account access or provider spending.

## Owner setup and control

First enroll the exact current worker streams. Existing `worker-enrollment`
commands support `--live --plan-digest DIGEST` to use an already-running local
supervisor instead of taking its owner lock. The existing `--run-id` is required;
mutating commands retain `--workspace` and `--by`. A supplied workspace/database
must match the selected supervisor. There is no fallback from live control to a
second local owner. Stream preparation returns a public proposal, never its key.
Secure distribution of the private enrollment material remains owner-managed.

`camol worker-gateway configure` takes `--policy FILE`, `--approval-digest DIGEST`,
`--state-dir`, `--run-id`, `--plan-digest` and `--by`. The approval digest is the
canonical digest of the complete policy, not its file bytes or a shortened hash.
The exact approved run owner must confirm it. The V1 policy has these fields:

| Field | Meaning |
| --- | --- |
| `schema`, `schema_version` | `camol.worker_gateway`, integer `1` |
| `configuration_id` | New stable identifier; stopped configurations cannot be revived |
| `run_id`, `plan_digest` | Exact current kernel identity |
| `scopes` | 1–128 distinct, already-enrolled stream digests; not all workers by default |
| `address`, `port` | Literal bind IP and explicit port 1–65535 |
| `allow_non_loopback` | Boolean; non-loopback/wildcard listening requires explicit `true` |
| `certificate_file`, `private_key_file` | Absolute owner-controlled TLS file paths |
| `certificate_digest` | Exact SHA-256 DER leaf pin from the TLS endpoint profile |
| `max_connections`, `timeout_seconds` | 1–64 pre-handshake slots; finite 0.1–60 second exchange limit |
| `poll_seconds`, `streams_per_tick` | Integer interval 1–60 seconds; fair rotating scan of 1–16 scopes per tick |
| `expires_at` | Explicit timestamp ending this listener/capture authorization |

These scope/scan limits bound one gateway, not Camol's global N-box topology.
Certificate loading and private control-storage checks happen before configuration
publication; no socket is bound by the synchronous configure command. The background
driver then verifies current enrollment material and opens the reviewed listener.
The policy is bound to the run and plan, but is a separate owner-approved observer
configuration, not a change to worker execution grants.

`camol worker-gateway status` shows the reviewed configuration, current eligibility,
actual listener state and bounded per-stream capture errors. Configured is not the
same as listening; listening is not task readiness. All commands require exact run
and plan digests. `stop` additionally takes the exact `--configuration-id` and
`--policy-digest`. Stop disables acceptance immediately at the service boundary;
the driver then closes sockets. Use a new configuration ID for a later policy.
Ordinary supervisor shutdown cancels the gateway task and closes its connections
before closing the kernel store.

The native local control protocol exposes corresponding `worker-gateway-*` and
`worker-stream-*` commands. The CLI uses run/plan-bound V3 requests. Existing V2
embeddings remain supported; supplying both `expected_run_id` and
`expected_plan_digest` to `send_control_v2` emits a V3 request. Unknown fields,
duplicate JSON keys, mismatched owner identities and changed run/plan bindings are
rejected. These commands have **not** been added to the SSH bridge's remote command
allowlist. Run them on the supervisor host or use its local owner embedding.

## Capture and lifecycle guarantees

Every incoming frame must pass the active policy's scope allowlist and the reviewed
enrollment/lease checks. The policy is rechecked inside the enrollment service's
kernel write reservation, so expiry while waiting for that lock cannot be bypassed
by an earlier successful outer check. Listener startup also rechecks policy and
lease freshness after TLS preparation, immediately before binding.

The pump scans only the approved scopes, fairly and within its per-tick allowance.
It creates no import events for empty polls. Nonempty pages use deterministic
request IDs bound to gateway policy, stream and cursor; kernel cursor/receipt
recovery prevents duplicate capture after interruption. Automated captures use a
V2 import envelope referencing the exact current gateway policy. Replay checks
that authority and expiry; a delayed pump cannot silently fall back to a manual
owner import. Manual V1 import history remains unchanged.

On restart, a still-current approved configuration can reopen only after a current
lease and private stream/key/spool material validate. Restoring public ledger events
without those operational files does not open a listener. Missing material, stale
leases and failed capture remain visible and do not produce successful task or
billing records. Socket failures are retried at the reviewed polling interval; this
does not retry external model work. An expired policy stops its background driver;
explicit new configuration is required to resume that authorization.

Policy history is limited to 128 configurations per run. The underlying import and
delivery ceilings still apply. Gateway errors and connection counters are current
in-memory diagnostics, not durable network-usage receipts or provider bills. The
configuration, stop and captured-report events are durable and replayable.

## Remaining work

This integrates transport and report visibility with the supervisor. Target adoption,
provisioning, target-side admission/launch, source and artifact transfer, trusted turn
results and provider usage, cross-host cancellation/salvage, operational restore and
key lifecycle remain separate distributed-execution requirements. There is no claim
of a real remote model build, native account acceptance or cloud/voice validation.
