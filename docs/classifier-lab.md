# Local classifier lab

An observation-only CLI compares GLiClass-Small and GLiClass-Qwen-0.5B on the
same user request, task state and candidate procedures. It does not execute
routes, provision workers, call a generative LLM, or change Camol's live router.
This is the local test environment for the [routing proposal](jev-integration.md).

## Start

Run from this checkout. Setup needs [uv](https://docs.astral.sh/uv/getting-started/installation/)
and network access to install a separate Python 3.12 environment and download
the two public model checkpoints. It then runs basic semantic and label-order
checks. No account token or hosted inference is required.

```sh
scripts/classifier-lab setup
scripts/classifier-lab
```

The environment lives in `.camol/classifier-lab/runtime`, independent of Camol's
normal dependencies. Weights use the usual Hugging Face cache. The original
float32 weight files total roughly 2.6 GB, plus dependencies and tokenizer files;
the Qwen quantized export is deliberately excluded (see compatibility below).
Setup was exercised on an Apple Silicon Mac with 48 GiB memory. Inference is
CPU-only with four threads by default; startup/loading and warm-up take longer
than the reported per-prompt timing. Linux/Windows runtime compatibility and
accelerator performance have not been measured. The launcher is a POSIX shell
script; on Windows use the Python entry point in an equivalent environment.

Type prompts to see both models' proposed route, top three scores and latency.
Interactive commands:

- `/state The current task is reviewing PR 5.` supplies context to both models.
- `/clear` removes that context.
- `/routes` displays the candidate procedure descriptions.
- `/quit` exits. Ctrl-C also exits; Ctrl-D ends input.

No interactive prompt history is written by the lab.

## Compare and evaluate

```sh
scripts/classifier-lab compare "Investigate why the worker stopped responding."
scripts/classifier-lab compare "Do it." --state "The assistant offered to review PR 5."
scripts/classifier-lab bench --repeat 3 --output .camol/classifier-lab/benchmark.json
scripts/classifier-lab doctor
```

Use `--json` for machine-readable stdout. Diagnostics go to stderr. `--models
small` or `--models qwen` selects one model; both run by default. `--threads`
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
CAMOL_CLASSIFIER_LIVE=1 .camol/classifier-lab/runtime/bin/python -m unittest tests.live.test_classifier_lab_models -v
```

The opt-in tests require setup and use cached files only. They check semantic
sensitivity, label reversal, refusal to truncate, and full attention/padding
behavior. Dependencies and model revisions are pinned. Regenerate the dependency
lock intentionally with the command recorded at its top, then rerun these checks
and the corpus; package upgrades can change this legacy model's behavior.
