# Provider connections in Product V0

Camol delegates authentication to provider-owned CLIs. It never asks for an account
password and never parses or copies `auth.json`, keychain entries, OAuth tokens, or
provider credential directories.

## Commands

Startup and bare `/connections` inspect cached observations only. They make no
provider, Docker, or HTTP call. `/connections refresh [all|claude|codex|local|openai]`
explicitly refreshes the selected observations: executable/version and official
status commands for Claude and Codex, only the presence of `OPENAI_API_KEY`, or
`/models` on the configured numeric-loopback endpoint. It stores no raw account identity;
when an identity is available, Camol stores only an HMAC-SHA256 fingerprint made with
an owner-only local key.

`/login claude` runs `claude auth login` in the real terminal. `/login codex` runs
`codex login`. After it exits, Camol refreshes only that provider's status. A failed
refresh cannot reuse a cached success as a new login confirmation. An API key remains an
environment or OS-credential-store concern; Product V0 never writes one.

The rail distinguishes `installed`, `auth-observed`, `key-presence-observed`, and
`catalog-observed`; saved observations show their age. Docker executable presence
does not mean its daemon is connected. A catalog, including an empty catalog,
does not prove a model is loaded, can infer, or fits the machine. The legacy persisted
`ready` connection status remains readable for compatibility, but the terminal
labels it by observation type, not as task readiness. The rail does not currently
consume exact fresh kernel admission evidence, so it never displays a filled
readiness indicator. It explicitly says `task unverified`.

Each explicit CLI status/version command has a 10-second deadline and a 256 KiB
combined output bound. The local catalog GET has a two-second absolute deadline
and a 1 MiB response-body bound; it does not follow redirects, discover proxies,
or resolve hostnames. TCP connect, TLS handshake, and HTTP reads share the deadline;
slow peers are interrupted by shutting down the client-owned socket, and responses
are closed. An aborted CLI probe kills its owned process group even if its leader
already exited; this is not containment of a descendant that deliberately escapes
that group. These are application-level
bounds, not an on-wire bandwidth or server-computation guarantee. `/cancel` or
Ctrl+C cancels an explicit refresh and prevents subsequent probes in that request.
No refresh runs a model inference, download, Docker command, or paid preflight.

`/model` selects the planning provider:

```text
/model manual
/model claude:fable
/model codex:gpt-5.4
/model local:qwen3-coder
/model openai:gpt-5.4
```

The OpenAI selection is intentionally rejected for execution in V0 even if the key
reference exists. Local endpoints are restricted to HTTP(S) loopback. Claude and
Codex planning invocations are ephemeral/read-only and deny write tools. CLI-provider
text is streamed into a temporary terminal region; the loopback local provider
returns one bounded final response. In both cases only the final redacted message is
stored in the session transcript.

## Spend and capability

Selecting a non-manual model warns that normal planning messages may consume account
quota. Worker execution has a separate gate. `/run` for the packaged Claude/Fable
profile requires `--accept-spend`, an exact repetition of the approved worker ceiling
with `--worker-cents`, and accepts an optional 1–100 cent `--preflight-cents` cap.
The no-tools preflight records requested and resolved model, runtime, token usage,
cost, target, profile digest, and expiry. A fallback model outside the frozen
allowlist is denied rather than relabeled.

The packaged Fable profile is `SPECULATIVE`: its alias and allowlist are a request,
not evidence of entitlement. Only a fresh owner-authorized receipt can make that
dimension green. A connection glyph can never substitute for this receipt.
Provider-reported billing is validated after each response, but an upstream provider
can overrun a requested per-call ceiling. The plan and launch acknowledgement therefore
show the requested ceiling as authority, not a guarantee about external billing.
