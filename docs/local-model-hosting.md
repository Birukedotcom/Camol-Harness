# Owner-approved local model hosting

Camol includes a narrow **llama.cpp `llama-server` process backend** behind
`models.ModelHost`. It can prepare, approve, load, inspect, and unload one explicitly
owned local model process across CLI exits. It does not mutate an existing Ollama,
LM Studio, or user-managed server. No automatic installation, download, import,
quantization, provisioning, account login, or paid API call occurs here.

This backend has been tested with tiny real subprocess/loopback protocol fixtures.
An actual llama.cpp binary/model/GPU run has **not** been performed for this change.
A fixture pass is not hardware-fit, model-quality, or vendor compatibility evidence.

## Frozen contract and authority

`camol.model_host_plan`, schema version 1, freezes all these fields:

| Field | Meaning |
| --- | --- |
| `backend` | Exactly `llama_cpp` |
| `plan_id`, `owner` | Plan identity and human owner |
| `model_store_root` | Absolute private `ModelStore` path |
| `download_plan_digest`, `logical_path` | Exact previously verified download and GGUF file |
| `artifact_digest`, `artifact_size_bytes` | Approved SHA-256 and expected model bytes |
| `executable`, `executable_digest` | Absolute installed executable and approved SHA-256 |
| `port` | Explicit numeric loopback TCP port, 1024–65535 |
| `context_tokens`, `threads`, `gpu_layers` | Requested runtime settings, not physical resource caps |
| `load_timeout_seconds`, `stop_timeout_seconds` | Bounded readiness wait and termination grace |
| `lifetime_seconds` | Finite helper-enforced process lifetime, at most one day |

Only a download containing one GGUF file is supported. Split models, additional
projectors/adapters, download URLs, remote endpoints, arbitrary arguments,
environment variables, model-code imports, and dynamically discovered launch flags
are not accepted. The caller supplies reviewed hashes; hashing arbitrary code does
not make its publisher trustworthy. Installed runtime binaries must be ordinary
non-group/world-writable executable files; direct symlink executables are rejected.
Resolve a trusted installed binary to its real path when drafting the plan.

The private ledger directory must belong to the current OS user with mode `0700`;
database, locks, and generated API-key files use `0600`. Keep it outside a working
repository and outside every worker's write/read authority. These are trusted
local-host APIs, **not authentication for arbitrary clients who can submit an owner
name**. A worker must not receive the host ledger or be able to call owner controls.

Preparation/approval performs no process launch or HTTP requests. One approved plan
authorizes exactly **one** load operation. An operation ID is required before
execution. Repeating the same operation returns its existing state; changing the
operation ID does not reset authority or restart a failed/uncertain operation.

## Embedding API

```python
from camol.model_host import LlamaCppModelHost, ModelHostPlan

plan = ModelHostPlan.from_dict(reviewed_document)
with LlamaCppModelHost(private_host_root) as host:
    host.prepare(plan)
    host.approve(plan.digest(), by=plan.owner)  # trusted owner control path
    result = host.load(plan.digest(), by=plan.owner, operation_id="load-001")
    status = host.status(plan.digest(), live=True)
    request = host.propose_unload(plan.digest(), operation_id="unload-001")
    # Show request.to_dict() and request.digest() to the owner before proceeding.
    host.unload(request, by=plan.owner, approve_digest=request.digest())
```

`ModelHostUnload` is a separate strict versioned contract binding its operation ID,
plan digest, exact load operation, and owner. Approval of a different unload digest,
owner, or load identity is rejected. The ledger retains both requests and outcomes.
`inventory()` and `events(plan_digest)` are available without contacting endpoints.
`LlamaCppModelHost(root, read_only=True)` refuses missing stores and never creates or
chmods files. Default `status` is a cached, timestamped inspection; `live=True`
explicitly permits fresh bounded read-only requests to the owned loopback process.

## Command line

`camol model-host --help` lists the same operations. Supply your reviewed local
plan as `--plan`, its exact digest as `--plan-digest`, and a private external
ledger directory as `--root`. The sequence is `validate`, `prepare`, `approve`,
then `load`. Approval and loading require `--by OWNER`; loading also requires a
unique `--operation-id`. Validation neither creates host state nor contacts an
endpoint. Preparation does not approve or allocate anything.

`inventory`, `events`, and default `status` inspect cached state without starting
a process. Only `status --live` requests fresh readback. `propose-unload` prints
the strict request and its digest for review; `unload` requires that request file
as `--request`, its exact `--digest`, and the owner via `--by`.

Load exits zero only for a confirmed freshly observed `loaded` result. Unload
exits zero only for a confirmed `unloaded` result. Failed, cancelled, expired,
in-progress or unknown execution results retain their JSON receipt and exit 2.
Inspection commands can exit zero while reporting an unhealthy state: successful
inspection is not successful loading, inference readiness or safe reconciliation.

