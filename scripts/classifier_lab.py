#!/usr/bin/env python3
"""Local, observation-only comparison of pinned GLiClass classifiers.

Heavy dependencies are imported only when models are loaded. Nothing here can
dispatch a Camol task or grant permission to execute a suggested route.
"""

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples" / "classifier-lab"
MODELS = {
    "small": {
        "repo": "knowledgator/gliclass-small-v1.0",
        "revision": "21edefaf7951f68c68c505f9139ba536d3b448f7",
    },
    "qwen": {
        "repo": "knowledgator/gliclass-qwen-0.5B-v1.0",
        "revision": "3d6a39e2d09c04288d90dce7e8d9b3a1d3466eb1",
    },
}
FILES = ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
         "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt", "spm.model"]
RESERVED = ("<<LABEL>>", "<<SEP>>")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be nonempty text".format(name))
    if any(marker in value for marker in RESERVED):
        raise ValueError("{} contains a reserved classifier delimiter".format(name))
    return value


def load_labels(path):
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("routes"), list):
        raise ValueError("labels must contain a routes array")
    routes = payload["routes"]
    if not 2 <= len(routes) <= 25:
        raise ValueError("supply between 2 and 25 routes")
    ids, descriptions = set(), set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("each route must be an object")
        identifier = nonempty(route.get("id"), "route id")
        description = nonempty(route.get("description"), "route description").lower()
        if identifier in ids or description in descriptions:
            raise ValueError("route ids and descriptions must be unique")
        ids.add(identifier)
        descriptions.add(description)
    return payload


def load_cases(path, routes):
    cases, ids = [], set()
    allowed = {route["id"] for route in routes}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict):
            raise ValueError("case at line {} must be an object".format(number))
        identifier = nonempty(case.get("id"), "case id")
        nonempty(case.get("text"), "case text")
        if identifier in ids or case.get("expected") not in allowed:
            raise ValueError("duplicate case id or unknown expected route at line {}".format(number))
        state = case.get("state", "")
        if not isinstance(state, str):
            raise ValueError("case state must be text")
        if state:
            nonempty(state, "case state")
        ids.add(identifier)
        cases.append(case)
    if not cases:
        raise ValueError("case file is empty")
    return cases


def input_text(text, state=""):
    nonempty(text, "prompt")
    if state:
        nonempty(state, "state")
        return "Current task state:\n{}\n\nLatest user request:\n{}".format(state, text)
    return text


def prepare_text(text, descriptions, prompt_first):
    # Matches GLiClass 0.1.7 UniEncoderZeroShotClassificationPipeline.
    labels = "".join("<<LABEL>>" + label.lower() for label in descriptions) + "<<SEP>>"
    return labels + text if prompt_first else text + labels


def decision(logits, routes, threshold, min_margin):
    if len(logits) != len(routes) or not all(math.isfinite(x) for x in logits):
        raise ValueError("model returned invalid logits")
    weights = [math.exp(value - max(logits)) for value in logits]
    total = sum(weights)
    ranked = sorted([
        {"route": route["id"], "score": weight / total, "logit": float(logit)}
        for route, weight, logit in zip(routes, weights, logits)
    ], key=lambda row: row["score"], reverse=True)
    margin = ranked[0]["score"] - ranked[1]["score"]
    reasons = []
    if ranked[0]["score"] < threshold:
        reasons.append("low_score")
    if margin < min_margin:
        reasons.append("small_margin")
    return {
        "top_route": ranked[0]["route"],
        "proposed_route": None if reasons else ranked[0]["route"],
        "abstained": bool(reasons), "reasons": reasons, "margin": margin,
        "scores": ranked,
    }


def bidirectional_qwen_class():
    """Restore LLM2Vec's full attention with the pinned Transformers 4.44.1.

    llm2vec 0.2.3 marks Qwen attention as non-causal but inherits Qwen2Model's
    causal mask builder. Even SDPA receives that triangular mask. This local
    subclass overrides only mask construction; weights remain unchanged.
    No KV-cache decoding or batched generation is supported by this lab.
    """
    import torch
    from llm2vec.models import Qwen2BiModel

    class BidirectionalQwen2(Qwen2BiModel):
        def _update_causal_mask(self, attention_mask, input_tensor, cache_position,
                                past_key_values, output_attentions):
            if past_key_values is not None and past_key_values.get_seq_length():
                raise ValueError("classifier does not support cached decoding")
            batch, length, _ = input_tensor.shape
            mask = torch.zeros((batch, 1, length, length), dtype=input_tensor.dtype,
                               device=input_tensor.device)
            if attention_mask is not None:
                if tuple(attention_mask.shape) != (batch, length):
                    raise ValueError("expected a two-dimensional padding mask")
                mask.masked_fill_(attention_mask[:, None, None, :] == 0,
                                  torch.finfo(input_tensor.dtype).min)
            return mask

    return BidirectionalQwen2


