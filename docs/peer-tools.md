# Turn-scoped peer tools for embedding adapters

`PeerTools` is an owner-side Python adapter interface. It does not authenticate
an untrusted caller, expose the supervisor token, open sockets, or grant additional
sandbox paths. In-process adapters already run inside the trusted harness process;
this object is not an OS isolation boundary. Native Claude/Codex/process worker
subprocesses do **not** automatically receive it.

An embedding adapter opts in with the literal class/instance attribute
`supports_peer_tools = True`. During `execute_turn`, `adapter.peer_tools` provides:

Supply a trusted factory as `Harness(..., adapter_factory=factory)` or
`HarnessRunner(..., adapter_factory=factory)`. It receives the same kind,
workspace/run and frozen sandbox/artifact arguments as `create_agent_adapter`;
a wrapper can delegate to that bundled factory. The factory is an explicit Python
owner choice, never imported from runbook text or a model response, and is not
persisted into a session. It remains installed when `Harness.prepare()` rebuilds
the runner; a new embedding process must explicitly supply it again. Custom
adapters must honor the existing launch, sandbox, usage and result contracts.
Calls run on the harness's owning event-loop/thread, not an independently opened
SQLite writer or arbitrary SDK worker thread. A future threaded/remote transport
must marshal requests back to that owner.

```python
fleet = self.peer_tools.call(
    "list", {"offset": 0, "limit": 50}, request_id="list-001"
)
target = self.peer_tools.call(
    "observe", {"box_id": exact_peer_id}, request_id="observe-001"
)["result"]
sent = self.peer_tools.send(
    target, request_id="note-001", body="Please inspect this edge case.",
    kind="question", ttl_seconds=300,
)
own_inbox = self.peer_tools.call(
    "inbox", {"offset": 0, "limit": 10}, request_id="inbox-001"
)
```

The runner constructs the object only for the exact fenced assignment/current
turn and binds it to the currently owned adapter invocation. Returning, raising,
or cancellation closes the object and clears `adapter.peer_tools`; retaining a
Python reference does not keep it operational. Lease expiry, reassignment,
revocation, a recorded next turn or a superseded run also denies further access.
Unsupported unfenced opt-in is a typed adapter failure, not a widened grant.

Owners embedding `Harness` may explicitly create a tool session using
`harness.peer_tools(assignment, turn_number)` and call `.close()` when finished.
That owner-created object checks lease/turn validity but has no runner invocation
callback; its lifecycle remains the embedding owner's responsibility. Neither
factory proves a physical provider process or grants a task lease.

## Evidence and boundaries

- `list` exposes only exact current-run box/task IDs and lifecycle metadata, with
  at most 50 rows per page. It does not read files or raw terminal output.
- `observe` names one running fenced peer and returns the existing mailbox's
  exact generation/cursor receipt, valid for at most 60 seconds and never beyond
  the recipient's lease.
- `inbox` reads only the caller's mailbox, at most ten entries per page. Reading
  is not a delivery or consumption acknowledgment.
- `send` requires an observation recorded by this same caller/turn, then uses the
  existing mailbox validation and worker attribution. It cannot approve, schedule,
  stop, grant tools or amend a plan. Local send IDs are namespaced by caller/turn
  before entering the run-wide idempotent mailbox.

Every successful read appends `BOX_PEER_READ_RECORDED` with its exact subject,
turn, request ID, operation, arguments, result and digest. Replay recomputes the
observation from that ledger cut and event time; actor, identity, type, result and
digest tampering are rejected. Box inspection associates the event with the caller,
and verified exports reproduce the read history. Legacy ledgers without these
events retain their original projection shape.

Read request IDs are immutable within a caller/turn. Retrying returns the original
record, **not a newly fresh observation**; use a new request ID for a fresh read.
The send path independently rejects an expired or replaced recipient. Read records
are capped at 64 per turn and 5,000 per run, each at 64 KiB. These are protocol
safety limits, not increases to approved model-token or money budgets. An embedding
adapter must account for any tool results it supplies to a model within its frozen
provider/context envelope. The tools themselves make no model requests.

Denied requests raise typed errors without successful-read events. A send that
succeeds is durably represented by the mailbox event; a read is not a successful
send receipt. Admitted attempts, including failures and retries, now have the
separate telemetry records below. Provider-facing execution remains integration
work, not a claim made by this interface.

## Attempt logger and optimization profile

Before dispatch, the bridge appends `BOX_PEER_CALL_STARTED` with a generated call
ID, exact caller/turn, logical retry ID, whitelisted operation label, argument
digest and whether a matching result already existed. It never stores argument
prose in this attempt record; send-body digests use the mailbox-redacted body.
An invalid operation is labeled `unsupported`, not copied into diagnostic prose.
A start-publication failure prevents the operation from running.

