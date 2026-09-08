"""Read-only orchestrator conversations over user-owned provider runtimes."""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from threading import Event

from .connections import ConnectionError, open_without_proxy, validate_loopback_endpoint
from .probes import Redactor


class ConversationError(RuntimeError):
    """The selected orchestrator provider is unavailable or returned unsafe data."""


class ConversationCancelled(ConversationError):
    """The human cancelled an in-flight planning call."""


MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SYSTEM_PROMPT = """You are the planning-only orchestrator inside Camol. Help the human clarify software work, constraints, invariants, dependencies, evaluators, and resource limits. You do not execute tools, edit files, approve plans, or claim evidence. Be concise and explicitly label assumptions. Camol itself freezes and gates the executable plan after the human runs /grill and /approve."""


@dataclass(frozen=True)
class ProviderSelection:
    provider: str
    model: Optional[str]


@dataclass(frozen=True)
class ConversationReply:
    text: str
    provider: str
    requested_model: Optional[str]
    resolved_model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]


def parse_selection(value: str) -> ProviderSelection:
    text = value.strip()
    if text == "manual":
        return ProviderSelection("manual", None)
    provider, separator, model = text.partition(":")
    if provider not in {"claude", "codex", "local", "openai"}:
        raise ConversationError("model must be manual, claude[:MODEL], codex[:MODEL], local:MODEL, or openai:MODEL")
    if not separator:
        model = "fable" if provider == "claude" else ""
    if provider in {"local", "openai"} and not model:
        raise ConversationError("{} selection requires an explicit model".format(provider))
    if model and not MODEL_PATTERN.fullmatch(model):
        raise ConversationError("model name contains unsupported characters")
    return ProviderSelection(provider, model or None)


def _prompt(message: str, history: Sequence[Mapping[str, str]]) -> str:
    transcript = []
    conversation = [
        item
        for item in history
        if item.get("kind", "conversation") == "conversation"
        and not str(item.get("content", "")).startswith("model identity:")
    ]
    for item in conversation[-20:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"human", "orchestrator"} and isinstance(content, str):
            transcript.append("{}: {}".format(role.upper(), content[:4_000]))
    return "{}\n\nConversation so far:\n{}\n\nHUMAN: {}\nORCHESTRATOR:".format(
        SYSTEM_PROMPT,
        "\n".join(transcript) if transcript else "(new conversation)",
        Redactor().text(message)[:12_000],
    )


def provider_argv(
    selection: ProviderSelection, effort: str, workspace: Path, *, stream: bool = False
) -> List[str]:
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConversationError("effort is unsupported")
    if selection.provider == "claude":
        executable = shutil.which("claude")
        if not executable:
            raise ConversationError("Claude CLI is not installed")
        argv = [
            executable, "-p", "--output-format", "stream-json" if stream else "json",
            "--model", selection.model or "fable",
            "--effort", effort, "--max-turns", "1", "--permission-mode", "plan",
            "--permission-prompts", "none", "--no-session-persistence", "--disable-slash-commands",
            "--safe-mode", "--disallowedTools", "Bash", "Edit", "Write",
        ]
        if stream:
            argv.extend(["--include-partial-messages", "--verbose"])
        return argv
    if selection.provider == "codex":
        executable = shutil.which("codex")
        if not executable:
            raise ConversationError("Codex CLI is not installed")
        argv = [
            executable, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "-C", str(Path(workspace).resolve()),
            "-c", 'model_reasoning_effort="{}"'.format(effort),
        ]
        if selection.model:
            argv.extend(["--model", selection.model])
        argv.append("-")
        return argv
    raise ConversationError("{} does not use a local CLI argv".format(selection.provider))


def _environment() -> Dict[str, str]:
    return {
        name: os.environ[name]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
        if name in os.environ
    }


