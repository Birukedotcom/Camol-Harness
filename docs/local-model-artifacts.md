# Local model artifact preparation

Camol can download explicitly approved, hash-pinned model files into a private local
store. **Downloaded does not mean loaded, connected, capable, or inference-ready.**
No model code is imported, no archives are extracted, and no runtime is started by
preparing, approving, downloading, listing, or inspecting a model.

## Frozen download authority

`camol.model_download_plan` version 1 requires:

- `plan_id`, `model_id`, a descriptive immutable `revision`, and `owner`;
- `files`: logical relative `path`, HTTPS `url`, trusted expected SHA-256 `digest`,
  exact `size_bytes`, and an optional opaque `credential_ref` for every file;
- canonical `allowed_origins`, including any permitted CDN redirect origins;
- `max_disk_bytes` covering the unique expected model contents;
- `max_transfer_bytes`, a cumulative body-read ceiling, including unknown reads;
- a bounded per-I/O `timeout_seconds` and `max_redirects`.

The publisher's checksum or another reviewed source supplies the expected digest.
Downloading unknown bytes and then calling their own hash “trusted” is not a
supply-chain check. A valid checksum does not prove model quality, license rights,
benign behavior, or runtime compatibility.

Initial URLs must not contain userinfo, fragments, or query strings. Use a separate
credential resolver for private sources, never a bearer token or signed URL in the
manifest. The built-in HTTPS transport does not inherit ambient proxies, permits
only approved HTTPS redirect origins, strips authorization on cross-origin redirects,
and does not retain transient signed redirect URLs. Raw provider exceptions and
credential values are not written into the download catalog or receipts.

## Lifecycle and commands

```text
planned -> approved -> downloading -> downloaded_verified
                           |                 |
                           +-> paused        +-> loaded: unverified
                           +-> failed        +-> inference_ready: unverified
```

Use a reviewed manifest and the exact digest printed by validation:

```sh
camol models validate --plan /absolute/model-download.json
camol models prepare --plan /absolute/model-download.json
camol models approve --digest sha256:EXACT_PLAN_DIGEST --by YOUR_PLAN_OWNER
camol models download --digest sha256:EXACT_PLAN_DIGEST --by YOUR_PLAN_OWNER
camol models list
camol models status --digest sha256:EXACT_PLAN_DIGEST
camol models artifacts --digest sha256:EXACT_PLAN_DIGEST
```

`EXACT_PLAN_DIGEST` and `YOUR_PLAN_OWNER` are placeholders, not runnable values.
The default store is `models/` beneath the normal Camol state home. `--root` selects
another owner-only store. Choose a private directory outside a working repository.
The store's directory must be owned by the current OS user with mode `0700`;
catalog/content files are private. This is a local trusted-host API, not an
authentication system for arbitrary remote callers supplying an owner name.

`ModelStore(root).prepare(DownloadPlan.from_dict(document))` and
`approve(plan.digest(), by=owner)` never contact the source. `download(...)` performs
the explicitly authorized transfer. `list`, `status`, `events`, and
`verified_artifacts` support a read-only store; `verified_artifacts` rehashes the
current bytes instead of trusting an old availability label. Logical filenames are
metadata, not filesystem destinations: blobs are addressed only by SHA-256, and
each plan has its own partial-file namespace.

## Cancellation, resume, and uncertain outcomes

The downloader streams synchronously in the caller's thread. Ctrl+C or a supplied
`threading.Event` cancellation signal preserves a flushed partial and returns
`paused`; there is no detached background writer that keeps modifying files after
the caller returns. Cancellation during a blocking read is observed after that
bounded I/O completes or times out. Embedders that use a worker thread must await
that worker's termination before disposing its store.

An explicit later `download` call rehashes the partial, requests its exact next
byte range, and requires a matching `206 Content-Range` and exact remaining
`Content-Length`. Servers that ignore range requests, return encoded bodies, omit
the required length, change total size, or supply a wrong final hash fail closed.
There is no silent overwrite/restart or automatic network retry.

Before each bounded read, its maximum body bytes are charged durably. A successful
read settles to the observed byte count. If the process or read fails before its
outcome is known, that reservation remains charged. `observed_bytes` and
`accounted_bytes` therefore intentionally differ after uncertain outcomes. The
ceiling measures application payload-body reads, not TCP/TLS/header overhead or a
promise about upstream bandwidth billing. HTTP/socket libraries may already have
buffered body bytes before Camol's next read reservation or before it rejects a
response header. A strict on-wire bandwidth/egress-cost cap is not implemented;
status explicitly labels this accounting scope. Exhausted authority needs a new reviewed plan;
it is not reset by retrying.

Incomplete bytes never become a verified blob. Final publication is atomic and
hash-checked; a crash between publication and removal of the staging link is
recoverable without another network transfer. An orphaned `downloading` record
without a live store lock is displayed as paused/requiring resume, not live work.
The global store lock serializes model preparation; arbitrary multi-download
parallelism is not claimed by this initial implementation.

`discard_partial(plan_digest, by=owner)` is an explicit destructive maintenance API.
It removes only that plan's unverified partial bytes, records the removed count and
that they are not recoverable, never removes verified blobs, and does not refund
transfer authority. No download automatically invokes it after a corrupt response.

Disk checks compare remaining expected contents with current free space. The disk
ceiling covers this plan's unique contents, not unrelated models or metadata, and
is not a reservation against other applications filling the disk. Disk exhaustion
fails into a retained partial/uncertain record rather than inventing success.

## Adapter boundary and current scope

`DownloadTransport.open(...)` returns a bounded byte stream. The built-in transport
uses HTTPS; injected transport plugins are trusted host code and must honor the
same contract. Tests use tiny local HTTP fixtures through an explicit fixture
adapter, never real external model downloads.

`ModelHost.inventory()` is a protocol for a separate hosting adapter boundary,
not an implemented model loader. Existing loopback
endpoint/model inventory remains read-only. Download preparation does not invoke
Ollama pull, LM Studio load, remote Python code, package installation, extraction,
GPU allocation, or inference. A host/load adapter must obtain its own exact resource
and execution authority and produce actual runtime/capability evidence before any
connection glyph or lease can call the model ready.