## Runtime ownership and readiness

```text
prepared -> approved -> starting -> loading -> loaded
                           |          |          |
                           +----------+----------+-> stopping -> unloaded/cancelled/expired
                           |          |          |
                           +----------+----------+-> failed/unknown
```

Before launch Camol rehashes the verified artifact and executable, rejects an
occupied port, and durably records a one-shot load intent. A private helper claims
that intent once and owns the actual child process. It sets explicit offline,
loopback, single-slot, no-agent/no-tools/no-model-autoload/no-warmup options and a
fresh per-load alias. It passes no ambient HF/provider tokens, proxy configuration,
Python module paths, user model flags, or loader environment overrides. Runtime
stdout/stderr is discarded; structured receipt metadata contains only digests,
approved file identity, state changes, timing, process identity, and exit status.
Raw server output, prompts, responses, and API-key values are not journaled.

Readiness failures retain an attempt count, timestamp, and fixed phase/error-kind
metadata (`listener`, `credential`, `health`, `models`, `properties`, or `identity`).
They do not retain raw exception messages or provider content. This distinguishes
an unobservable listener from HTTP/readback failure without loosening authority
or hiding a startup failure behind an increased deadline.

Camol verifies that the expected child owns the loopback listener before sending
its generated API key (Linux `/proc` or macOS system `lsof`; unsupported observation
fails closed). `/health`, authenticated `/v1/models`, and `/props` must agree with
the exact alias, model path, one slot, and nonsleeping state. Redirects, ambiguous
JSON/headers, oversized responses, missing lengths, and slow-drip peers fail closed.
Readback uses an absolute I/O deadline, not an indefinitely reset socket timeout.

The receipt explicitly separates:

- `downloaded: verified_at_load`: file bytes matched the approved download when loaded;
- `loaded: observed`: a fresh owned runtime reported successful residency;
- `inference_ready: unverified`: **no inference probe was executed**;
- `task_ready: unverified`: no workflow capability or suitability proof exists.

The requested file hash is **not** a provider-attested loaded-weight digest.
`provider_weight_digest` remains null. File hashes are checked before and after
spawn; listener/path/alias checks identify the observed process, not its hardware,
shared libraries, kernel, model quality, or behavior under an adversarial same-OS-user
writer. A compatible native llama.cpp binary is trusted local executable code, not
a sandboxed hostile plugin. This adapter does not prove confinement of arbitrary
subprocesses a substituted/hostile executable may create.

There is currently **no automatic Camol planner/worker handoff** for this owned
host. Its generated private API key and per-load alias are not consumed by the
existing unauthenticated local-conversation/Codex-OSS profiles. In particular,
`/model local` does not turn this lifecycle receipt into a usable authenticated
planner connection. The separate [owner-approved inference bridge](local-model-inference.md)
can now submit one exact approved prompt through an internal private credential
reference, but does not automatically connect the planner/worker. Removing
authentication is not a supported workaround.

## Cancellation and uncertain outcomes

The helper checks private durable stop requests and records heartbeats. An explicit
unload, cancellation event, Ctrl+C during load, readiness loss, startup deadline, or
lifetime expiry drains only the child the helper actually created: terminate,
bounded grace, kill if necessary, then reap. The load cancellation path waits for
its helper to settle within the bounded grace; an unsettled result is unknown.
Closing a normal CLI/session does not unload a successfully loaded process.

If the helper disappears, a launch outcome is lost, or termination cannot be
confirmed, the status is `unknown`, not safely unloaded. Camol does not kill a saved
PID or silently restart the model. This one-shot plan stays consumed; unresolved
operations block additional loads through that host ledger. No automatic orphan
reconciliation is implemented: inspect and resolve the recorded process situation
through the owner/OS, preserving the evidence. Submitting another unload cannot
pretend a dead helper received it or that an unobserved process stopped.

The helper's lifetime deadline is not an OS-level guarantee after helper death or
machine suspension. CPU/GPU/context settings are runtime requests, not cgroup,
RAM/VRAM, hardware isolation, provider-token, or on-wire billing ceilings. Offline
flags and scrubbed environment are not a kernel-enforced network sandbox. Plans
requiring those stronger guarantees need another execution backend; this lifecycle
does not upgrade task/provider readiness or publish capacity automatically.

## Official interface references

Interface reviewed 2026-09-07 against the upstream
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md):
local file/alias/offline options, API-key-file, health/model/properties readback,
single-slot operation, optional warmup, and disabled idle sleep. See the upstream
[health endpoint](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#get-health-returns-health-check-result),
[model information](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#get-v1models-openai-compatible-model-info-api),
and [sleeping behavior](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#sleeping-on-idle).
Actual supported flags are a property of the owner-pinned executable. Unsupported
runtime flags fail the load; Camol never retries with a weaker command line.
