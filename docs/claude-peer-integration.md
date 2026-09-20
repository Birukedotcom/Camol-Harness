# Explicit Claude peer execution tier

Schema4 and schema5 model profiles opt Claude workers into the same four-operation local
peer bridge as schema3 Codex profiles. This is an implementation with controlled
subprocess tests, not a live Claude acceptance result. Legacy Claude schema1
profiles continue using safe mode and receive no peer capability.

## Why a separate policy

The current [Claude CLI reference](https://code.claude.com/docs/en/cli-usage)
documents safe mode as disabling MCP, while restricted mode isolates ordinary
settings and permits explicitly configured tools. The installed 2.1.263 help
also says bare mode excludes subscription OAuth. Camol therefore does not
silently replace existing profiles with bare mode or remove safe mode from them.
Only an exact human-approved schema4 or schema5 profile selects the restricted peer tier.

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

Schema4 has no verified required-server startup guarantee here. The
adapter requires an authenticated relay handshake before accepting completion;
if none occurred, it fails the attempt and retains observed billing/output.
**This does not prevent model spend before the missing handshake is detected.**
A handshake proves the scoped relay initialized, not that a model called a tool.
An owner requiring peer readiness before any inference must not enable this
provisional tier. Schema5 adds owner-side prompt withholding as described below;
native pre-inference acceptance remains an open gate.
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

## Schema5: bounded startup before task-prompt dispatch

The reviewed upstream Python Agent SDK HEAD was
`efd4d865ef1795daffee3cd24cce45307aed8a51`. Its
[control implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/efd4d865ef1795daffee3cd24cce45307aed8a51/src/claude_agent_sdk/_internal/query.py)
separates initialization and `mcp_status` control requests from user messages;
its [CLI transport](https://github.com/anthropics/claude-agent-sdk-python/blob/efd4d865ef1795daffee3cd24cce45307aed8a51/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
uses streaming JSON input. Camol now implements that owner-side staged-input
sequence in `ClaudeStartup`, behind the same outer sandbox. This is verified
with controlled CLI subprocesses, not the real Claude startup protocol.

Schema5 retains every schema4 field and changes only the exact execution-policy
value `peer_startup` to `initialized_before_prompt`. A schema/version mismatch is
rejected; old digests, behavior and default profiles are not migrated.

Before sending the task, Camol sends only initialize and MCP-status control
requests. Replies must match their unique pending request IDs. Status must name
exactly one connected `camol_peers` server with the expected server identity and
exact four-tool catalog. The owner independently requires its authenticated relay
handshake and current turn/lease, reauthorizes launch, then checks that owned
connection again immediately before dispatch. CLI status alone cannot release
the prompt. No permission-grant callback or provider-requested reconfiguration
is supported. Both control waits are bounded to ten seconds within the overall
frozen adapter timeout; startup output is capped at 2 MiB, 128 frames and 256 KiB
per frame. Foreign, duplicate, malformed and unsolicited control messages,
missing/failed peers, EOF and timeout all refuse further input.

Evidence records phase, fixed failure code, attempted-input hash/byte count,
separate prompt hash, dispatch-started and prompt-sent flags. `prompt_sent` means
the pipe write drained, not that the provider acknowledged receipt. A failed
write after dispatch starts is not reported as an unsent prompt. Raw prompts and
control contents are absent from this startup summary. Cancellation preserves
phase evidence; absent billing receipts remain unknown with the invocation hold
intact. Startup withholding is **not** a zero-cost or no-network guarantee.

On startup refusal, Camol kills the owned process group and bounds process/pipe
cleanup together. A detached pipe holder cannot block refusal indefinitely;
detached descendants are still not claimed to be contained by a process group.
The bounded descendant fixture self-terminates in its disposable workspace.
Successful cached results do not reopen a peer endpoint or resend a prompt.

Controlled builds cover both trusted and actual macOS-sandboxed execution,
missing owner authentication despite a forged connected status, failed native
status, real-child timeout/cancellation, write failures and detached pipe holders.
Native protocol compatibility, actual settings exclusion, managed-policy effects,
live model tool use, and billing behavior remain explicit acceptance gates. Do
not treat the schema5 fixture results as evidence that real Claude is ready.
