"""Detachable single-writer supervisor and authenticated local control protocol."""

import asyncio
import fcntl
import hmac
import json
import os
import secrets
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .orchestrator import Orchestrator, StateTransitionError
from .runner import HarnessRunner, summary
from .runbook import load_runbook
from .sandbox import SandboxError, process_start_fingerprint, validate_process_invocation
from .schema import SchemaError, reject_unknown_fields
from .store import SQLiteEventStore
from .workspace import WorkspaceManager


class SupervisorError(RuntimeError):
    """Supervisor ownership or control protocol failed."""


@dataclass(frozen=True)
class SupervisorPaths:
    state_dir: Path
    control_dir: Path
    socket: Path
    token: Path
    lock: Path
    pid: Path
    log: Path
    database: Path

    @classmethod
    def under(cls, state_dir: Path, database: Optional[Path] = None) -> "SupervisorPaths":
        root = Path(state_dir).resolve()
        control = root / "control"
        return cls(
            state_dir=root,
            control_dir=control,
            socket=control / "camol.sock",
            token=control / "control.token",
            lock=control / "leader.lock",
            pid=control / "leader.pid",
            log=control / "supervisor.log",
            database=Path(database).resolve() if database else root / "camol.sqlite3",
        )


class LeaderLock:
    """Process-scoped non-blocking advisory leader lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            raise SupervisorError("another Camol supervisor owns this state directory") from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _control_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise SupervisorError("control token must be a regular file")
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise SupervisorError("control token permissions must be 0600")
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise SupervisorError("control token is malformed")
        return token
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return token


def _read_control_token(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise SupervisorError("a secure supervisor control token is not available")
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise SupervisorError("control token is malformed")
    return token


class Supervisor:
    """Own the event store and keep execution independent of a client terminal."""

    MAX_REQUEST_BYTES = 64 * 1024

    def __init__(
        self,
        runbook: Path,
        workspace: Path,
        state_dir: Path,
        *,
        database: Optional[Path] = None,
        approve_by: Optional[str] = None,
    ):
        self.runbook_path = Path(runbook).resolve()
        self.workspace = Path(workspace).resolve()
        self.paths = SupervisorPaths.under(state_dir, database)
        WorkspaceManager(self.workspace, self.paths.state_dir)
        try:
            self.paths.database.relative_to(self.paths.state_dir)
        except ValueError as error:
            raise SupervisorError("supervisor database must stay inside the state directory") from error
        if self.paths.control_dir.is_symlink():
            raise SupervisorError("supervisor control directory must not be a symlink")
        self.paths.control_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.control_dir, 0o700)
        if self.paths.control_dir.resolve().parent != self.paths.state_dir:
            raise SupervisorError("supervisor control directory escaped the state directory")
        for path in (self.paths.socket, self.paths.token, self.paths.lock, self.paths.pid, self.paths.log):
            if path.is_symlink():
                raise SupervisorError("supervisor control files must not be symlinks")
        self.approve_by = approve_by
        self.lock = LeaderLock(self.paths.lock)
        self.token = ""
        self.server = None
        self.store = None
        self.orchestrator = None
        self.runner = None
        self.run_id = None
        self.mode = "starting"
        self.draining = False
        self.stop_after_drain = False
        # These are created inside ``serve`` so Python 3.9 cannot bind them to
        # the caller's implicit loop before ``asyncio.run`` creates its loop.
        self._shutdown = None
        self._wake = None
        self._driver_task = None
        self._force_task = None
        self.orphans = []

    def _write_pid(self) -> None:
        descriptor = os.open(str(self.paths.pid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _boxes(self) -> Dict[str, Any]:
        records = self.paths.state_dir / "records" / "workspaces"
        boxes = []
        if records.is_dir() and not records.is_symlink():
            for path in sorted(records.glob("*.json")):
                if path.is_symlink():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    receipt = payload["receipt"]
                    boxes.append({
                        "workspace_id": receipt["workspace_id"],
                        "task_id": payload["task_id"],
                        "box_id": payload["box_id"],
                        "path": receipt["path"],
                        "branch": receipt["branch"],
                        "integration": payload["integration"],
                    })
                except (KeyError, OSError, json.JSONDecodeError):
                    boxes.append({"record": path.name, "status": "invalid"})
        return {"boxes": boxes}

    @staticmethod
    def _replace_json(path: Path, payload: Dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _scan_invocations(self) -> list:
        records = []
        packet_root = self.paths.state_dir / "packets"
        if not packet_root.is_dir() or packet_root.is_symlink():
            return records
        for path in sorted(packet_root.rglob("*.invocation.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = validate_process_invocation(json.loads(path.read_text(encoding="utf-8")))
                if payload.get("state") != "active":
                    continue
                pid = payload["pid"]
                pgid = payload["pgid"]
                if type(pid) is not int or type(pgid) is not int or pid <= 1 or pgid != pid:
                    raise SupervisorError("active invocation record has an unsafe process identity")
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
                if alive:
                    try:
                        if os.getpgid(pid) != pgid or process_start_fingerprint(pid) != payload["process_started"]:
                            raise SupervisorError("active invocation process identity no longer matches its record")
                    except SandboxError as error:
                        raise SupervisorError("active invocation process identity cannot be proven") from error
                    records.append({"path": str(path), **payload})
                else:
                    payload.update(state="orphan_dead", finished_at=None, exit_code=None)
                    self._replace_json(path, payload)
            except (KeyError, OSError, json.JSONDecodeError, SandboxError) as error:
                raise SupervisorError("invocation recovery record is malformed") from error
        return records

    def status(self) -> Dict[str, Any]:
        state = self.orchestrator.state(self.run_id)
        return {
            "schema": "camol.supervisor_status",
            "schema_version": 1,
            "pid": os.getpid(),
            "mode": self.mode,
            "draining": self.draining,
            "run": summary(state),
            "unknown_effects": sorted(
                effect_id for effect_id, effect in state.get("effects", {}).items()
                if effect["state"] == "EFFECT_UNKNOWN"
            ),
            "orphan_invocations": [
                {key: item[key] for key in ("pid", "pgid", "cwd", "argv_digest", "started_at")}
                for item in self.orphans
            ],
            **self._boxes(),
        }

    async def _driver(self) -> None:
        while not self._shutdown.is_set():
            state = self.orchestrator.state(self.run_id)
            if self.orphans:
                self.mode = "orphaned"
                self._wake.clear()
                await self._wake.wait()
                continue
            if state["status"] in {"completed", "blocked"}:
                self.mode = "terminal"
                self._shutdown.set()
                return
            if self.draining:
                self.mode = "drained"
                if self.stop_after_drain:
                    self._shutdown.set()
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            if state["status"] == "draft":
                self.mode = "awaiting_approval"
                self._wake.clear()
                await self._wake.wait()
                continue
            self.mode = "running"
            before = state["last_seq"]
            state = await self.runner.run_until_terminal(
                self.run_id, should_drain=lambda: self.draining
            )
            if state["status"] in {"completed", "blocked"}:
                continue
            if self.draining:
                continue
            self.mode = "waiting"
            self._wake.clear()
            # A waiting run requires an external-state change or operator retry;
            # do not busy-loop probes against an unchanged world.
            if state["last_seq"] == before or any(task["status"] == "waiting" for task in state["tasks"].values()):
                await self._wake.wait()

    async def _force_shutdown(self, requested_by: str) -> None:
        if self._driver_task is not None:
            self._driver_task.cancel()
            try:
                await self._driver_task
            except asyncio.CancelledError:
                pass
        await self._terminate_orphans()
        if self.runner is not None and self.run_id is not None:
            self.runner.force_interrupt(self.run_id, requested_by=requested_by)
        self.mode = "stopped"
        self._shutdown.set()

    async def _terminate_orphans(self) -> None:
        for item in self.orphans:
            pid = item["pid"]
            pgid = item["pgid"]
            try:
                if (
                    os.getpgid(pid) != pgid
                    or process_start_fingerprint(pid) != item["process_started"]
                ):
                    raise SupervisorError("orphan process group identity changed; refusing to signal it")
                os.killpg(pgid, signal.SIGTERM)
                for _ in range(50):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.1)
                else:
                    os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            path = Path(item["path"])
            payload = {key: value for key, value in item.items() if key != "path"}
            payload.update(state="terminated_by_supervisor", finished_at=None, exit_code=None)
            self._replace_json(path, payload)
        self.orphans = []

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        fields = ("schema", "schema_version", "token", "command", "requested_by")
        reject_unknown_fields(request, fields, "control request")
        if (
            request.get("schema") != "camol.control_request"
            or type(request.get("schema_version")) is not int
            or request.get("schema_version") != 1
        ):
            raise SupervisorError("unsupported control request")
        if not isinstance(request.get("token"), str) or not hmac.compare_digest(request["token"], self.token):
            raise SupervisorError("control authentication failed")
        command = request.get("command")
        if not isinstance(command, str):
            raise SupervisorError("control command must be a string")
        if command == "ping":
            return {"ok": True, "pid": os.getpid()}
        if command in {"status", "boxes"}:
            return {"ok": True, "result": self.status() if command == "status" else self._boxes()}
        if command == "drain":
            self.draining = True
            self.mode = "draining"
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "resume":
            if self.orphans:
                raise SupervisorError("live orphan invocation requires explicit force-stop or operator reconciliation")
            self.draining = False
            self.stop_after_drain = False
            self.mode = "running"
            self._wake.set()
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "approve":
            state = self.orchestrator.state(self.run_id)
            if state["status"] != "draft":
                raise SupervisorError("run is not awaiting plan approval")
            approved_by = str(request.get("requested_by") or "")
            if not approved_by:
                raise SupervisorError("approval requires requested_by")
            self.orchestrator.approve_plan(self.run_id, approved_by, state["plan_digest"])
            self._wake.set()
            return {"ok": True, "result": {"plan_digest": state["plan_digest"], "approved_by": approved_by}}
        if command == "stop":
            self.draining = True
            self.stop_after_drain = True
            self.mode = "draining"
            self._wake.set()
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "force-stop":
            if self._force_task is None:
                self._force_task = asyncio.create_task(self._force_shutdown(str(request.get("requested_by") or "operator")))
            return {"ok": True, "result": {"mode": "stopping"}}
        raise SupervisorError("unknown control command")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw or len(raw) > self.MAX_REQUEST_BYTES:
                raise SupervisorError("control request is empty or too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise SupervisorError("control request must be an object")
            response = await self._dispatch(request)
        except (json.JSONDecodeError, SupervisorError, StateTransitionError, SchemaError, OSError) as error:
            response = {"ok": False, "error": str(error)}
        writer.write((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self) -> None:
        self._shutdown = asyncio.Event()
        self._wake = asyncio.Event()
        self.lock.acquire()
        try:
            self.token = _control_token(self.paths.token)
            if self.paths.socket.exists() or self.paths.socket.is_symlink():
                self.paths.socket.unlink()
            self.store = SQLiteEventStore(self.paths.database)
            self.orchestrator = Orchestrator(self.store)
            runbook = load_runbook(self.runbook_path)
            state = self.orchestrator.initialize(runbook)
            self.run_id = state["run_id"]
            if state["status"] == "draft" and self.approve_by:
                self.orchestrator.approve_plan(self.run_id, self.approve_by, state["plan_digest"])
                state = self.orchestrator.state(self.run_id)
            if state["status"] == "running":
                self.orchestrator.mark_interrupted_effects_unknown(self.run_id)
            self.runner = HarnessRunner(self.orchestrator, self.workspace, state_dir=self.paths.state_dir)
            self.orphans = self._scan_invocations()
            self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.paths.socket))
            os.chmod(self.paths.socket, 0o600)
            self._write_pid()
            self._driver_task = asyncio.create_task(self._driver())
            loop = asyncio.get_running_loop()
            for caught in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(caught, lambda: self._schedule_stop())
                except NotImplementedError:
                    pass
            async with self.server:
                await self._shutdown.wait()
        finally:
            if self._driver_task is not None and not self._driver_task.done():
                self._driver_task.cancel()
                try:
                    await self._driver_task
                except asyncio.CancelledError:
                    pass
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            if self.store is not None:
                self.store.close()
            for path in (self.paths.socket, self.paths.pid):
                if path.exists() or path.is_symlink():
                    path.unlink()
            self.lock.release()

    def _schedule_stop(self) -> None:
        self.draining = True
        self.stop_after_drain = True
        self._wake.set()


async def send_control(state_dir: Path, command: str, *, requested_by: str = "operator") -> Dict[str, Any]:
    paths = SupervisorPaths.under(state_dir)
    token = _read_control_token(paths.token)
    if not paths.socket.exists() or paths.socket.is_symlink():
        raise SupervisorError("Camol supervisor socket is not available")
    reader, writer = await asyncio.open_unix_connection(str(paths.socket))
    request = {
        "schema": "camol.control_request",
        "schema_version": 1,
        "token": token,
        "command": command,
        "requested_by": requested_by,
    }
    writer.write((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
    await writer.drain()
    response = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
    writer.close()
    await writer.wait_closed()
    if not response.get("ok"):
        raise SupervisorError(response.get("error") or "control request failed")
    return response
