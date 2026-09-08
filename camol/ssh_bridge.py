"""Remote stdio bridge to an existing owner-authorized Camol supervisor.

No daemon creation, credential forwarding, arbitrary paths, or remote worker
admission. Invoke via the fixed installed ``camol-ssh-bridge`` command.
"""

import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

from ._version import __version__
from .schema import canonical_digest
from .probes import Redactor
from .supervisor import SupervisorPaths
from .ssh_protocol import (REQUEST_LIMIT, RESPONSE_LIMIT, ALL_COMMANDS, SSHTransportError,
    encode_frame, fields, load_bridge_policy, read_frame, read_regular, sha256, strict_json, text)


def bridge_identity():
    """Authenticated host self-report, NOT remote execution attestation."""
    root = Path(__file__).resolve().parent
    inventory, size = {}, 0
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in {".py", ".json", ".txt", ".tcss"}:
            continue
        data = read_regular(path, maximum=16 << 20, owner=False)
        size += len(data)
        if size > 64 << 20 or len(inventory) > 10000:
            raise SSHTransportError("IDENTITY_DENIED", "installed bridge package exceeds the identity bound")
        inventory[str(path.relative_to(root))] = sha256(data)
    executable = Path(sys.executable).resolve()
    return dict(camol_version=__version__, python_executable=str(executable),
                python_sha256=sha256(read_regular(executable, maximum=256 << 20, owner=False)),
                package_sha256=canonical_digest(inventory), control_version=3)


def _authorize(policy_path, request):
    fields(request, {"schema", "schema_version", "nonce", "request_id", "target_id", "target_digest",
                     "run_id", "plan_digest", "command", "requested_by", "params"})
    if request["schema"] != "camol.ssh_request" or type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise SSHTransportError("PROTOCOL_DENIED", "unsupported SSH request")
    text(request["request_id"], "request ID", 128)
    for name in ("nonce", "target_id", "target_digest", "run_id", "plan_digest", "command", "requested_by"):
        text(request[name], name)
    if not isinstance(request["params"], dict) or request["command"] not in ALL_COMMANDS:
        raise SSHTransportError("PROTOCOL_DENIED", "invalid bound control request")
    policy = load_bridge_policy(policy_path)
    target = policy["targets"].get(request["target_id"])
    if (target is None or canonical_digest(target) != request["target_digest"]
            or target["run_id"] != request["run_id"] or target["plan_digest"] != request["plan_digest"]
            or target["owner"] != request["requested_by"] or request["command"] not in target["allowed_commands"]):
        raise SSHTransportError("POLICY_DENIED", "remote owner policy does not authorize this exact target and command")
    if "approved_by" in request["params"] and request["params"]["approved_by"] != target["owner"]:
        raise SSHTransportError("POLICY_DENIED", "control approval actor differs from the remote owner")
    return target