class Classifier:
    def __init__(self, name, threads=4, qwen_attention="bidirectional"):
        import torch
        from gliclass import GLiClassModel
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer

        self.name = name
        self.torch = torch
        torch.set_num_threads(threads)
        began = time.perf_counter()
        spec = MODELS[name]
        path = snapshot_download(spec["repo"], revision=spec["revision"], local_files_only=True)
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        from gliclass.model import DECODER_MODEL_MAPPING
        original = DECODER_MODEL_MAPPING["Qwen2Config"]
        try:
            if name == "qwen" and qwen_attention == "bidirectional":
                DECODER_MODEL_MAPPING["Qwen2Config"] = bidirectional_qwen_class()
            # Upstream prints a decoder message on stdout; preserve JSON stdout.
            with contextlib.redirect_stdout(sys.stderr):
                model, loading = GLiClassModel.from_pretrained(
                    path, local_files_only=True, use_safetensors=True,
                    torch_dtype=torch.float32, output_loading_info=True,
                )
                if any(loading.get(key) for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
                    raise ValueError("checkpoint did not load completely: {}".format(loading))
                self.model = model.to("cpu").eval()
        finally:
            DECODER_MODEL_MAPPING["Qwen2Config"] = original
        self.model.config.encoder_config.use_cache = False
        if name == "qwen":
            self.model.model.encoder_model.config.use_cache = False
        self.load_ms = (time.perf_counter() - began) * 1000
        self.warmed = False

    def predict(self, text, routes, state="", threshold=0.55, min_margin=0.15):
        began = time.perf_counter()
        assembled = prepare_text(input_text(text, state), [r["description"] for r in routes],
                                 self.model.config.prompt_first)
        encoded = self.tokenizer(assembled, truncation=False, return_tensors="pt")
        count = encoded["input_ids"].shape[1]
        if count > 512:
            raise ValueError("{} input is {} tokens including labels; maximum is 512. "
                             "Shorten prompt/state/labels; nothing was truncated.".format(self.name, count))
        markers = (encoded["input_ids"] == self.model.config.class_token_index).sum().item()
        if markers != len(routes):
            raise ValueError("label token count mismatch")
        tokenized_ms = (time.perf_counter() - began) * 1000
        with self.torch.inference_mode():
            # Warm up once per loaded model, excluded from reported latency.
            if not self.warmed:
                self.model(**encoded)
                self.warmed = True
            began = time.perf_counter()
            logits = self.model(**encoded).logits[0].tolist()
            inference_ms = (time.perf_counter() - began) * 1000
        result = decision(logits, routes, threshold, min_margin)
        result.update(model=self.name, tokens=count, inference_ms=inference_ms,
                      latency_ms=inference_ms + tokenized_ms)
        return result


def compare(models, text, routes, state, threshold, margin):
    return [model.predict(text, routes, state, threshold, margin) for model in models]


def summarize(rows):
    correct = sum(row["top_route"] == row["expected"] for row in rows)
    accepted = [row for row in rows if not row["abstained"]]
    accepted_correct = sum(row["proposed_route"] == row["expected"] for row in accepted)
    confusion = {}
    for row in rows:
        counts = confusion.setdefault(row["expected"], {})
        counts[row["top_route"]] = counts.get(row["top_route"], 0) + 1
    times = sorted(row["latency_ms"] for row in rows)
    by_case = {}
    for row in rows:
        by_case.setdefault(row["case_id"], set()).add(row["top_route"])
    return {
        "predictions": len(rows), "unique_cases": len(by_case),
        "top1_accuracy": correct / len(rows), "correct": correct,
        "coverage": len(accepted) / len(rows), "accepted": len(accepted),
        "accepted_accuracy": accepted_correct / len(accepted) if accepted else None,
        "wrong_accepted": len(accepted) - accepted_correct,
        "median_ms": statistics.median(times), "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
        "repeat_disagreements": sum(len(values) > 1 for values in by_case.values()),
        "confusion": confusion,
    }


def metadata(args, labels):
    versions = {}
    for package in ("torch", "gliclass", "transformers", "llm2vec", "tokenizers", "huggingface-hub"):
        versions[package] = importlib.metadata.version(package)
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dependency_lock_sha256": hashlib.sha256((ROOT / "scripts/classifier-lab-requirements.txt").read_bytes()).hexdigest(),
        "models": {name: MODELS[name] for name in args.models},
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "device": "cpu", "precision": "float32", "threads": args.threads, "packages": versions,
                    "qwen_attention": args.qwen_attention},
        "policy": {"threshold": args.threshold, "min_margin": args.margin, "max_tokens": 512,
                   "score": "softmax over candidate logits; not calibrated confidence"},
        "labels": labels, "labels_sha256": digest(labels), "observation_only": True,
    }


