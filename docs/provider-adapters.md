# Provider and model adapters

Camol keeps provider behavior outside the state-machine kernel. A schema-v3
runbook selects an adapter kind and a workspace-relative, versioned model
profile. The profile freezes the runtime name, requested alias, allowed resolved
model identities, tool policy, credential references, network authority, and
turn/task/run ceilings. Its digest is part of the provider probe definition and
therefore part of the lease's probe-policy fence.

A profile is policy, not proof. It cannot declare itself authenticated, entitled,
within quota, or connected. Those properties require a short-lived
`camol.provider_capability` receipt from an explicit provider preflight.

## Fable profile naming

`profiles/models/claude-fable-5-1.yaml` preserves the requested “Fable 5.1”
product preference in its profile id. It intentionally requests the CLI's
`fable` alias and only accepts the concrete `claude-fable-5` identity. The
profile remains `SPECULATIVE`. Camol will not record or display “Fable 5.1” as
the resolved model unless a future profile explicitly allows that real provider
identifier and a provider response returns it.

## Readiness sequence

1. Install and authenticate the supported provider CLI outside Camol. Camol
   stores only the credential reference `claude-existing-login`, never the
   credential value.
2. Validate and approve the schema-v3 runbook and model profile.
3. Explicitly authorize one low-impact preflight:

   ```bash
   camol provider-preflight \
     --profile profiles/models/claude-fable-5-1.yaml \
     --workspace /absolute/path/to/repository \
     --state-dir /absolute/path/to/external-camol-state \
     --max-usd-cents 10 \
     --accept-spend
   ```

4. Run `camol doctor` or `camol run`. Admission reads the receipt, rechecks the
   existing CLI login without sending a model request, verifies target/profile/
   model/freshness bindings, and only then permits a lease.

The preflight is never called by `doctor`, admission, or test discovery. It uses
one no-tools request, a one-turn limit, and the profile's per-turn dollar ceiling.
Revoked login, absent or stale capability, a different target, a changed profile,
an unexpected resolved model, or an exhausted task/run budget prevents launch.

## Claude CLI turn evidence

The first adapter implementation runs Claude CLI print mode through the same
outer sandbox used by other workers. The context packet is written to stdin so
it does not appear in process arguments. Each turn records:

- adapter and profile versions/digests;
- redacted argv, cwd, timing, exit status, and sandbox-policy digest;
- requested model and provider-reported resolved model;
- provider-reported input/output tokens and cost in integer USD micros;
- tool request/result identities and input digests;
- content-addressed, redacted provider stream and stderr;
- the resulting workspace diff and independently collected files.

Provider-reported usage replaces any token numbers authored by the model. The
adapter rejects unknown model resolution, a model outside the frozen allowlist,
or usage above the remaining turn/task/run ceiling.

## Adding OpenAI or a local runtime

A new backend implements the same two boundaries:

- a read-only provider probe plus an explicit capability-preflight producer;
- a bounded-turn adapter that returns Camol's result contract and observer
  evidence.

Local runtimes may use an empty network grant and no credential references.
Hosted runtimes must name both. The scheduler, leases, evaluator, artifacts, and
state transitions do not acquire provider-specific branches.
