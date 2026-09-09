# Explicit inference on a Camol-owned local host

`camol.model_inference` adds a narrow authenticated inference operation to the
[owned llama.cpp lifecycle](local-model-hosting.md). It does not download or load
a model, touch an existing Ollama/LM Studio server, select arbitrary endpoints,
or silently connect the orchestrator. The host must already have an approved,
freshly observed load in its private ledger.

This implementation has been tested with tiny real subprocess/loopback HTTP
fixtures, not an actual llama.cpp model or GPU. Fixture success is not vendor,
hardware, model-quality, or workflow-readiness evidence.

## Exact one-shot authority

`ModelInferencePlan` is a strict `camol.model_inference_plan`, schema version 1:

| Field | Frozen authority |
| --- | --- |
| `operation_id` | Unique one-shot inference identity |
| `host_plan_digest`, `load_operation_id` | Exact existing host plan and load |
| `owner`, `model_alias` | Same approved owner and per-load random alias |
| `prompt_digest`, `prompt_size_bytes` | SHA-256 of exact UTF-8 prompt bytes, 1 byte–1 MiB |
| `max_output_tokens` | Requested output-token limit, 1–32,768; **not an enforced physical ceiling** |
| `timeout_seconds` | Absolute local operation deadline, 1–600 seconds |
| `issued_at`, `expires_at` | Explicit timestamp interval, positive and no more than one hour |

Unknown fields, invalid numeric types, unsupported versions, future-issued or
expired authority, and a different owner/load/alias fail closed. The effective
deadline is also clipped by the remaining owned-host lifetime. The prompt file
is read through a nonblocking, no-follow regular-file descriptor, bounded, checked
for mutation, and compared with the approved hash and length. No prompt-file path
is stored in the contract or ledger. UTF-8 bytes are not trimmed or normalized.

Preparation and approval do not issue HTTP requests or read the API key. Approval
requires the exact inference-plan digest through the trusted owner control path.
It does not follow from approval to download or load the model. The private
directory and owner-name controls share the lifecycle's trusted-local-owner model;
they are not authentication for hostile clients allowed to choose an owner name.

## Embedding API

```python
from camol.model_inference import (
    ModelInference, ModelInferencePlan, prompt_file_identity, validate_prompt_file,
)

# Passive helpers: no host creation or HTTP.
fingerprint = prompt_file_identity(prompt_path)
plan = ModelInferencePlan.from_dict(reviewed_document)
validate_prompt_file(prompt_path, plan)

with ModelInference(existing_private_host_root) as inference:
    inference.prepare(plan)
    # Display plan.to_dict() and plan.digest() before this explicit owner action.
    inference.approve(plan.digest(), by=plan.owner)
    result = inference.infer(plan.digest(), by=plan.owner, prompt_path=prompt_path)
    text = result["response_text"]  # available only to this successful caller
    metadata = inference.status(plan.digest())
```

`infer(..., cancel_event=threading.Event())` supports caller cancellation.
`status`, `events`, and `inventory` are passive metadata inspection.
`ModelInference(root, read_only=True)` refuses missing stores and does not create,
chmod, recover, infer, or contact a server. The new ledger is `inference.sqlite3`
inside the existing `0700` host root, with database and per-operation locks `0600`.

Repeated invocation of a consumed operation returns metadata only, including on
success; `response_text` is then null. It never repeats the request to recover
lost text. Reapproval cannot reset consumed authority. An operation ID cannot be
rebound to changed prompt, model, owner, or limits.

## CLI

`camol model-inference --help` exposes the same explicit lifecycle. First use
`fingerprint --prompt-file FILE` to obtain the exact prompt hash and byte count,
then review a contract binding those values to an existing approved host/load.
`validate --plan PLAN_JSON --prompt-file FILE` checks the contract without
creating state, contacting the host, or proving readiness. The sequence is:

```text
camol model-inference prepare --root HOST_STATE --plan PLAN_JSON --prompt-file FILE
camol model-inference approve --root HOST_STATE --plan-digest EXACT_INFERENCE_DIGEST --by OWNER
camol model-inference infer --root HOST_STATE --plan-digest EXACT_INFERENCE_DIGEST --by OWNER --prompt-file FILE --show-response
camol model-inference status --root HOST_STATE --plan-digest EXACT_INFERENCE_DIGEST
```

Uppercase values are explicit owner-supplied paths/identities, not literal shell
commands to paste unchanged. `HOST_STATE` must already contain the owned host
lifecycle; inference does not create or load a host. The inference digest is
distinct from the host plan digest. The CLI never accepts prompt text in argv.

