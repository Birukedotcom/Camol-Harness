# Reviewed execution-target identities

`camol target` and `Harness.targets` now maintain a run-scoped adoption registry.
This implements the identity/review portion of SPEC 19.5. It does **not** discover
a host, attest its identity, register a runnable worker, connect to a transport,
provision infrastructure or authorize execution/deletion. Those gates remain open.
Provider inventory and transport observations must not be mistaken for readiness.
The [local runtime observation path](target-runtime.md) now measures reviewed local
profiles and records generation-bound reports; authenticated remote observation
and execution admission remain separate.

## Operator workflow

1. Prepare a target descriptor using the exact identities below.
2. Use `camol target propose --descriptor FILE --expires-at TIMESTAMP` to obtain
   a plan-bound review proposal. No event or external request is issued.
3. Review the complete proposal, save it, then use `camol target adopt --proposal
   FILE --approval-digest DIGEST`. This records owner approval of that identity.
4. Use `camol target inspect` to inspect adoption history, not live host health.
5. `camol target retire --generation ID --adoption-digest DIGEST --reason TEXT`
   detaches the registry generation. It never deletes the machine or logs out.

All commands require `--state-dir` and `--run-id`; `--db` optionally selects the
existing database. Propose/adopt/retire also require `--workspace`, `--by` and the
exact `--plan-digest`. Add `--live` to use an existing supervisor; live inspection
also requires `--plan-digest`. No failed live operation falls back to a second
local owner. Explicit workspace/database paths must match the live supervisor.
Offline inspection uses retained ledger evidence and does not take ownership.

These are argument templates, not ready-to-paste values: select the actual run and
plan from `camol status`, and inspect the complete canonical proposal digest.
The expiry limits when a new adoption can be approved. It is not a claim that the
machine stays healthy or that an adopted record is a renewable execution lease.

## Descriptor V1

The internal review schema is strict and versioned. Provider-specific discovery
adapters must normalize raw inventory separately; this is not a raw provider API
decoder, and tolerant fleet decoding/capability negotiation remains unfinished.

| Field | Meaning |
| --- | --- |
| `schema`, `schema_version` | `camol.execution_target`, integer `1` |
| `target_id` | Harness execution-target identity; not a VM display name or box ID |
| `generation` | Unique adoption generation in this run; never reused |
| `control_plane_id` | Owner-reviewed controller identity; consistent across the run's registry |
| `label` | Display-only label, never a routing identity |
| `ownership` | Exactly `adopted`; created infrastructure requires a future provisioning receipt |
| `provider.kind` | Provider namespace, e.g. `gcp`, `manual`, `local` |
| `provider.account`, `.project`, `.location` | Explicit resource namespace; not inferred from ambient credentials |
| `provider.resource_id` | Stable provider identity, distinct from its display name |
| `provider.resource_name` | Provider's human-readable name; renaming cannot bypass duplicate detection |
| `transport.kind` | `local`, `ssh` or `worker_tls` |
| `transport.profile_digest` | Exact reviewed transport-profile digest; not proof of a successful connection |

All fields are required. A provider adapter must supply explicit namespace values,
not silently replace unknown identity with the current CLI account. Secrets and raw
credentials do not belong in descriptors. New protected values are rejected before
publication. Inspection applies current redaction without reinterpreting old approvals.

## State and concurrency

Adoption is a single `TARGET_ADOPTED` event; retirement is `TARGET_RETIRED`.
Events bind the approved human owner, exact run/plan/proposal and explicit time.
The append compares the complete run cursor, preventing a race from overwriting
another adoption. Replay/export preserve the same history. Inspection pages contain
up to 100 generations, with a cut cursor and redacted snapshot digest. History is
bounded to 1,024 generations per run, not a hard-coded three-box topology.

An active target ID or provider resource cannot be adopted twice under different
names. Retire it before approving a new generation. A retried adoption returns its
original record, including its retired status, rather than reviving it. A changed
proposal or changed retirement reason under the same identity is rejected.
Retirement refuses running/verifying leases bound to that target in the current
run, including expired but unreconciled leases. New adoption into a terminal run is
rejected; historical receipt retrieval remains possible.

This is **run-scoped metadata retirement**, not fleet-wide drain, salvage or a
provider deletion receipt. Other runs, pending external effects, unpushed commits,
untracked artifacts and outstanding invocations need independent reconciliation
before actual teardown. The registry explicitly records `readiness: unproven`,
`execution_authority: false` and `deletion_authority: false`.

Python embeddings use `harness.targets.propose`, `.adopt`, `.retire` and `.inspect`.
The supervisor provides matching `target-*` commands through its local authenticated
control socket; the CLI uses run/plan-bound V3 requests. The SSH bridge allowlist is
unchanged. No credentials, connections, external commands or provider spending are
introduced by this registry.

## Remaining distributed-execution acceptance

Target-side observed capabilities and admission, authenticated worker adoption,
assignment delivery, source/artifact transfer, remote process lifetime and fencing,
trusted result/accounting reduction, fleet-wide salvage and cloud lifecycle adapters
are still required. This identity registry is a prerequisite, not proof that a
remote box can build a project.
