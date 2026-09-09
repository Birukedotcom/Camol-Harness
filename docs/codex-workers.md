# Codex and local-model workers

Schema5 runbooks can assign real fenced work to `codex_cli` or `codex_oss`.
They run through the same isolated workspace, immutable candidate, independent
evaluation, integration, lease, usage and human-acceptance path as other workers.
They are not planning-only wrappers. Current verification uses a deterministic
fake CLI and a local HTTP catalog fixture; no live paid or local-model inference
was performed for this change.

## Declare the capability tier honestly

Use `camol.model_profile` schema2 for these workers. It contains all schema1 fields,
plus `execution_policy`, `local_provider`, and `local_endpoint`. Schema1 serialization
and old runbook digests are unchanged. A hosted Codex profile requires:

```json
{
  "execution_policy": {
    "cost_enforcement": "observed_only",
    "model_identity": "requested_only",
    "inner_turn_limit": "unsupported",
    "tool_policy": "sandbox",
    "network_enforcement": "ambient"
  },
  "local_provider": null,
  "local_endpoint": null
}
```

The remaining fields must name `adapter_kind: codex_cli`, `provider: openai`,
`runtime_binary: codex`, an explicit model chosen by the owner,
`permission_mode: acceptEdits`, `allowed_tools: []`, and
`network_destinations: ["*"]`. Credential paths/references must explicitly name
the existing login scope. Never copy credentials into the profile.

The CLI has no supported hard USD cap, hard inner-model-turn cap, or exact
resolved-model receipt in this integration. Profiles requesting those guarantees
are rejected with `POLICY_DENIED`; a restricted-egress grant cannot be satisfied
by ambient network access. Existing `max_*_usd_cents` fields reserve budget, not
a hard spending limit on Codex. Token use is checked after the provider returns.
`max_agent_turns` is not a Codex inner-loop limit. Do not use this tier when those
hard guarantees are required.

The read-only readiness probe verifies runtime flags and the existing login.
It explicitly leaves model entitlement, quota and resolved model unknown under
the owner-approved weaker tier. It does not run a paid preflight. The exact
profile snapshot is checked against the admitted probe digest before every turn;
changing a file-backed profile inside a worker causes a typed policy wait.

## Local models

For `adapter_kind: codex_oss`, use `provider: local`,
`cost_enforcement: not_applicable`, `local_provider: ollama` or `lmstudio`, and an
explicit loopback HTTP origin such as `http://127.0.0.1:11434`. No provider
credential reference or credential read path is allowed. The owner must name an
already downloaded model. Cloud-labelled and remote-backed catalog entries are
rejected.

Camol reads only `/api/tags` for Ollama or `/v1/models` for LM Studio, with proxy
inheritance and redirects disabled, a five-second timeout and a one-MiB response
bound. It never downloads a model. Missing catalog entries produce
`NEEDS_DOWNLOAD` rather than implicit installation.

Execution uses an explicit custom local Responses provider, not the `--oss`
bootstrap path that can install missing models. This requires a local server
supporting the Responses protocol and structured results. The observed loopback
catalog is not proof of inference quality, enough RAM/VRAM, model weights identity,
or an air-gapped machine: a trusted local daemon could itself forward traffic.
The current outer sandbox supports denied or explicitly unrestricted network,
not an enforcing destination allowlist. The local profile therefore still needs
the explicit unrestricted runtime grant; model-generated shell network is disabled
by Codex's inner workspace-write policy. Do not label this offline containment.

## Usage, failure and restart

The JSONL stream retains transcript/tool evidence and observed token/cache counts.
Hosted Codex dollars and resolved model remain `null`, not fabricated zero values.
An unknown paid charge consumes its conservative reservation and stops further
paid launches; a previously retained successful result can still be consumed
without paying again. The owner must reconcile an uncertain charge rather than
delete state or blindly retry. Local-provider API charges are marked not applicable;
electricity and hardware costs are not estimated.

Both Claude and Codex reserve an immutable, fsynced invocation identity before
launch. It binds the full packet hash, profile digest, workspace, run, task,
worker, lease/fence and turn. Exclusive publication prevents two concurrent
claimants. Terminal observations are a separate immutable record. A crash without
a recoverable result cannot erase the intent and duplicate a possibly paid call.
Cancellation and rejected final JSON preserve observed usage or unknown reserves.

Codex executes with `--ignore-user-config`, `--ignore-rules`, `--ephemeral`,
`--json`, `--output-schema`, `--sandbox workspace-write`, an explicit working
directory and noninteractive approval policy, inside the frozen Camol outer
sandbox. It cannot silently widen that outer authority.

## Verification

```bash
python3 -m unittest tests.test_codex_adapter tests.test_claude_adapter tests.test_invocations -v
```

Fixtures cover real subprocess edits, V5 gates/integration, successful-result
recovery, missing/invalid output, interruption, unknown-cost stopping, profile
tampering, policy refusal, local inventory and redirect refusal, atomic concurrent
claims, and cross-subject/symlink rejection. They do not establish live account
entitlement or any particular model's capability.

Protocol references: [Codex noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
[provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).
Read-only flag compatibility was checked with installed `codex-cli 0.146.0`.