def benchmark(models, cases, routes, args):
    rows = {model.name: [] for model in models}
    for repetition in range(args.repeat):
        for index, case in enumerate(cases):
            # Alternate model order to reduce systematic timing-order effects.
            ordered = models if (index + repetition) % 2 == 0 else list(reversed(models))
            for model in ordered:
                row = model.predict(case["text"], routes, case.get("state", ""), args.threshold, args.margin)
                row.update(case_id=case["id"], expected=case["expected"], repetition=repetition)
                rows[model.name].append(row)
    return {"cases": cases, "cases_sha256": digest(cases), "repeat": args.repeat,
            "results": rows, "summary": {name: summarize(values) for name, values in rows.items()}}


def doctor(models):
    """Catch label-position collapse using content and label-order changes."""
    routes = [{"id": name, "description": name} for name in ("travel", "sport", "science", "politics")]
    cases = [("The soccer team won the match.", "sport"),
             ("Scientists discovered a new planet.", "science"),
             ("I booked a flight to Paris.", "travel")]
    rows = []
    for model in models:
        for text, expected in cases:
            for order in (routes, list(reversed(routes))):
                result = model.predict(text, order)
                rows.append({"model": model.name, "text": text, "expected": expected,
                             "label_order": [r["id"] for r in order],
                             "actual": result["top_route"], "passed": result["top_route"] == expected})
    return {"passed": all(row["passed"] for row in rows), "checks": rows}


def safe_display(text):
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in str(text))


def show_comparison(results):
    for row in results:
        proposed = row["proposed_route"] or "ABSTAIN"
        top = ", ".join("{} {:.3f}".format(safe_display(item["route"]), item["score"])
                        for item in row["scores"][:3])
        print("{:<6} {:<22} {:7.1f} ms | {}".format(row["model"], safe_display(proposed), row["latency_ms"], top))


def show_benchmark(report):
    print("Model   Top-1 correct   Coverage   Accepted accuracy   Wrong accepted   Median / p95")
    for name, summary in report["summary"].items():
        accuracy = "n/a" if summary["accepted_accuracy"] is None else "{:.1%}".format(summary["accepted_accuracy"])
        print("{:<7} {:>3}/{:<4} {:>6.1%}     {:>6.1%}           {:>6}          {:>3}       {:.1f} / {:.1f} ms".format(
            name, summary["correct"], summary["predictions"], summary["top1_accuracy"],
            summary["coverage"], accuracy, summary["wrong_accepted"], summary["median_ms"], summary["p95_ms"]))
    print("\nMisclassifications (first repetition):")
    for name, rows in report["results"].items():
        for row in rows:
            if row["repetition"] == 0 and row["top_route"] != row["expected"]:
                print("  {} {}: expected {}, got {}{}".format(name, safe_display(row["case_id"]),
                      safe_display(row["expected"]), safe_display(row["top_route"]),
                      " (abstained)" if row["abstained"] else ""))
    print("\nHandwritten exploratory cases; repeated runs are not independent accuracy samples.")


def save_report(path, report):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit output is opt-in and may contain the supplied prompts/state.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def interactive(models, routes, args):
    state = args.state
    print("Camol classifier lab — local CPU, observation only; scores are not calibrated confidence.")
    print("Enter a prompt. Commands: /state TEXT, /clear, /routes, /quit. No prompt history is saved.")
    while True:
        try:
            text = input("\nroute> ").strip()
        except EOFError:
            print()
            break
        if text in ("/quit", "/exit"):
            break
        if not text:
            continue
        if text == "/routes":
            for route in routes:
                print("{}: {}".format(safe_display(route["id"]), safe_display(route["description"])))
            continue
        if text == "/clear":
            state = ""
            print("State cleared.")
            continue
        if text.startswith("/state "):
            state = text[7:]
            print("State updated for both models.")
            continue
        try:
            show_comparison(compare(models, text, routes, state, args.threshold, args.margin))
        except ValueError as exc:
            print("Input rejected: {}".format(exc), file=sys.stderr)


