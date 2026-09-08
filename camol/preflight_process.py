"""Bounded subprocess capture for explicit no-tools capability probes."""

import os
import selectors
import signal
import subprocess
import time


def bounded_preflight_run(argv, *, cwd, env, input=None, timeout=120, stdout=None, stderr=None, check=False):
    if input is not None and (not isinstance(input, bytes) or len(input) > 4096):
        raise ValueError("preflight input exceeds the fixed request ceiling")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    finished = False
    try:
        with selectors.DefaultSelector() as selector:
            for name in output:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            pending = memoryview(input or b"")
            if input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(min(0.1, remaining)):
                    if key.data == "stdin":
                        try:
                            pending = pending[os.write(key.fileobj.fileno(), pending):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(data)
                        if sum(map(len, output.values())) > 1 << 20:
                            raise OSError("preflight process exceeded its output ceiling")
        result = subprocess.CompletedProcess(argv, process.wait(timeout=max(.001, deadline - time.monotonic())),
                                             bytes(output["stdout"]), bytes(output["stderr"]))
        finished = True
        if check:
            result.check_returncode()
        return result
    finally:
        if not finished:
            # Include descendants retaining pipes even after the leader exits.
            # This is owned process-group cleanup, not a hostile-setsid sandbox.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
