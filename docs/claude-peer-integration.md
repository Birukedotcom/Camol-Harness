# Explicit Claude peer execution tier

Schema4 model profiles opt Claude workers into the same four-operation local
peer bridge as schema3 Codex profiles. This is an implementation with controlled
subprocess tests, not a live Claude acceptance result. Legacy Claude schema1
profiles continue using safe mode and receive no peer capability.

## Why a separate policy

The current [Claude CLI reference](https://code.claude.com/docs/en/cli-usage)
documents safe mode as disabling MCP, while restricted mode isolates ordinary
settings and permits explicitly configured tools. The installed 2.1.263 help
also says bare mode excludes subscription OAuth. Camol therefore does not
silently replace existing profiles with bare mode or remove safe mode from them.
Only an exact human-approved schema4 profile selects the restricted peer tier.

In addition to all ordinary model-profile fields, schema4 requires `peer_policy`
with the existing `camol.peer_policy` v1 four-operation contract, null
`local_provider`/`local_endpoint`, and exactly this `execution_policy`:

```json
{
  "customizations": "restricted_explicit_settings",
  "managed_policy": "host_applies",
  "peer_capability_exposure": "worker_environment",
  "peer_startup": "observed_before_completion",
  "network_enforcement": "ambient",
  "cost_enforcement": "provider_estimated",
  "inner_turn_limit": "provider_max_turns"
}
```

The manifest includes these fields and their digest before `/run` accepts the
exact launch. There is no migration, default profile replacement or automatic
permission expansion. Supported built-in names are Read, Write, Edit, Glob,
Grep and Bash; the explicit list may be empty. Wildcards, permission-pattern
strings, duplicate names, external MCP names and `default` are rejected in this
tier. Only `dontAsk` and `acceptEdits` are supported, with permission prompts off.
The new tier requires an explicitly unrestricted network grant; it does not
convert an existing denied-egress plan into a network-enabled one.

## Runtime configuration and limits

Each invocation uses `--restricted`, empty ordinary setting sources, the exact
built-in tool list, and strict per-invocation MCP configuration containing only
`camol_peers`. Its allowed tools add only the four explicit peer tool names.
The frozen settings disable hooks and auto memory and exclude discovered
CLAUDE.md files. Skills remain disabled. These controls follow the official
[settings](https://code.claude.com/docs/en/settings) and
[memory](https://code.claude.com/docs/en/memory) documentation. Managed host policy
still applies. Actual exclusion behavior, including managed customization,
must be verified in native acceptance; the outer sandbox is a separate boundary.

The [MCP configuration](https://code.claude.com/docs/en/mcp) forwards the two
short-lived capability environment variables by name. No literal token is put
in argv or a global config file. Unlike the Codex configuration, this tier does
**not** claim that the capability is excluded from worker-shell environments.
It is the worker's own scoped messaging capability, not an account credential or
supervisor-control token. Redaction persists in owner memory through subsequent
artifact collection. This is not protection from an unrestricted same-UID
attacker or a proof against all possible covert exfiltration.

Admission binds the exact relay/runtime read roots and private socket directory.
The owner creates an endpoint only for an owned worker turn and rejects overlapping
worker-write grants. It revokes the endpoint on return, cancellation or failure.
Foreign/replaced transport contents are preserved for owner inspection, not
recursively deleted. A cached successful provider result creates no new endpoint
or model invocation.

Claude has no equivalent verified required-server startup guarantee here. The
adapter requires an authenticated relay handshake before accepting completion;
if none occurred, it fails the attempt and retains observed billing/output.
**This does not prevent model spend before the missing handshake is detected.**
A handshake proves the scoped relay initialized, not that a model called a tool.
An owner requiring peer readiness before any inference must not enable this
provisional tier. Exact pre-inference startup enforcement remains an open gate.
The existing explicit model-capability preflight does not prove peer readiness.

## Verification scope

Controlled fake-Claude subprocesses parse the generated arguments, start the
actual stdio relay, authenticate the socket and invoke a peer read. A complete
fixture build runs through admission, verification, integration and the final
human-acceptance wait in both trusted and actual macOS-sandboxed modes. Negative
cases cover absent required CLI flags, missing relay initialization, cleanup
failure, invalid policies and retained usage. The fixture echoes its token so
event/database/artifact checks can reject secret persistence. Cached results
are recovered with a zero new-spend allowance and a trap against endpoint reopen.

No hosted model, real account mutation, subscription entitlement test or API-key
request ran. The installed Claude command was used only for help/parser inspection;
help acceptance does not prove settings application or MCP initialization.
Native account login, required-startup behavior, real tool use, shell capability
exposure and managed-policy interactions remain live acceptance gates.

## Next gate: withhold the prompt until startup is proven

The reviewed upstream Python Agent SDK HEAD was
`efd4d865ef1795daffee3cd24cce45307aed8a51`. Its
[control implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/efd4d865ef1795daffee3cd24cce45307aed8a51/src/claude_agent_sdk/_internal/query.py)
separates initialization and `mcp_status` control requests from user messages;
its [CLI transport](https://github.com/anthropics/claude-agent-sdk-python/blob/efd4d865ef1795daffee3cd24cce45307aed8a51/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
uses streaming JSON input. This suggests a staged-input adapter that sends only
bounded initialization/status controls, requires both exact native server status
and the owner endpoint's authenticated handshake, then releases the task prompt.
This is a candidate design, not verified native behavior or implemented gating.

Implement it behind the existing outer sandbox with bounded concurrent stdout
capture, exact request IDs, no permission-grant callbacks, no agent-proposed
configuration changes, launch reauthorization immediately before prompt dispatch,
and cancellation/process-tree cleanup during every phase. Retained phase evidence
must distinguish no prompt sent from unknown provider billing; startup alone
cannot manufacture a zero-cost receipt. Verify malformed/foreign/duplicate
control replies, early exit, initialization timeout, expired lease, detached
children and recovery without resending an uncertain prompt. A new strict policy
must not silently reinterpret the schema4 pre-completion contract.