Response text is omitted by default. `--show-response` opts into displaying it
on the original successful invocation, which may expose sensitive text in
terminal scrollback or caller logs. Text is still not retained in the Camol
ledger; replaying a completed invocation cannot recover it or resend the prompt.
`infer` exits zero only for a completed result, otherwise two, including unknown
and in-flight receipts. Status/inventory/events are passive, noncreating reads;
successful inspection is not a claim of successful inference.

## Request, identity, and privacy boundaries

The only inference endpoint is numeric loopback `POST /v1/chat/completions` with
one user message, one nonstreaming completion, the exact alias, and `max_tokens`.
There are no tools, supplied headers, arbitrary URL/path, redirects, proxies,
ambient credentials, model download flags, or automatic retries.

Before POST, Camol checks the active helper/load identity and authenticated
`/health`, `/v1/models`, and `/props` readback. After opening the inference
connection it checks the owned listener again and resolves the private API-key
reference internally. Each authenticated readback socket also rechecks the owned
listener after connection and before sending its key; a replacement between
health and model readback does not inherit the credential. Key material is neither an API return value nor a contract
field; an exact key echoed in decoded completion text is rejected rather than
returned. This is not general data-loss prevention against arbitrary transformations
by a malicious runtime. Listener checking is an OS observation with a local TOCTOU boundary, not
cryptographic server, loaded-weight, hardware, or library attestation; a malicious
same-user owner rewriting trusted files/processes remains outside this boundary.

The ledger records intent before possible dispatch and retains only identities,
hashes, byte counts, timestamps, status, and narrowly selected usage fields.
Prompt text, response text, HTTP headers, keys, raw exceptions, and raw provider
errors are never persisted. The immediate successful caller receives text in
memory and decides what to do with it. Hashes can themselves reveal information
through guessing; retain the metadata ledger as private, not public telemetry.
The trusted local runtime also receives the prompt; this is not a claim that a
hostile executable cannot retain it in memory or elsewhere.

## Failure, cancellation, and accounting

A durable one-shot intent and process-held lock prevent concurrent/restarted
replay. An in-flight record without its live lock is reported as `unknown`.
One active or unknown invocation retains an accounting reservation for its exact
load; a different operation ID cannot silently retry through that reservation.
There is no automatic reservation clearing from cached terminal status or orphan
PID reuse. Normal successful completion permits a new separately approved request.

The client uses a bounded, absolute-deadline transport. Cancellation or timeout
closes its own socket; **closing HTTP does not prove the server stopped computing**.
Any uncertain result after potential dispatch stays `unknown`, holds its request
reservation, and is never automatically retried. Even a known HTTP error after
dispatch cannot establish zero work or zero cost. Owner-approved lifecycle unload
is separate authority, not an inferred response to an inference failure.

Each SQLite busy wait is at most 50 ms, clipped further by the active operation's
remaining deadline; there is no hidden ten-second lock wait. The separate owned
host ledger receives a durable heartbeat every 100 ms. Its DELETE-journal commit
can briefly block the client's read-only host/load snapshot. Only that
SELECT-only observation is retried on SQLite busy/locked errors: passive
prepare/approve allow at most one second, and inference uses its original
absolute deadline and cancellation signal. The inference deadline starts at
method entry, so these initial reads do not extend the request ceiling.
Authority is checked again after observation. Corruption and other SQLite
errors are distinct failures, not reasons to retry. No credential read, HTTP
readback, POST, inference-ledger write, or outcome commit is retried by this path.

Inference-ledger write contention before durable intent still fails without
dispatch. If an outcome cannot be committed after dispatch, the caller receives `unknown` with
`outcome_persisted: false`, no response text, and the durable in-flight intent
continues to hold its reservation. Camol does not manufacture a persisted success
or repeat a request to repair the missing outcome.

Metrics distinguish locally observed byte counts/timing from provider-reported
token counts. Missing or invalid usage remains unknown, not zero. Local model
cost/energy is not measured, so `total_cost_usd` remains null. If the provider
reports more output tokens than requested, the result is `failed` with
`limit_violation: true`, while preserving the actual reported usage. It never
clips accounting to the requested limit. The reservation is a request-level
accounting device, not a hard tokenizer, GPU/RAM, compute, or billing cap.

The lifecycle status still reports inference readiness as unverified: a single
separate inference receipt does not establish general task suitability or wire
the planner/worker automatically. The orchestrator's `/model local` path does not
consume this private authenticated connection.

## Interface reference

The fixed request endpoint and fields follow the upstream
[llama.cpp chat-completions interface](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api),
reviewed 2026-09-07. Actual support remains a property of the owner-pinned server
binary. Unsupported response shapes fail closed; Camol does not retry using
weaker identity checks, extra endpoints, or disabled authentication.