async def forward_control(policy_path, request):
    target = _authorize(policy_path, request)
    state = Path(target["state_dir"])
    if state.resolve() != state or not state.is_dir():
        raise SSHTransportError("TARGET_UNAVAILABLE", "the existing remote state directory is unavailable")
    paths = SupervisorPaths.under(state)
    try:
        metadata = paths.socket.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise OSError("unsafe socket")
    except OSError as error:
        raise SSHTransportError("TARGET_UNAVAILABLE", "the existing supervisor socket is unavailable") from error
    token = read_regular(paths.token, maximum=4096, private=True).decode("ascii").strip()
    if len(token) < 32:
        raise SSHTransportError("TARGET_UNAVAILABLE", "the existing control credential is malformed")
    reader, writer = await asyncio.open_unix_connection(str(paths.socket), limit=RESPONSE_LIMIT)
    dispatched = False
    try:
        # Re-read authoritative policy after connect's await. The supervisor
        # independently performs atomic run/plan binding at V3 dispatch.
        _authorize(policy_path, request)
        payload = dict(schema="camol.control_request", schema_version=3, token=token,
                       request_id=request["request_id"], command=request["command"], requested_by=target["owner"],
                       params=request["params"], expected_run_id=target["run_id"], expected_plan_digest=target["plan_digest"])
        raw = json.dumps(payload, sort_keys=True, allow_nan=False).encode() + b"\n"
        if len(raw) > 64 * 1024:
            raise SSHTransportError("PROTOCOL_DENIED", "local control request exceeds its byte limit")
        dispatched = True
        writer.write(raw)
        await writer.drain()
        response = strict_json(await reader.readline())
        if type(response.get("ok")) is not bool:
            raise SSHTransportError("PROTOCOL_DENIED", "invalid supervisor response")
        if not response["ok"]:
            # Preserve a safe generic refusal, not an arbitrary remote error
            # string that could contain control-token or filesystem material.
            return {"ok": False, "error": "remote supervisor rejected the bound control request"}
        if any(name in response for name in ("token", "credential", "control_token")):
            raise SSHTransportError("PROTOCOL_DENIED", "unexpected credential-bearing control response")
        return Redactor({"CONTROL_TOKEN": token}).value(response)
    except (asyncio.CancelledError, OSError, ValueError, SSHTransportError) as error:
        if dispatched:
            error.dispatch_started = True
        raise
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (OSError, asyncio.TimeoutError):
            pass


async def serve_streams(reader, writer, policy_path, *, timeout=45, identity=None):
    """One hello and one framed RPC; every request gets a fresh SSH process."""
    request_id, target_id, outcome = None, None, "not_dispatched"
    try:
        async def exchange():
            nonlocal request_id, target_id, outcome
            load_bridge_policy(policy_path)
            nonce = secrets.token_hex(32)
            hello = dict(schema="camol.ssh_hello", schema_version=1, nonce=nonce, identity=identity or bridge_identity())
            writer.write(encode_frame(hello, limit=RESPONSE_LIMIT))
            await writer.drain()
            request = await read_frame(reader, limit=REQUEST_LIMIT)
            if request.get("nonce") != nonce:
                raise SSHTransportError("PROTOCOL_DENIED", "SSH request does not bind the bridge nonce")
            request_id, target_id = request.get("request_id"), request.get("target_id")
            _authorize(policy_path, request)
            # Conservatively unknown once entering local forwarding: exceptions
            # and cancellation never claim that a mutation was rolled back.
            outcome = "unknown"
            response = await forward_control(policy_path, request)
            outcome = "completed" if response["ok"] else "rejected"
            reply = dict(schema="camol.ssh_response", schema_version=1, request_id=request_id, target_id=target_id,
                         target_digest=request["target_digest"], run_id=request["run_id"], plan_digest=request["plan_digest"],
                         outcome=outcome, response=response)
            writer.write(encode_frame(reply, limit=RESPONSE_LIMIT))
            await writer.drain()
        await asyncio.wait_for(exchange(), timeout=timeout)
    except (SSHTransportError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError) as error:
        reply = dict(schema="camol.ssh_error", schema_version=1, request_id=request_id, target_id=target_id,
                     code=getattr(error, "code", "TRANSPORT_UNAVAILABLE"), outcome=outcome)
        try:
            writer.write(encode_frame(reply, limit=RESPONSE_LIMIT))
            await asyncio.wait_for(writer.drain(), timeout=1)
        except (OSError, asyncio.TimeoutError):
            pass


async def _stdio(policy_path):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    try:
        await serve_streams(reader, writer, policy_path)
    finally:
        writer.close()


def main():
    if sys.argv[1:] == ["--identity"]:
        print(json.dumps(bridge_identity(), sort_keys=True))
        return 0
    if sys.argv[1:]:
        return 2
    try:
        asyncio.run(_stdio(Path.home() / ".config" / "camol" / "ssh-bridge.json"))
        return 0
    except (SSHTransportError, OSError, ValueError):
        # Never print policy values, filesystem paths, tokens or raw frames.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
