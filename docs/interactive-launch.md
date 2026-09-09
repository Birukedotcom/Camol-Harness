# Exact interactive launch review

Provider-containing Product V0 plans support arbitrary combinations of `process`,
`claude_cli`, `codex_cli`, and `codex_oss` workers. They also support multiple distinct
Claude profiles. This does not add a remote execution target: the selected local
terminal host remains the target, including when Camol itself runs on a VM.

Every provider adapter must embed its complete `profile_snapshot`. Reusing one
`profile_id` for different digests is denied, because capability storage is keyed by
that identifier. All hosted profiles must name one common `max_run_usd_cents` value.
It is a run-wide worker accounting envelope, never multiplied by worker/profile
count. Individual task/turn limits, trust tiers, tools, credentials references,
network policy and V6 capacity bindings remain exactly as approved in the runbook.

After `/import` or a proposal and exact plan approval:

```text
/run
/run --preflight-cents 7
```

Both commands only render a deterministic manifest. They make no provider status,
catalog, inference, paid preflight, or worker call. The second command selects a
different bounded review input (1–100 cents per distinct Claude profile). The review
includes the product/runbook digests, complete source pin, state directory, local
target, workers, exact profiles, task scope, trust limitations, and separate budget
envelopes. Live receipt freshness is not part of the manifest digest.

Copy the command printed by the review, for example:

```text
/run --accept-launch sha256:EXACT_DIGEST --accept-spend --preflight-cents 7
```

`--accept-spend` is required if any worker is hosted Claude/Codex and rejected for a
local-only provider plan. Weaker draft-creation policies additionally retain their
separate `--accept-draft-policy PRODUCT_PLAN_DIGEST` acknowledgement. A new preflight
review input changes the launch digest: it cannot be substituted after approval.
The old `--worker-cents` and `--accept-provider-policy` forms cannot start provider
work. Existing unpaid process-only `/run` behavior is retained for compatibility.

## Two separate cost envelopes

The review displays the common hosted worker envelope and the total **requested
preflight reservation separately**. The latter sums one request for each distinct
Claude profile/target pair, capped by that profile's turn ceiling and the selected
review input. Duplicated workers with an identical profile do not multiply it.
Neither number is a combined account spending cap. Claude's requested dollar ceiling
is a client-estimate stop/configured reservation, not a hard billing guarantee;
upstream billing can exceed it. Codex provides observed-only cost accounting and no
hard dollar/inner-turn cap. Unknown paid usage remains unknown and blocks further
paid launches under the existing policy.

New worker invocations use atomic shared journal admission, so concurrent boxes
cannot each allocate the same remaining worker envelope. In-flight holds remain
charged across restart, and known settlement is deduplicated against event
receipts. See [usage accounting](usage-accounting.md#shared-hosted-worker-admission)
for the conservative pause/resume policy, bounded scan and remaining live
reservation-inspection limitations. This does not convert the provider's declared
weak controls into a hard upstream billing guarantee.

After exact acknowledgement, Claude preflights run sequentially. Each has a stable
operation ID derived from the launch scope, exact profile and target. A repeated
operation cannot spend again or silently renew its receipt. A currently fresh,
exact existing capability may be reused; the transcript reports zero additional
preflight charge for reuse, without erasing its original charge. Codex and OSS do
not run a Claude-style paid preflight.

Any failed/unknown operation stops the batch before worker launch. Unknown usage
holds the entire state directory. Inspect it with:

```text
camol preflight-status --state-dir /EXACT/RUN/STATE/DIRECTORY
```

This UI does not reconcile an unknown charge, clear a hold, delete an intent, or
automatically create a replacement operation ID. A stale/known-failed operation
requires explicit owner review; a separately approved CLI operation can establish
a fresh receipt where policy permits. A source/profile/budget change requires a new
exact review, not reusing an earlier approval.

All required receipts are checked again after the last preflight, and source/target
are checked again before one supervisor spawn. The full source pin is passed to the
kernel for persistent approval/admission binding. Launch acceptance still proves
neither quota nor readiness by itself: the kernel's exact workspace, authority,
capacity, sandbox, runtime/login/catalog, receipt, reservation and fence gates remain
in force. Local catalog presence is not inference, weights identity, resource fit,
or airgap proof, and never causes a download/load.

An existing matching supervisor is reattached without preflight or new spending.
The UI does not claim exactly-once reconciliation for an ambiguous external spawn;
the authoritative supervisor's leader lock and immutable run/source bindings remain
the duplicate-execution protection. Source changes after a completed preflight keep
that preflight's accounting even when launch is denied.

Closing the terminal cancels its current client request and waits for its actual
session writes to finish. Cancellation is rechecked after source inspection and
immediately before supervisor dispatch; it is not an atomic transport cancellation
guarantee. A supervisor already dispatched remains authoritative and is not stopped
by closing the terminal. Explicit login observations release their serialization
lock before queuing any UI update, so teardown never waits on a synchronous render.

## Verification

Tests use real disposable Git repositories, injected provider responses and the real
preflight journal. They cover mixed review, distinct-profile deduplication, old-flag
refusal, spend/digest matching, unknown and stale evidence, cancellation, source
drift, and reuse without another model request. Product E2E uses a fake Codex CLI and
a real supervisor/build/acceptance path. No live account or model capability is
established by these fixtures.