def tui(args, labels):
    # Reuse Camol's renderer and composer. This controller has no live harness
    # command engine, durable session, execution adapter or provider credentials.
    sys.path.insert(0, str(ROOT))
    from camol.classifier_preview import ClassifierPreviewController
    from camol.classifier_tui import run_preview

    models = []

    def warm():
        if not models:
            loaded = [Classifier(name, args.threads, args.qwen_attention) for name in args.models]
            models.extend(loaded)

    def predict(text, state):
        warm()
        return compare(models, text, labels["routes"], state, args.threshold, args.margin)

    controller = ClassifierPreviewController(predict, labels["routes"], warm=warm)
    controller.state = args.state
    return run_preview(controller, show_boot=not args.no_boot)


def bounded_float(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("must be a finite number between 0 and 1")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("tui", "interactive", "compare", "bench", "doctor", "download"), default="tui")
    parser.add_argument("text", nargs="?", help="prompt for compare")
    parser.add_argument("--state", default="", help="observed task state supplied to both models")
    parser.add_argument("--models", nargs="+", choices=tuple(MODELS), default=list(MODELS))
    parser.add_argument("--labels", type=Path, default=DATA / "routes.json")
    parser.add_argument("--cases", type=Path, default=DATA / "cases.jsonl")
    parser.add_argument("--threshold", type=bounded_float, default=0.55)
    parser.add_argument("--margin", type=bounded_float, default=0.15)
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--repeat", type=positive_int, default=1)
    parser.add_argument("--qwen-attention", choices=("bidirectional", "upstream"), default="bidirectional",
                        help="upstream reproduces the known legacy Qwen causal-mask defect")
    parser.add_argument("--output", type=Path, help="save JSON, including input text/state")
    parser.add_argument("--json", action="store_true", help="machine-readable stdout for compare/bench")
    parser.add_argument("--no-boot", action="store_true", help="skip Camol boot art in the terminal preview")
    args = parser.parse_args(argv)
    if len(set(args.models)) != len(args.models):
        parser.error("models must be unique")
    if args.mode == "compare" and not args.text:
        parser.error("compare requires a quoted prompt")
    if args.mode != "compare" and args.text:
        parser.error("a prompt argument is only supported by compare")
    if args.mode == "bench" and args.state:
        parser.error("put per-case state in the benchmark JSONL instead of --state")
    if args.mode in {"interactive", "tui"} and (args.json or args.output):
        parser.error("--json and --output are supported by compare/bench/doctor")
    if args.mode == "tui" and args.models != list(MODELS):
        parser.error("the native comparison view requires both models; use compare/interactive for a single model")
    if args.mode == "tui" and args.qwen_attention != "bidirectional":
        parser.error("the native comparison view requires corrected Qwen attention; use doctor for the upstream control")
    try:
        if args.qwen_attention == "upstream" and "qwen" in args.models:
            print("WARNING: uncorrected Qwen control has a known causal-mask defect; diagnostic results only.", file=sys.stderr)
        if args.mode == "download":
            from huggingface_hub import snapshot_download
            for name in args.models:
                spec = MODELS[name]
                print("Downloading pinned {} weights...".format(name), file=sys.stderr)
                snapshot_download(spec["repo"], revision=spec["revision"], allow_patterns=FILES, token=False)
            return 0
        # Inference must work without network access or inherited account tokens.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        labels = load_labels(args.labels)
        routes = labels["routes"]
        if args.mode == "tui":
            return tui(args, labels)
        cases = load_cases(args.cases, routes) if args.mode == "bench" else None
        if args.mode == "compare":
            input_text(args.text, args.state)
        models = []
        for name in args.models:
            print("Loading {} (CPU)...".format(name), file=sys.stderr)
            models.append(Classifier(name, args.threads, args.qwen_attention))
        if args.mode == "interactive":
            interactive(models, routes, args)
            return 0
        report = metadata(args, labels)
        report["load_ms"] = {model.name: model.load_ms for model in models}
        if args.mode == "doctor":
            report.update(doctor(models))
        elif args.mode == "compare":
            report.update(text=args.text, state=args.state,
                          results=compare(models, args.text, routes, args.state, args.threshold, args.margin))
        else:
            report.update(benchmark(models, cases, routes, args))
        if args.output:
            save_report(args.output, report)
            print("Saved {}".format(args.output), file=sys.stderr)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        elif args.mode == "doctor":
            for row in report["checks"]:
                print("{} {}: expected {}, got {} ({} first)".format(
                    "PASS" if row["passed"] else "FAIL", row["model"], row["expected"],
                    row["actual"], row["label_order"][0]))
        elif args.mode == "compare":
            show_comparison(report["results"])
            print("Scores are relative to these labels, not calibrated confidence. No action executed.")
        else:
            show_benchmark(report)
        return 1 if args.mode == "doctor" and not report["passed"] else 0
    except (ValueError, OSError, ImportError, RuntimeError) as exc:
        print("Classifier lab error: {}\nRun scripts/classifier-lab setup if dependencies or weights are missing.".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