def _parse_cli_reply(provider: str, stdout: bytes) -> Tuple[str, Optional[str], Optional[int], Optional[int]]:
    decoded = stdout.decode("utf-8", "replace")
    if provider == "claude":
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise ConversationError("Claude returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise ConversationError("Claude returned a non-object response")
        text = payload.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ConversationError("Claude returned no orchestrator message")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        model_usage = payload.get("modelUsage") or payload.get("model_usage")
        resolved = next(iter(model_usage)) if isinstance(model_usage, dict) and len(model_usage) == 1 else payload.get("model")
        return text, resolved if isinstance(resolved, str) else None, usage.get("input_tokens"), usage.get("output_tokens")
    texts: List[str] = []
    input_tokens = output_tokens = None
    resolved = None
    for line in decoded.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if payload.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            if isinstance(item.get("text"), str):
                texts.append(item["text"])
        if payload.get("type") == "turn.completed" and isinstance(payload.get("usage"), dict):
            input_tokens = payload["usage"].get("input_tokens")
            output_tokens = payload["usage"].get("output_tokens")
            resolved = payload.get("model") if isinstance(payload.get("model"), str) else None
    if not texts:
        raise ConversationError("Codex returned no orchestrator message")
    return "\n".join(texts), resolved, input_tokens, output_tokens


def _local_reply(endpoint: str, model: str, prompt: str, timeout: int) -> ConversationReply:
    try:
        endpoint = validate_loopback_endpoint(endpoint)
    except ConnectionError as error:
        raise ConversationError(str(error)) from error
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with open_without_proxy(request, timeout=timeout) as response:
            result = json.loads(response.read(8 << 20).decode("utf-8"))
        text = result["choices"][0]["message"]["content"]
        resolved = result.get("model")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        if not isinstance(text, str) or not text.strip():
            raise ConversationError("local model returned no orchestrator message")
        return ConversationReply(
            Redactor().text(text), "local", model,
            resolved if isinstance(resolved, str) else None,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise ConversationError("local model request failed without a usable response") from error


def _stream_cli(
    selection: ProviderSelection,
    argv: Sequence[str],
    prompt: str,
    workspace: Path,
    timeout: int,
    on_chunk: Callable[[str], None],
    cancel_event: Optional[Event] = None,
) -> ConversationReply:
    try:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(Path(workspace).resolve()), env=_environment(), start_new_session=True,
        )
    except OSError as error:
        raise ConversationError("{} orchestrator could not start".format(selection.provider)) from error
    try:
        selector = selectors.DefaultSelector()
        pending_input = memoryview(prompt.encode("utf-8"))
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        line_buffer = bytearray()
        chunks: List[str] = []
        final_payload: Optional[Dict[str, Any]] = None
        import time
        deadline = time.monotonic() + timeout
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise ConversationCancelled("planning request cancelled")
            if time.monotonic() >= deadline:
                os.killpg(process.pid, signal.SIGKILL)
                raise ConversationError("{} orchestrator timed out".format(selection.provider))
            for key, _ in selector.select(timeout=0.1):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fileobj.fileno(), pending_input[:65536])
                        pending_input = pending_input[written:]
                    except BrokenPipeError:
                        pending_input = pending_input[:0]
                    if not pending_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                channel = key.data
                if len(buffers[channel]) < (16 << 20):
                    buffers[channel].extend(data[: (16 << 20) - len(buffers[channel])])
                if channel != "stdout":
                    continue
                line_buffer.extend(data)
                if len(line_buffer) > (16 << 20):
                    raise ConversationError("provider emitted an oversized streaming record")
                while b"\n" in line_buffer:
                    line, _, rest = line_buffer.partition(b"\n")
                    line_buffer = bytearray(rest)
                    try:
                        payload = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if selection.provider == "claude":
                        if payload.get("type") == "stream_event":
                            event = payload.get("event")
                            delta = event.get("delta") if isinstance(event, dict) else None
                            piece = delta.get("text") if isinstance(delta, dict) else None
                            if isinstance(piece, str) and piece:
                                chunks.append(piece)
                                on_chunk(piece)
                        if payload.get("type") == "result":
                            final_payload = payload
                    else:
                        item = payload.get("item")
                        if payload.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                            piece = item.get("text")
                            if isinstance(piece, str) and piece:
                                chunks.append(piece)
                                on_chunk(piece)
        return_code = process.wait(timeout=2)
        if return_code != 0:
            detail = Redactor().text(bytes(buffers["stderr"]).decode("utf-8", "replace"))[-500:].strip()
            raise ConversationError("{} orchestrator exited {}{}".format(
                selection.provider, return_code, ": " + detail if detail else ""
            ))
        if selection.provider == "claude":
            if final_payload is None:
                raise ConversationError("Claude returned no final result")
            text = final_payload.get("result")
            if not isinstance(text, str) or not text.strip():
                text = "".join(chunks)
            if not text.strip():
                raise ConversationError("Claude returned no orchestrator message")
            usage = final_payload.get("usage") if isinstance(final_payload.get("usage"), dict) else {}
            model_usage = final_payload.get("modelUsage") or final_payload.get("model_usage")
            resolved = next(iter(model_usage)) if isinstance(model_usage, dict) and len(model_usage) == 1 else final_payload.get("model")
            if not chunks:
                on_chunk(text)
            return ConversationReply(
                Redactor().text(text), "claude", selection.model,
                resolved if isinstance(resolved, str) else None,
                usage.get("input_tokens") if type(usage.get("input_tokens")) is int else None,
                usage.get("output_tokens") if type(usage.get("output_tokens")) is int else None,
            )
        text, resolved, input_tokens, output_tokens = _parse_cli_reply("codex", bytes(buffers["stdout"]))
        return ConversationReply(
            Redactor().text(text), "codex", selection.model, resolved,
            input_tokens if type(input_tokens) is int else None,
            output_tokens if type(output_tokens) is int else None,
        )
    finally:
        if "selector" in locals():
            selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for handle in (process.stdin, process.stdout, process.stderr):
            if handle is not None:
                handle.close()


