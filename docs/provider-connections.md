# Provider connections in Product V0

Camol delegates authentication to provider-owned CLIs. It never asks for an account
password and never parses or copies `auth.json`, keychain entries, OAuth tokens, or
provider credential directories.

## Commands

`/connections` is read-only. It checks executable/version and official status
commands for Claude and Codex, checks only whether `OPENAI_API_KEY` is present, and
queries `/models` on the loopback local endpoint. It stores no raw account identity;
when an identity is available, Camol stores only an HMAC-SHA256 fingerprint made with
an owner-only local key.

`/login claude` runs `claude auth login` in the real terminal. `/login codex` runs
`codex login`. After it exits, use `/connections` again. An API key remains an
environment or OS-credential-store concern; Product V0 never writes one.

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
profile requires `--accept-spend` and accepts an optional 1–100 cent preflight cap.
The no-tools preflight records requested and resolved model, runtime, token usage,
cost, target, profile digest, and expiry. A fallback model outside the frozen
allowlist is denied rather than relabeled.

The packaged Fable profile is `SPECULATIVE`: its alias and allowlist are a request,
not evidence of entitlement. Only a fresh owner-authorized receipt can make that
dimension green. A connection glyph can never substitute for this receipt.