`BOX_PEER_CALL_FINISHED` records success, error or interruption and locally
measured monotonic operation time. A successful finish must reference a matching
durable peer read or worker message and bind its exact request/result digest.
Replayed timing is a trusted local bridge observation, not independently measured
CPU, network latency, provider tokens or dollar cost. It excludes publication of
the final audit record. Provider usage stays in the existing provider receipts.

If completion publication fails or the process disappears, the durable start
remains **unknown**. A missing measurement is never displayed as zero duration.
A tool error does not prove that no message was committed: retry the same logical
request to use the mailbox's existing idempotency. That retry gets a new attempt
record and a `reused` result; it does not erase the first unknown measurement or
manufacture a fresh observation. Failure-log publication preserves the original
exception, leaving the start unknown instead of replacing it with a misleading
outcome. Measurement completion may occur after caller revocation, but cannot
start another operation or grant authority.

`camol profile --db PATH --run-id RUN` includes an optional `peer_tools` section
grouped by operation and task: attempts, successes, errors, interruptions, unknown
outcomes, reused results, measured-call counts and the sum of known elapsed times.
Its data and the JSONL metadata logger contain no request/response prose. Box
inspection associates both call events with the explicit caller; exports replay
them. The profile field is absent for legacy runs with no attempt records.

Calls are capped at 128 admitted attempts per turn and 10,000 per run, independently
of the lower successful-read cap. Reconstructing a tool object does not reset
these counters. A closed/revoked caller, malformed request identity or exhausted
logging allowance is denied before admission; these pre-admission rejections do
not append into a sealed or unauthorized run. Native/remote transport security
logging for those rejected connections remains separate integration work. The
additional events can exceed a reader's default 20,000-event inspection allowance;
bounded reader overrides or exports are required rather than silently truncating
the run to a healthy-looking partial history.

## Explicit local transport and MCP relay

`PeerEndpoint(tools, parent)` is an optional owner-side Python API. It creates a
disposable Unix socket for one already-owned `PeerTools` turn. The parent must
be an existing canonical owner-private directory (0700); the socket is 0600.
The complete socket path is limited to 100 encoded bytes for macOS portability.
There is no TCP listener, global registration, model call or owner-control API.
The owner must keep the endpoint on its own event loop and close it before
releasing the turn. Closing the endpoint does not close the supplied tool object.

An explicit embedding can start `python -m camol.peer_mcp` as a stdio child,
supplying `CAMOL_PEER_ENDPOINT` and `CAMOL_PEER_TOKEN` only to that child. These
are short-lived worker capabilities, not account credentials. Do not put the
token in command arguments, global configuration or a transcript. The relay
removes the token environment entry on startup. This is not automatic native
provider registration and is not an instruction to modify a user's CLI config.