def converse(
    selection_text: str,
    message: str,
    history: Sequence[Mapping[str, str]],
    *,
    effort: str,
    workspace: Path,
    local_endpoint: str = "http://127.0.0.1:11434/v1",
    timeout: int = 120,
    runner: Any = subprocess.run,
    on_chunk: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Event] = None,
) -> ConversationReply:
    if cancel_event is not None and cancel_event.is_set():
        raise ConversationCancelled("planning request cancelled")
    selection = parse_selection(selection_text)
    if selection.provider == "manual":
        raise ConversationError("manual mode has no model call; use /grill GOAL to build a deterministic plan")
    prompt = _prompt(message, history)
    if selection.provider == "local":
        if cancel_event is not None:
            result_queue = queue.Queue(maxsize=1)
            def request_local() -> None:
                try:
                    result_queue.put((True, _local_reply(local_endpoint, selection.model or "", prompt, timeout)))
                except Exception as error:
                    result_queue.put((False, error))
            threading.Thread(target=request_local, name="camol-local-planner", daemon=True).start()
            while True:
                if cancel_event.is_set():
                    raise ConversationCancelled("local planning wait cancelled; the endpoint may still finish its request, so usage is unknown")
                try:
                    succeeded, result = result_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if succeeded:
                    return result
                raise result
        return _local_reply(local_endpoint, selection.model or "", prompt, timeout)
    if selection.provider == "openai":
        raise ConversationError("OpenAI API execution is not enabled in Product V0; the key reference can be inspected with /connections")
    use_stream = runner is subprocess.run
    argv = provider_argv(selection, effort, workspace, stream=use_stream or on_chunk is not None)
    if use_stream:
        return _stream_cli(selection, argv, prompt, workspace, timeout, on_chunk or (lambda chunk: None), cancel_event)
    try:
        completed = runner(
            argv,
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path(workspace).resolve()),
            env=_environment(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConversationError("{} orchestrator request failed: {}".format(selection.provider, error.__class__.__name__)) from error
    if completed.returncode != 0:
        detail = Redactor().text(completed.stderr.decode("utf-8", "replace"))[-500:].strip()
        raise ConversationError("{} orchestrator exited {}{}".format(
            selection.provider, completed.returncode, ": " + detail if detail else ""
        ))
    text, resolved, input_tokens, output_tokens = _parse_cli_reply(selection.provider, completed.stdout)
    return ConversationReply(
        Redactor().text(text), selection.provider, selection.model,
        resolved, input_tokens if type(input_tokens) is int else None,
        output_tokens if type(output_tokens) is int else None,
    )
