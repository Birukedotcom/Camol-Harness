# Classifier lab and Qwen routed chat

An observation-only Camol terminal preview compares GLiClass-Small and GLiClass-Qwen-0.5B on the
same user request, task state and candidate procedures. It does not execute
routes, provision workers, call a generative LLM, or change Camol's live router.
This is the local test environment for the [routing proposal](jev-integration.md).
The separate `chat` mode below connects Qwen routing to an actual generative LLM
and saves conversation, routing and provider-call logs.

## Qwen → prompt → LLM

After setup, launch the Camol-native routed chat:

```sh
scripts/classifier-lab chat --llm claude:sonnet
```

GLiClass Qwen 0.5B runs locally on CPU and suggests a procedure. Camol assembles
a prompt containing the original request, explicit task context and advisory
procedure guidance; Claude generates the answer using your existing Claude CLI
login. This mode loads only Qwen. It does not load Small or call a generative
Qwen model. If the classifier abstains, the original request still reaches the
LLM. An incorrect route must yield to the user's explicit instructions. This
reduces unnecessary rewriting but cannot guarantee that an LLM never drifts.

Claude requires an authenticated `claude` CLI on PATH. If needed, authenticate
with the CLI's own `claude auth login` flow. Camol does not collect a key. For an
already running local OpenAI-compatible server, use its actual served model ID:

```sh
scripts/classifier-lab chat --llm local:YOUR_SERVED_MODEL --endpoint http://127.0.0.1:11434/v1
```

Replace `YOUR_SERVED_MODEL` with the installed model's name. The endpoint must be
numeric loopback; proxies and redirects are not used. A server must already be
running. The classifier itself never downloads weights during chat. Claude
generation uses the network and sends your request/context/recent conversation
to Claude; a local generator stays on the configured local endpoint. The earlier
network-blocked `run-safe` preview cannot connect to Claude. Use the chat launcher
above instead; it is a separate mode, not a change to that sandbox.

Enter a request or press F2 for this example:

> Create an implementation plan for a browser-based 3D ping-pong game controlled
> with Left/Right arrows or A/D. Include an AI opponent, ball physics, scoring,
> restart controls, milestones and acceptance tests. Plan only; do not write code yet.

The native view shows the Qwen scores, generated response, resolved generator
identity and token usage when reported. `/model` reports the selected provider;
`/model claude:sonnet` or `/model local:MODEL` changes it. The selection survives
restart unless `--llm` overrides it. `/state TEXT` saves explicit task context;
the classifier also receives a bounded previous user request for follow-ups.
Overlength classifier inputs are rejected visibly rather than silently truncated.