The dependency-free relay implements MCP 2025-06-18 initialization, ping,
tools/list and tools/call over bounded newline-delimited JSON-RPC. Its tools are
`list_boxes`, `observe_box`, `inbox` and `send_message`. Each requires a stable
logical `request_id`, independent of the RPC envelope's unique request ID.
Initialization authenticates the existing turn without adding a tool attempt or
claiming provider/model readiness. There are no sampling, resource, shell,
approval or task-allocation tools. Tool failures are `isError` results; malformed
protocol requests receive protocol errors. This follows the primary MCP
[stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
[lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
and [tool protocol](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).

MCP text content contains a `camol.peer_response` envelope. A successful read's
`result` is the audited read record, which itself contains the observation in
`result`. For `send_message.target`, pass the unchanged observation at
`json.loads(content[0].text)["result"]["result"]` from `observe_box`, not its outer
read record. List rows and inbox entries likewise live inside that read record's
`result`. Send results contain the mailbox record directly. The outer error and
MCP `isError` must be checked before extracting successful results.

The internal socket protocol is distinct from MCP. It accepts one request per
connection, at most 64 KiB per request and 128 KiB per response, eight active
connections and 256 total connections per endpoint. Reads have a five-second
timeout. The relay session is additionally limited to 512 protocol messages.
These limits do not replace frozen provider/context/resource budgets.

Lost replies are explicitly unknown: the operation may have committed. There
are no automatic retries. Retaining the exact logical ID/content allows an
explicit retry to retrieve the existing result without refreshing observations
or duplicating sends. Tool outcomes use the durable peer-call ledger; connection
counts from `snapshot()` are content-free **ephemeral owner-memory counters**,
not a replayable security log. A new endpoint resets those transport counters,
but cannot reset the tool ledger's per-turn/per-run allowances.

The socket's capability and Unix permissions are not a security boundary against
an unrestricted hostile process with the same OS user. An actual provider bridge
must freeze and enforce sandbox access to the exact transport and trusted relay
runtime. Cleanup refuses to remove a replaced socket or directory. Revoking or
closing the underlying worker turn makes later reads, messages and handshakes
fail even if the relay process remains alive.

## Opt-in Codex worker integration

Model-profile schema3 extends the existing explicit Codex schema2 profile with:

```json
"peer_policy": {
  "schema": "camol.peer_policy",
  "schema_version": 1,
  "transport": "local_mcp_stdio",
  "operations": ["list", "observe", "inbox", "send"]
}
```

This is a profile fragment, not a complete runbook. All schema2 fields remain
required, including its honest execution-policy limitations. The new profile
must be embedded/frozen in a human-reviewed V5/V6 runbook (or approved successor)
before launch. Schema1/schema2 profiles keep their identities and no-peer
behavior. Schema3 rejects null, extra, unsupported or partially specified peer
policies and does not yet support Claude. The transport version fixes the bounded
four-operation contract above; it does not authorize arbitrary MCP servers.

Admission includes the exact private short-path directory, trusted Camol/Python
read roots and the two capability environment names in the sandbox-policy digest.
No directory, account change or model request is needed merely to parse a profile.
The runner supplies the exact owned `PeerTools` object; a new provider invocation
creates its socket before reserving provider spend. Each invocation closes the
endpoint on return, error or cancellation. Retained successful provider results
return without creating an endpoint or spending again. Ordinary completion removes
the empty private transport directory; replaced/foreign content is preserved.

The native command uses an absolute owner-selected interpreter with isolated
imports and loads the exact `camol/__init__.py`, not a package discovered through
worker PATH/cwd. Python 3.9's ordinary package discovery needed enclosing-directory
read access; explicit package loading avoids granting that parent. Worker write
grants may not overlap the socket or trusted runtime roots in either direction.

The per-invocation Codex configuration requires the server to initialize, lists
only the four peer tools, forwards capability variables by name, and explicitly
excludes both from the agent-shell environment. It does not edit account-wide
config. Schema3 requires the CLI's `--strict-config` flag so unsupported settings
cannot silently become a permissive fallback. The read-only adapter probe denies
a CLI missing that flag before a worker invocation or spend reservation.
These configuration choices follow [official OpenAI documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
and the [shell-environment configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

New capabilities are registered with the run's redactors before they can reach a
provider. Tests deliberately echo the token to stderr and check retained evidence,
SQLite/artifact files and argv for leaks. Redactors retain these values in owner
memory for subsequent diff/artifact collection; they are not serialized secrets.
Endpoint-cleanup failures revoke the token and refuse successful task completion,
but first retain available provider usage/transcripts. A simultaneous provider
error/cancellation keeps its original attached evidence. Cleanup status is an
observed endpoint fact, not proof that a model used its tools.

Real fake-CLI builds exercise the full runner/admission/provider/relay path in
developer-trusted and macOS-sandboxed modes. The installed Codex CLI separately
accepts the generated configuration via read-only `mcp get`. That parser check
does **not** establish actual native tool initialization, model-directed calls,
shell-environment behavior, account entitlement or live model maturity. Those
remain required native acceptance gates; no paid model call was made.

## Remaining native integration

Claude registration and remote workers still need integration. It must bind the
same run/task/box/lease/turn and preserve the observer-before-send requirement,
bounded data, redaction, idempotency and revoked-turn behavior. It must not hand a
worker the owner control token, silently allow network on a network-denied plan,
or place trusted responses in worker-writable evidence directories. Refreshing
receipts behind an agent's stale request is not an acceptable substitute.

The direct-Python build and real stdio/socket fixtures prove their respective
interfaces, not a hosted model autonomously calling these tools. The owner has a
`/delegate` compatibility view and stopped-run revision-review entry point; this
is not an agent-facing approval tool or automatic proposal generator.
Claude registration, remote transport, broader read-only peer views,
natural-language delegation and live provider acceptance remain separate gates.

### Network and remaining native acceptance boundaries

The real macOS Seatbelt fixture in `tests/test_peer_sandbox.py` shows that the
current denied-network policy blocks Unix peer communication. The exact socket
directory plus trusted Python/package read roots and an explicitly permitted
network policy allow a read; the worker still cannot unlink the owner socket.
No sandbox rule was changed for this experiment. It does not justify adding `*`
network access to a denied profile: a network-denied peer integration would need
a separately specified, tested narrow Unix-socket policy.

The profile, admission, lifecycle and command configuration are implemented above.
Actual native acceptance must still verify token forwarding to the relay separately
from the agent's shell environment, required-server startup failure, and tool calls
under native provider execution. Independent SDK/fake-CLI compatibility alone does
not establish these Codex/Claude behaviors.

Claude registration must separately account for its current safe-mode and tool
allowlist behavior. Neither provider's account-wide configuration will be modified
as an implicit consequence of enabling a Camol worker.
