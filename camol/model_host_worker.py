"""Private helper for one approved load; not a generic command runner."""

import fcntl
import http.client
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from .model_host import LlamaCppModelHost, ModelError, _file_digest, _private
from .schema import canonical_digest


def _stop(child, timeout):
    """Only signal the unreaped process created by this helper, never a saved PID."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=timeout)
    return child.returncode


def run_owned(host, digest, operation_id):
    row, plan = host._plan(digest)
    operation = host._operation(digest)
    if operation is None or operation["operation_id"] != operation_id or row["approved_by"] != plan.owner:
        raise ModelError("helper requires an exact approved durable load intent")
    directory = host._directory(digest)
    lock_path = directory / "helper.lock"
    _private(lock_path)
    descriptor = os.open(str(lock_path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    child, stopping, spawn_attempted = None, [False], False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelError("another helper already owns this exact load") from None
        # Recheck after acquiring ownership. A second process can never revive
        # an old intent, even after the prior helper exits.
        operation = host._operation(digest)
        if operation["state"] != "starting":
            raise ModelError("model host load intent has already been claimed")
        host._update(digest, "loading", {"helper_pid": os.getpid(), "claimed_once": True})
        signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
        signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
        # TTL begins at the durable intent, including input verification/startup.
        elapsed = max(0, time.time() - operation["created_at"])
        lifetime_deadline = time.monotonic() + max(0, plan.lifetime_seconds - elapsed)
        load_deadline = time.monotonic() + max(0, plan.load_timeout_seconds - elapsed)
        artifact = host._verify_inputs(plan)
        alias = "camol-" + secrets.token_hex(24)
        argv = [plan.executable, "--model", artifact["path"], "--alias", alias,
                "--host", "127.0.0.1", "--port", str(plan.port), "--api-key-file", str(directory / "api.key"),
                "--ctx-size", str(plan.context_tokens), "--threads", str(plan.threads),
                "--gpu-layers", str(plan.gpu_layers), "--parallel", "1", "--offline",
                "--no-webui", "--no-agent", "--no-jinja", "--no-warmup", "--no-mmproj",
                "--no-models-autoload", "--sleep-idle-seconds", "-1", "--fit", "off", "--log-disable"]
        if plan.gpu_layers == 0:
            argv += ["--device", "none", "--no-op-offload"]
        host._update(digest, detail={"artifact_verified": True, "artifact_digest": artifact["digest"],
                                    "executable_digest": plan.executable_digest, "model_path": artifact["path"],
                                    "alias": alias, "argv_digest": canonical_digest(argv)})
        reason = host._operation(digest)["detail"].get("stop_requested")
        if reason or stopping[0] or time.monotonic() >= min(lifetime_deadline, load_deadline):
            host._update(digest, "expired" if time.monotonic() >= lifetime_deadline else "cancelled", {"child_started": False})
            return
        # Popen is the only allocation point; durable claimed state precedes it.
        spawn_attempted = True
        child = subprocess.Popen(argv, cwd=str(directory), env={"PATH": "/usr/bin:/bin", "HOME": str(directory),
                                 "TMPDIR": str(directory), "LANG": "C"}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                 start_new_session=False)
        host._update(digest, detail={"child_pid": child.pid, "child_started": True})
        # Observed file hashes are not a loaded-weight attestation. Recheck after
        # spawn to detect substitutions in the normal trusted-owner threat model.
        _file_digest(Path(plan.executable), plan.executable_digest, executable=True)
        _file_digest(Path(artifact["path"]), plan.artifact_digest)
        loaded, last_readback = False, 0.0
        while True:
            operation = host._operation(digest)
            reason = operation["detail"].get("stop_requested")
            if operation["state"] not in {"loading", "loaded", "stopping"}:
                reason = "load_authority_state_changed"
            if stopping[0]:
                reason = "helper_signal"
            if child.poll() is not None:
                host._update(digest, "failed", {"error": "child_exited", "exit_code": child.returncode})
                return
            if time.monotonic() >= lifetime_deadline:
                reason = "lifetime_expired"
            elif not loaded and time.monotonic() >= load_deadline:
                reason = "load_deadline"
            if reason:
                host._update(digest, "stopping", {"stop_reason": reason})
                code = _stop(child, plan.stop_timeout_seconds)
                outcome = "unloaded" if reason == "owner_unload" else "expired" if reason == "lifetime_expired" else "cancelled"
                host._update(digest, outcome, {"exit_code": code, "owned_child_reaped": True})
                return
            if not loaded or time.monotonic() - last_readback >= 2:
                try:
                    readback = host._readback(plan, operation)
                    if child.poll() is not None:
                        raise ModelError("child exited during readback")
                except (ModelError, OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
                    if loaded:
                        # Losing identity/residency readback drains the owned
                        # process; it does not claim the previous green persisted.
                        host._update(digest, "stopping", {"error": "runtime_readback_lost"})
                        code = _stop(child, plan.stop_timeout_seconds)
                        host._update(digest, "failed", {"exit_code": code, "owned_child_reaped": True})
                        return
                else:
                    host._update(digest, "loaded" if not loaded else None, {"readback": readback})
                    loaded = True
                last_readback = time.monotonic()
            host._update(digest)
            time.sleep(0.1)
    except BaseException as error:
        # Only this still-owning helper can safely settle process termination.
        # Raw exceptions/child output/API keys never enter the receipt.
        detail = {"error": type(error).__name__}
        state = "failed"
        if spawn_attempted and child is None:
            state, detail["error"] = "unknown", "child_spawn_uncertain"
        if child is not None:
            try:
                detail.update(exit_code=_stop(child, plan.stop_timeout_seconds), owned_child_reaped=True)
            except BaseException:
                state, detail["error"] = "unknown", "owned_child_stop_uncertain"
        # A competing or stale helper must not rewrite a different owner's state.
        current = host._operation(digest)
        if current and current["detail"].get("helper_pid") == os.getpid():
            host._update(digest, state, detail)
    finally:
        os.close(descriptor)


def main():
    if len(sys.argv) != 4:
        raise SystemExit(2)
    root, digest, operation_id = sys.argv[1:]
    try:
        with LlamaCppModelHost(root) as host:
            run_owned(host, digest, operation_id)
    except (ModelError, OSError, ValueError):
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