`/prompt` displays the last assembled request payload (the provider adapter adds
Camol's planning system prompt and bounded conversation history). `/history`
shows saved messages; restarting restores recent messages. `/clear` starts a
new conversation while retaining audit files. `/cancel` interrupts generation;
the provider may already have consumed tokens. `/quit` exits.

Logs live under `.camol/classifier-lab/chat-state/projects/`, separate from live
harness sessions. The exact directory appears at startup and in `/help`:

- `session.json` and `transcript.jsonl`: saved conversation and notices.
- `proposal-events.jsonl`: classifier scores, exact assembled prompt and digest,
  or a routing failure that did not reach the generator.
- `planning-calls.jsonl`: provider/model, completed/failed/cancelled status,
  duration and token usage when available, joined to the prompt by call ID.

Logs are private local files, excluded from Git, and contain conversation text.
The generation adapter runs in a separate empty chat workspace. Claude's
runtime-verified no-tools configuration and the local adapter's no-tools
request/response checks apply. This chat generates text and proposed code;
it does not edit the repository, run tasks, provision MAGI, or deploy anything.
Live harness execution/approval/login commands are unavailable.

On the tested Mac, a live Qwen → Claude request produced the 3D ping-pong plan
with arrows/A-D, AI, physics, scoring, restart, milestones and acceptance tests.
The installed CLI resolved `sonnet` to `claude-sonnet-5`. Qwen abstained on that
request (top `explain` score 0.462, below 0.55), and the original request reached
Claude through the abstention path. This is useful end-to-end connectivity
evidence, not a correct-classification claim or a game execution test.
A second live request, "Review this pull request for bugs and regressions,"
was accepted as `review_changes`; Claude asked for the missing diff rather than
inventing review findings. Sanitized synthetic requests, routes, prompts and
responses are recorded in [the live smoke evidence](../evidence/classifier-lab/routed-chat-smoke.json).

The live test also exposed a Claude adapter issue: its coding plan mode could
produce only a preamble about writing a file. No-tools calls now replace the
coding system prompt with Camol's planning instructions and disable plan-file
workflow behavior while keeping all tools disabled. Error/turn-limit results
are rejected as incomplete; streaming tool attempts are rejected too. The main
model identity comes from the CLI's initialization event when auxiliary models
also appear in usage. Input token totals include reported cache reads/writes.

## Start

Run from this checkout. Setup needs [uv](https://docs.astral.sh/uv/getting-started/installation/)
and network access to install a separate Python 3.12 environment and download
the two public model checkpoints. It then runs basic semantic and label-order
checks. No account token or hosted inference is required.

```sh
scripts/classifier-lab setup
scripts/classifier-lab
```

The default now opens Camol's native terminal interface: its boot artwork,
green theme, transcript, multiline composer and keyboard controls, with a
comparison panel for Small and Qwen. It reuses `CamolApp`, not a separate line
prompt. This preview has its own in-memory controller and does not attach to
your durable harness session or expose the live command engine.

The environment lives in `.camol/classifier-lab/runtime`, independent of Camol's
normal dependencies. Weights use the usual Hugging Face cache. The original
float32 weight files total roughly 2.6 GB, plus dependencies and tokenizer files;
the Qwen quantized export is deliberately excluded (see compatibility below).
Setup was exercised on an Apple Silicon Mac with 48 GiB memory. Inference is
CPU-only with four threads by default; startup/loading and warm-up take longer
than the reported per-prompt timing. Linux/Windows runtime compatibility and
accelerator performance have not been measured. The launcher is a POSIX shell
script; on Windows use the Python entry point in an equivalent environment.

Type prompts to see both models' proposed route, alternative scores and latency.
Enter compares; Shift+Enter adds a line. F2 tries a review example; Ctrl-L clears
the view. A narrow terminal stacks the comparison below the transcript.
Interactive commands:

- `/state The current task is reviewing PR 5.` supplies context to both models.
- `/clear` removes context, the transcript and displayed results.
- `/routes` displays the candidate procedure descriptions.
- `/example` compares a sample review request.
- `/cancel` discards a pending comparison after its local computation returns.
- `/quit` exits. Ctrl-C also exits.

No interactive prompt history is written by the lab. `/run`, `/approve`, `/login`
and other live harness commands are unavailable in this preview. Classification
does not generate an LLM answer or authorize a proposed action.

The earlier line interface remains available explicitly:

```sh
scripts/classifier-lab interactive
```

In line mode, `/clear` only clears context and Ctrl-D also exits.

## Compare and evaluate

```sh
scripts/classifier-lab compare "Investigate why the worker stopped responding."
scripts/classifier-lab compare "Do it." --state "The assistant offered to review PR 5."
scripts/classifier-lab bench --repeat 3 --output .camol/classifier-lab/benchmark.json
scripts/classifier-lab doctor
```

Use `--json` for machine-readable stdout. Diagnostics go to stderr. `--models
small` or `--models qwen` selects one model in line/compare/benchmark modes;
the native comparison view requires both. `--threads`
changes the CPU thread count. Once setup completes, inference uses local files
only and sets the Hugging Face/Transformers offline flags. It never downloads
missing weights implicitly.

`--labels FILE` accepts the structure in
[`routes.json`](../examples/classifier-lab/routes.json): a version and 2–25 unique
route IDs with natural-language descriptions. `--cases FILE` accepts JSONL:

```json
{"id":"review-1","text":"Review this diff for bugs.","state":"Optional observed context","expected":"review_changes"}
```

The default [36-case corpus](../examples/classifier-lab/cases.jsonl) includes ten
routes, state-dependent follow-ups, negation, quoted instructions and a typo-heavy
request. It is a handwritten exploratory fixture, not a held-out production
benchmark. Do not tune labels/thresholds on these cases and then claim improved
generalization from the same cases. Add a separate held-out set from actual
tasks before choosing a production model.

Reports include pinned checkpoint revisions, package versions, attention mode,
label/case hashes, thresholds, CPU thread count, load time, all scores and logits,
and a confusion matrix. Prompt latency includes tokenization and inference;
model loading and one initial warm-up forward pass are excluded. Models run
sequentially and alternate order across cases/repetitions. Repetitions measure
timing and consistency, not additional independent accuracy samples.

The single-route score is softmax over the current candidate logits. It is
**not a calibrated probability of correctness**. The default acceptance rule
requires a top score of at least 0.55 and a lead of at least 0.15; these are
provisional lab settings, not validated thresholds. Change them with `--threshold`
and `--margin`. The report distinguishes:

- Top-1 accuracy: the raw winning label, including abstained cases.
- Coverage: the fraction accepted by the score/margin rule.
- Accepted accuracy and wrong accepted: whether the rule still lets mistakes through.
- Repeat disagreements: cases whose raw winning label changes across repetitions.

`clarify` is a candidate route; abstention is a separate numeric decision and
appears as `ABSTAIN`/`proposed_route: null`. Neither grants execution authority.
The CLI rejects reserved model delimiters, invalid/nonfinite outputs and inputs
over 512 tokens including label descriptions. It never silently truncates state.
Explicit `--output` saves supplied prompts/state locally; do not commit private
reports. Interactive mode does not persist them.

## Qwen compatibility correction

The [Qwen model card](https://huggingface.co/knowledgator/gliclass-qwen-0.5B-v1.0)
describes a bidirectional LLM2Vec encoder and requests Transformers 4.44.1.
With the pinned GLiClass 0.1.7 / LLM2Vec 0.2.3 stack, Qwen's modified attention
sets `is_causal=False` but inherits the causal mask builder. Local tests showed
it choosing the first label on unrelated topic inputs. The published INT8 ONNX
file also failed these basic checks; no causal diagnosis of its graph is assumed.

The lab uses the original safetensors weights and an explicit local subclass
that supplies a full bidirectional attention mask while preserving padding
masks. It disables caching and rejects cached decoding. It changes no weights
or installed package files. The override is scoped to constructing this Qwen
instance; the registry is restored afterward. Reports record
`qwen_attention: bidirectional`. These results therefore describe this corrected
runtime, not the unmodified legacy package or quantized export.

Reproduce the uncorrected control (expected to fail topic checks):

```sh
scripts/classifier-lab doctor --models qwen --qwen-attention upstream
```

`doctor` checks travel, sports and science with both normal and reversed label
order. Failure exits nonzero. Passing this small check proves basic sensitivity
to content; it does not prove useful Camol routing accuracy.

## Verified baseline

On 2026-09-20 UTC, using CPU float32, four threads, both pinned checkpoints and
the unchanged 36-case corpus, three repetitions agreed on every top label.
The [recorded results](../evidence/classifier-lab/baseline.json) include runtime
versions, source/label/case hashes and per-case decisions:

| Model | Correct unique cases | Coverage | Accuracy among accepted | Wrong accepted unique cases | Median / p95 |
| --- | --- | --- | --- | --- | --- |
| Small | 19/36 (52.8%) | 38.9% | 13/14 (92.9%) | 1 | 23.0 / 28.8 ms |
| Qwen, corrected attention | 25/36 (69.4%) | 36.1% | 12/13 (92.3%) | 1 | 90.0 / 103.6 ms |

Small accepted an implementation request as `explain`; Qwen accepted “Fix that
thing.” as `repair_environment`. Both missed vague/out-of-domain requests and
some distinctions between procedures. These results do **not** justify enabling
automatic routing. Both passed all six topic/order checks after the correction.
The next evaluation should use independently labeled real task examples,
including state-dependent and out-of-scope requests, and measure results for
each procedure before connecting classifier proposals to Camol's policy gates.

## Developer checks

Ordinary tests do not download models or require ML dependencies:

```sh
python3 -m unittest tests.test_classifier_lab
python3 -m unittest tests.test_classifier_preview tests.test_classifier_tui
CAMOL_CLASSIFIER_LIVE=1 .camol/classifier-lab/runtime/bin/python -m unittest tests.live.test_classifier_lab_models -v
```

The opt-in tests require setup and use cached files only. They check semantic
sensitivity, label reversal, refusal to truncate, and full attention/padding
behavior. Dependencies and model revisions are pinned. Regenerate the dependency
lock intentionally with the command recorded at its top, then rerun these checks
and the corpus; package upgrades can change this legacy model's behavior.

The preview controller tests also cover blocked execution/login commands,
context propagation, cancellation, secret rejection and error handling. Native
UI tests exercise the real Camol composer/theme, both result cards, the narrowed
layout and the preview-only command palette. They require the `tui` extra but
not model weights. A real-weight headless UI check also confirmed that the same
review request populated both cards successfully.
