"""Optional SSH client for an already-running, policy-bound Camol supervisor."""

import asyncio
import fcntl
import json
import math
import os
import shutil
import signal
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schema import canonical_digest
from .probes import Redactor
from .sandbox import SandboxError, process_start_fingerprint
from .ssh_protocol import (MUTATING_COMMANDS, REMOTE_COMMAND, REQUEST_LIMIT, RESPONSE_LIMIT, SSHTarget,
    SSHTransportError, encode_frame, fields, read_frame, read_regular, sha256, strict_json, text, validate_identity)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SSHRequestCancelled(asyncio.CancelledError):
    def __init__(self, request_id, outcome):
        super().__init__("SSH request cancelled; retained outcome=" + outcome)
        self.request_id, self.outcome = request_id, outcome


def cancellation_receipt(error):
    """Python 3.9 wraps task cancellation; preserve its receipt via context."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, SSHRequestCancelled):
            return {"request_id": error.request_id, "outcome": error.outcome}
        error = error.__context__
    return None


def _save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


class SSHControlClient:
    def __init__(self, target, *, state_dir, ssh_binary="/usr/bin/ssh", timeout=45, read_only=False):
        self.target = target if isinstance(target, SSHTarget) else SSHTarget.from_dict(target)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 120:
            raise SSHTransportError("POLICY_DENIED", "SSH request timeout must be within 0.1..120 seconds")
        self.timeout = timeout
        if type(read_only) is not bool:
            raise SSHTransportError("POLICY_DENIED", "read_only must be boolean")
        self.read_only = read_only
        self.ssh_binary = Path(ssh_binary)
        if not self.ssh_binary.is_absolute():
            raise SSHTransportError("POLICY_DENIED", "SSH executable must be an explicit absolute path")
        identity = {key: getattr(self.target, key) for key in ("host", "port", "login", "target_id")}
        self.root = Path(state_dir).resolve() / canonical_digest(identity).split(":")[1]
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.exists() or self.root.is_symlink():
            metadata = self.root.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise SSHTransportError("POLICY_DENIED", "transport receipt directory must be owner-only")

    @contextmanager
    def _lock(self):
        if self.read_only:
            raise SSHTransportError("POLICY_DENIED", "receipt inspection cannot mutate transport state")
        descriptor = os.open(str(self.root / "owner.lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise SSHTransportError("OWNER_BUSY", "another transport mutation or reconciliation owns this target") from error
            yield
        finally:
            os.close(descriptor)

    def receipts(self):
        paths = sorted(self.root.glob("ssh-*.json"))
        if len(paths) > 10000:
            raise SSHTransportError("OPERATOR_ATTENTION", "transport receipt retention requires explicit owner archival")
        records = []
        for path in paths:
            record = strict_json(read_regular(path, maximum=REQUEST_LIMIT, private=True))
            if (record.get("schema") != "camol.ssh_dispatch_receipt" or not isinstance(record.get("request_id"), str)
                    or record["request_id"] + ".json" != path.name
                    or record.get("status") not in {"prepared", "dispatched", "completed", "rejected", "unknown", "acknowledged_unknown"}):
                raise SSHTransportError("OPERATOR_ATTENTION", "transport receipt is invalid")
            records.append(record)
        return records

    def acknowledge_unknown(self, request_id, *, requested_by, note):
        """Owner acknowledgment is not proof of success and never retries a call."""
        text(note, "reconciliation note", 4096)
        if Redactor().text(note) != note:
            raise SSHTransportError("POLICY_DENIED", "acknowledgment note contains credential-shaped or protected values")
        with self._lock():
            record = next((item for item in self.receipts() if item["request_id"] == request_id), None)
            if (record is None or record["status"] not in {"dispatched", "unknown"}
                    or requested_by != self.target.owner or requested_by != record["requested_by"]):
                raise SSHTransportError("POLICY_DENIED", "only the frozen owner may acknowledge this unresolved request")
            record.update(status="acknowledged_unknown", reconciled_at=_now(), reconciliation_note=note,
                          reconciliation="owner acknowledged uncertainty; execution success is not established")
            record.setdefault("history", []).append({"status": "acknowledged_unknown", "at": record["reconciled_at"]})
            _save(self.root / (request_id + ".json"), record)
            return record

    def ssh_argv(self, known_hosts_snapshot):
        target = self.target
        options = {
            "BatchMode": "yes", "StrictHostKeyChecking": "yes", "UserKnownHostsFile": str(known_hosts_snapshot),
            "GlobalKnownHostsFile": os.devnull, "UpdateHostKeys": "no", "VerifyHostKeyDNS": "no",
            "IdentitiesOnly": "yes", "IdentityAgent": "none", "AddKeysToAgent": "no", "CertificateFile": "none",
            "PreferredAuthentications": "publickey", "PasswordAuthentication": "no", "KbdInteractiveAuthentication": "no",
            "HostbasedAuthentication": "no", "GSSAPIAuthentication": "no", "GSSAPIDelegateCredentials": "no",
            "ForwardAgent": "no", "ForwardX11": "no", "ForwardX11Trusted": "no", "ClearAllForwardings": "yes",
            "PermitLocalCommand": "no", "ProxyCommand": "none", "ProxyJump": "none",
            "ControlMaster": "no", "ControlPath": "none", "ControlPersist": "no", "ConnectionAttempts": "1",
            "ConnectTimeout": str(max(1, min(10, math.ceil(self.timeout)))), "ServerAliveInterval": "5",
            "ServerAliveCountMax": "1", "EscapeChar": "none", "LogLevel": "ERROR",
        }
        argv = [str(self.ssh_binary), "-F", os.devnull, "-T", "-a", "-x"]
        for name, value in options.items():
            argv.extend(("-o", name + "=" + value))
        return argv + ["-i", target.identity_file, "-p", str(target.port), "-l", target.login, "--", target.host, REMOTE_COMMAND]

    async def _drain_stderr(self, stream):
        # Deliberately retain only a digest/count. SSH stderr can expose key
        # paths or server banners; no raw credentials or transcripts are logged.
        import hashlib
        hasher, total = hashlib.sha256(), 0
        while True:
            part = await stream.read(65536)
            if not part:
                return {"sha256": "sha256:" + hasher.hexdigest(), "bytes": total}
            hasher.update(part)
            total += len(part)

    async def _stop_process(self, process, started):
        cleanup = {"scope": "local_ssh_process_only", "group_signal": "not_proven", "pipes_detached": False}
        if process.returncode is None and started is not None:
            try:
                if process_start_fingerprint(process.pid) == started and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                    cleanup["group_signal"] = "sent_to_current_owned_group"
            except (OSError, ValueError, SandboxError):
                pass
        # A reaped leader does not prove ownership of a surviving process group.
        # Detach local pipes instead of signalling a possibly reused PGID. This
        # private asyncio transport seam is tested on supported Python versions.
        if process.returncode is not None:
            for descriptor in (0, 1, 2):
                transport = process._transport.get_pipe_transport(descriptor)
                if transport is not None:
                    transport.close()
            cleanup["pipes_detached"] = True
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            for descriptor in (0, 1, 2):
                transport = process._transport.get_pipe_transport(descriptor)
                if transport is not None:
                    transport.close()
            cleanup["pipes_detached"] = True
        if process.stdin is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=0.2)
            except (OSError, asyncio.TimeoutError):
                pass
        cleanup["leader_reaped"] = process.returncode is not None
        return cleanup

    async def request(self, command, *, params=None, requested_by=None):
        target = self.target
        requested_by = target.owner if requested_by is None else requested_by
        if not isinstance(command, str) or command not in target.allowed_commands or requested_by != target.owner or (params is not None and not isinstance(params, dict)):
            raise SSHTransportError("POLICY_DENIED", "target profile does not authorize this command or actor")
        # Freeze nested caller data before the first await. This exact snapshot
        # supplies both the receipt digest and the transmitted control frame.
        frozen_params = strict_json(encode_frame({"params": params or {}})[4:])["params"]
        mutating = command in MUTATING_COMMANDS
        if mutating:
            with self._lock():
                if any(item["status"] in {"dispatched", "unknown"} for item in self.receipts()):
                    raise SSHTransportError("OUTCOME_UNKNOWN", "reconcile the previous mutation before dispatching another", outcome="unknown")
                return await self._request(command, frozen_params, requested_by, mutating=True)
        return await self._request(command, frozen_params, requested_by, mutating=False)

    async def _request(self, command, params, requested_by, *, mutating):
        target = self.target
        request_id = "ssh-" + uuid4().hex
        record = dict(schema="camol.ssh_dispatch_receipt", schema_version=1, request_id=request_id,
                      target_id=target.target_id, target_digest=target.digest(), run_id=target.run_id,
                      plan_digest=target.plan_digest, command=command, requested_by=requested_by,
                      request_digest=canonical_digest({"command": command, "params": params}), created_at=_now(), status="prepared")
        record["history"] = [{"status": "prepared", "at": record["created_at"]}]
        path = self.root / (request_id + ".json")
        if mutating:
            _save(path, record)
        process, stderr_task, snapshot, process_started = None, None, None, None
        dispatched = False
        try:
            known_hosts = read_regular(target.known_hosts, maximum=1 << 20)
            if sha256(known_hosts) != target.known_hosts_sha256:
                raise SSHTransportError("HOST_IDENTITY_DENIED", "known_hosts bytes differ from the approved snapshot")
            # Stat the reference only. Private-key bytes are read exclusively by
            # the explicit OpenSSH executable, never by Camol.
            key = Path(target.identity_file).lstat()
            if not stat.S_ISREG(key.st_mode) or key.st_uid != os.getuid() or key.st_mode & 0o077:
                raise SSHTransportError("AUTH_REQUIRED", "SSH identity reference must be an owner-only regular file")
            snapshot = Path(tempfile.mkdtemp(prefix="camol-ssh-", dir="/tmp"))
            pinned = snapshot / "known_hosts"
            with pinned.open("xb") as stream:
                stream.write(known_hosts)
                stream.flush()
                os.fsync(stream.fileno())
            pinned.chmod(0o400)
            argv = self.ssh_argv(pinned)
            environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}

            async def exchange():
                nonlocal process, stderr_task, dispatched, process_started
                process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True,
                    limit=RESPONSE_LIMIT)
                try:
                    process_started = process_start_fingerprint(process.pid)
                except (OSError, ValueError, SandboxError):
                    process_started = None
                stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
                hello = await read_frame(process.stdout)
                fields(hello, {"schema", "schema_version", "nonce", "identity"})
                if hello["schema"] != "camol.ssh_hello" or type(hello["schema_version"]) is not int or hello["schema_version"] != 1:
                    raise SSHTransportError("IDENTITY_DENIED", "unsupported remote bridge handshake")
                if validate_identity(hello["identity"]) != target.bridge_identity:
                    raise SSHTransportError("IDENTITY_DENIED", "remote bridge version or installed bytes do not match the approved identity")
                text(hello["nonce"], "bridge nonce", 128)
                request = dict(schema="camol.ssh_request", schema_version=1, nonce=hello["nonce"], request_id=request_id,
                               target_id=target.target_id, target_digest=target.target_digest, run_id=target.run_id,
                               plan_digest=target.plan_digest, command=command, requested_by=requested_by, params=params)
                frame = encode_frame(request)
                if mutating:
                    record.update(status="dispatched", dispatched_at=_now())
                    record["history"].append({"status": "dispatched", "at": record["dispatched_at"]})
                    _save(path, record)
                dispatched = True
                process.stdin.write(frame)
                await process.stdin.drain()
                process.stdin.close()
                response = await read_frame(process.stdout)
                if response.get("schema") == "camol.ssh_error":
                    fields(response, {"schema", "schema_version", "request_id", "target_id", "code", "outcome"})
                    if response["request_id"] != request_id or response["target_id"] != target.target_id:
                        raise SSHTransportError("PROTOCOL_DENIED", "remote error does not bind this request")
                    outcome = "not_dispatched" if response["outcome"] == "not_dispatched" else "unknown"
                    raise SSHTransportError("REMOTE_REJECTED", "remote bridge could not complete the bound control", outcome=outcome)
                fields(response, {"schema", "schema_version", "request_id", "target_id", "target_digest", "run_id", "plan_digest", "outcome", "response"})
                if (response["schema"] != "camol.ssh_response" or type(response["schema_version"]) is not int or response["schema_version"] != 1
                        or any(response[field] != request[field] for field in ("request_id", "target_id", "target_digest", "run_id", "plan_digest"))
                        or response["outcome"] not in {"completed", "rejected"} or not isinstance(response["response"], dict)
                        or type(response["response"].get("ok")) is not bool
                        or response["response"]["ok"] != (response["outcome"] == "completed")):
                    raise SSHTransportError("PROTOCOL_DENIED", "remote response does not bind the exact dispatched request")
                if await process.stdout.read(1):
                    raise SSHTransportError("PROTOCOL_DENIED", "unexpected trailing transport output")
                await process.wait()
                if process.returncode != 0:
                    raise SSHTransportError("TRANSPORT_UNAVAILABLE", "SSH exited without clean control completion")
                stderr = await stderr_task
                record.update(status=response["outcome"], finished_at=_now(), response_digest=canonical_digest(response), stderr=stderr)
                record["history"].append({"status": record["status"], "at": record["finished_at"]})
                if mutating:
                    _save(path, record)
                return {**response["response"], "request_id": request_id, "transport": record}
            return await asyncio.wait_for(exchange(), timeout=self.timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError, SSHTransportError) as error:
            outcome = "unknown" if dispatched else "not_dispatched"
            if isinstance(error, SSHTransportError) and error.code == "REMOTE_REJECTED" and error.outcome == "not_dispatched":
                outcome = "not_dispatched"
            record.update(status="unknown" if outcome == "unknown" else "rejected", finished_at=_now(),
                          error_code=getattr(error, "code", "TRANSPORT_UNAVAILABLE"))
            record["history"].append({"status": record["status"], "at": record["finished_at"]})
            if mutating:
                _save(path, record)
            if isinstance(error, asyncio.CancelledError):
                raise SSHRequestCancelled(request_id, outcome) from None
            raise SSHTransportError(getattr(error, "code", "TRANSPORT_UNAVAILABLE"),
                "SSH control outcome is unknown; inspect and reconcile before another mutation" if outcome == "unknown" else str(error) or "SSH control was not dispatched",
                request_id=request_id, outcome=outcome) from error
        finally:
            if process is not None:
                record["cleanup"] = await asyncio.shield(self._stop_process(process, process_started))
                if mutating:
                    _save(path, record)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if snapshot is not None:
                # Only the exact private temporary directory created above.
                shutil.rmtree(snapshot)
