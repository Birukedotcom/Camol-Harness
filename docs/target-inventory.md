# Provider inventory before target adoption

`camol target inventory` decodes an existing provider JSON file without contacting
the provider, reading a login, running gcloud, or creating a run/state directory.
It implements the provider-schema boundary in SPEC §19.5. It is not authenticated
discovery, machine adoption, task readiness, remote execution or provisioning.

## Supported inputs

`camol target inventory-formats` lists explicit decoder names, capabilities and limits:

- `gcp-compute-v1-aggregated`: one Compute Engine v1 `instances.aggregatedList`
  response. The `compute#instanceAggregatedList` kind is required; records live
  inside each zone's `instances` array.
- `gcloud-compute-json-v1`: an existing JSON array from `gcloud compute instances
  list --format=json`. This is Camol's decoder version, not a claim that gcloud
  has a separately versioned JSON schema.

For an existing file, replace the example path and declared scopes:

```sh
camol target inventory --input /absolute/path/instances.json \
  --format gcloud-compute-json-v1 --account account-label --project project-id
```

The account label is non-secret review metadata, not a credential or authenticated
identity. Project ID/number is explicit; resource URLs must agree exactly with it.
Project-ID/number equivalence is not guessed. Input must be a regular file of at most
8 MiB. Duplicate JSON keys and non-finite values are rejected. At most 4,096 instance
rows are decoded per input.

The source contract follows the official [Instance reference](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances)
and [aggregated-list response](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/aggregatedList),
reviewed 2026-09-08. Pagination, scoped warnings and unreachable resources mean that
an empty page or absent next-page token cannot establish a complete fleet. The
[gcloud list reference](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/list)
documents the CLI surface. No provider code is copied or vendored.

## Identity and partial results

Accepted rows expose a provider record compatible with target descriptors: provider
kind, declared account/project, zone, server resource ID and VM name. IDs remain
uint64 decimal strings, never floating-point conversions. They are distinct from
Camol target, generation, control-plane, box and lease IDs; names never replace a
missing server ID.

Unknown optional fields do not invalidate a fleet. Missing/unfamiliar status becomes
explicit unknown. Missing or renamed required identities, conflicting project/zone/
self-link fields and unsupported kinds quarantine the row. Duplicate accepted IDs
or names within one zone quarantine every colliding row, never whichever comes
second. Invalid scope containers are surfaced separately; other scopes can decode.

Only identity and known status fields are exposed. Startup scripts, metadata,
network addresses, account details, warning text, page tokens and future fields are
not copied into output or errors. A source digest and report digest establish content
identity, not provider authenticity. Error locations use numeric indices.

Exit zero means the supplied input decoded without row/scope/page warnings. Exit two
means invalid input or a report requiring attention (rejected rows/scopes, more pages,
warnings or unreachable resources). A report can accompany exit two. Even exit zero
always leaves freshness unknown, authentication false and complete inventory false:
a file import cannot prove current state or complete enumeration.

## Adoption and embedding

Python callers use `camol.target_inventory.normalize_inventory(value, format=...,
account=..., project=...)`. Review a record's `provider` fields and place them in an
explicit [target descriptor](target-adoption.md), alongside separately chosen target,
generation/control-plane identity, label and pinned transport profile. The existing
`target propose` and exact human-approved `target adopt` steps remain required.
Retain the original file/digest with review evidence; adoption does not automatically
retain the dump or authenticate it.

The integration test decodes provider data, proposes through the actual registry,
rejects worker approval and records human adoption. Readiness stays unproven and
execution/deletion authority false. A `RUNNING` row never automatically connects,
registers, leases or deletes anything. Live collection, authenticated target/work
inspection, worker registration, target-side admission/execution, streaming and
salvage remain separate implementation and acceptance requirements.
