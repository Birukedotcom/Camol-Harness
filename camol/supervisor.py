"""Detachable single-writer supervisor and authenticated local control protocol."""

import asyncio
import atexit
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from .orchestrator import Orchestrator, StateTransitionError
from .runner import HarnessRunner, summary
from .runbook import load_runbook
from .sandbox import SandboxError, process_start_fingerprint, validate_process_invocation
from .schema import SchemaError, reject_unknown_fields, parse_timestamp, canonical_digest
from .store import SQLiteEventStore, ConcurrentAppendError
from .json_contracts import decode_contract
from .workspace import WorkspaceManager
from .watchers import Watcher, WatcherError, WatchSpec
from .watch_runtime import WatchRuntime
from .source_binding import SourceBindingError, make_binding, load_binding, persist_binding, assert_source
from .runbook import runbook_digest
from .mailbox import MailboxError
from .worker_delivery import DeliveryError
from .worker_gateway import WorkerGateway


class SupervisorError(RuntimeError):
    """Supervisor ownership or control protocol failed."""


# Keep intentional detached children referenced for the lifetime of this
# client process. Popen otherwise warns during immediate garbage collection.
_DETACHED_CHILDREN = []

# Event and box responses include bounded transcript evidence and can exceed
# asyncio's 64 KiB default reader limit. Keep a hard protocol ceiling so a
# local peer cannot force unbounded buffering.
CONTROL_RESPONSE_LIMIT = 8 * 1024 * 1024


def _reap_or_detach_children() -> None:
    """Reap finished children and silence Popen cleanup for live daemons."""
    for process in _DETACHED_CHILDREN:
        if process.poll() is None:
            # The supervisor owns a new session and is intentionally still
            # alive after this client exits. Prevent Popen.__del__ from
            # misreporting that expected state as a ResourceWarning.
            process.returncode = 0


atexit.register(_reap_or_detach_children)


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
        socket = control / "camol.sock"
        # Darwin and several BSDs cap AF_UNIX paths near 104 bytes. Product
        # sessions live under a descriptive private state path, so use a
        # private hashed runtime directory only when the direct path is unsafe.
        if len(os.fsencode(str(socket))) >= 100:
            digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
            runtime_root = Path("/tmp") if os.name == "posix" and Path("/tmp").is_dir() else Path(tempfile.gettempdir())
            socket = runtime_root / "camol-{}-{}".format(os.getuid(), digest) / "camol.sock"
        return cls(
            state_dir=root,
            control_dir=control,
            socket=socket,
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
        expected_source: Optional[Dict[str, Any]] = None,
        source_binding_digest: Optional[str] = None,
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
        if self.paths.socket.parent != self.paths.control_dir:
            runtime_dir = self.paths.socket.parent
            if runtime_dir.exists() and (runtime_dir.is_symlink() or not runtime_dir.is_dir()):
                raise SupervisorError("supervisor runtime directory is unsafe")
            runtime_dir.mkdir(parents=False, exist_ok=True, mode=0o700)
            metadata = runtime_dir.stat()
            if metadata.st_uid != os.getuid() or (metadata.st_mode & 0o777) != 0o700:
                raise SupervisorError("supervisor runtime directory must be owner-only")
        self.approve_by = approve_by
        self.expected_source = deepcopy(expected_source)
        self.source_binding_digest = source_binding_digest
        self.lock = LeaderLock(self.paths.lock)
        self.token = ""
        self.server = None
        self.store = None
        self.orchestrator = None
        self.runner = None
        self.run_id = None
        self.runbook = None
        self.mode = "starting"
        self.draining = False
        self.stop_after_drain = False
        # These are created inside ``serve`` so Python 3.9 cannot bind them to
        # the caller's implicit loop before ``asyncio.run`` creates its loop.
        self._shutdown = None
        self._wake = None
        self._driver_task = None
        self._watch_task = None
        self.watch_runtime = None
        self.watch_error = None
        self.worker_gateway = None
        self._worker_task = None
        self._force_task = None
        self.orphans = []
        self.last_error = None

    def _write_pid(self) -> None:
        descriptor = os.open(str(self.paths.pid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _boxes(self) -> Dict[str, Any]:
        state = self.orchestrator.state(self.run_id)
        boxes = []
        for bundle in state.get("admissions", {}).values():
            binding, receipt = bundle["binding"], bundle["workspace"]
            if binding["run_id"] == self.run_id and binding["plan_digest"] == state["plan_digest"]:
                boxes.append(dict(workspace_id=receipt["workspace_id"], task_id=binding["task_id"],
                    box_id=binding["box_id"], run_id=self.run_id, path=receipt["path"], branch=receipt["branch"],
                    integration=False, basis="recorded_admission_not_live_probe"))
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
                    try:
                        # Read-only existence test, never signal authority. A
                        # missing leader does not prove its group has stopped.
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        payload.update(state="orphan_dead", finished_at=None, exit_code=None)
                        self._replace_json(path, payload)
                    except PermissionError:
                        records.append({"path": str(path), **payload})
                    else:
                        records.append({"path": str(path), **payload})
            except (KeyError, OSError, json.JSONDecodeError, SandboxError) as error:
                raise SupervisorError("invocation recovery record is malformed") from error
        return records

    def status(self) -> Dict[str, Any]:
        state = self.orchestrator.state(self.run_id)
        result = {
            "schema": "camol.supervisor_status",
            "schema_version": 1,
            "pid": os.getpid(),
            "mode": self.mode,
            "draining": self.draining,
            "last_error": self.last_error,
            "watch_error": self.watch_error,
            "watchers": self.watch_runtime.inspect() if self.watch_runtime else {},
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
        if state.get("worker_gateway") and self.worker_gateway is not None:
            result["worker_gateway"] = self.worker_gateway.status()
        return result

    def _plan(self) -> Dict[str, Any]:
        state = self.orchestrator.state(self.run_id)
        return {
            "schema": "camol.control_plan",
            "schema_version": 1,
            "run_id": self.run_id,
            "plan_digest": state["plan_digest"],
            **({"source_binding": state["source_binding"]} if state.get("source_binding") else {}),
            "approved_by": state.get("approved_by"),
            "status": state["status"],
            "runbook": self.runbook,
        }

    def _box(self, box_id: str, after_seq: int, limit: int, *, tail: bool = False) -> Dict[str, Any]:
        from .box_inspection import box_snapshot, read_artifact_preview, BoxInspectionError
        state = self.orchestrator.state(self.run_id)
        history_events = self.store.read(self.run_id, after_seq=0)
        allowance = [8 << 20]
        try:
            return box_snapshot(self.run_id, self.runbook, state, history_events, box_id,
                after_seq=after_seq, limit=limit, tail=tail,
                preview_reader=lambda reference: read_artifact_preview(self.paths.state_dir, reference, allowance))
        except BoxInspectionError as error:
            raise SupervisorError(str(error)) from error

    async def _events(self, after_seq: int, limit: int, wait_ms: int) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + (wait_ms / 1000)
        while True:
            events = self.store.read(self.run_id, after_seq=after_seq, limit=limit)
            if events or wait_ms == 0 or asyncio.get_running_loop().time() >= deadline:
                return {
                    "schema": "camol.control_events",
                    "schema_version": 1,
                    "events": events,
                    "next_seq": events[-1]["seq"] if events else after_seq,
                }
            await asyncio.sleep(0.1)

    async def _watch_driver(self) -> None:
        while not self._shutdown.is_set():
            if not self.draining and not self.orphans and self.orchestrator.state(self.run_id)["status"] not in {"draft", "superseded"}:
                try:
                    before = self.orchestrator.state(self.run_id)["watchers"]
                    await self.watch_runtime.tick()
                    after = self.orchestrator.state(self.run_id)["watchers"]
                    if any(before[key]["status"] != item["status"] or before[key]["observations"] != item["observations"]
                           for key, item in after.items() if key in before):
                        self._wake.set()
                    self.watch_error = None
                except Exception as error:
                    self.watch_error = self.orchestrator.redactor.text("{}: {}".format(type(error).__name__, error))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _worker_driver(self):
        while not self._shutdown.is_set():
            try:
                await self.worker_gateway.tick()
            except (OSError, ValueError, RuntimeError) as error:
                self.worker_gateway.error = type(error).__name__
                await self.worker_gateway.close()
            if self.worker_gateway._active()[1] is None:
                return
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _driver(self) -> None:
        while not self._shutdown.is_set():
            state = self.orchestrator.state(self.run_id)
            if self.orphans:
                self.mode = "orphaned"
                self._wake.clear()
                await self._wake.wait()
                continue
            if self.draining:
                self.mode = "drained"
                if self.stop_after_drain:
                    self._shutdown.set()
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            if state["status"] in {"completed", "blocked"}:
                # A terminal daemon remains inspectable and reattachable until
                # the owner explicitly stops it.
                self.mode = "terminal"
                self._wake.clear()
                await self._wake.wait()
                continue
            if state["status"] == "awaiting_acceptance":
                self.mode = "awaiting_acceptance"
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
            # Requests arriving while the driver awaits must remain set; clearing
            # this after a run could lose a concurrent resume or capacity signal.
            self._wake.clear()
            try:
                capacity_cursor = (self.runner.capacity.change_cursor()
                    if state["runbook"].get("schema_version", 0) >= 6 else None)
                # Reached only after startup orphan reconciliation; expired
                # unused slots cannot be reclaimed while their process lives.
                if self.runner.capacity is not None:
                    self.runner.capacity.release_finished(self.run_id, processes_stopped=True)
                state = await self.runner.run_until_terminal(
                    self.run_id, should_drain=lambda: self.draining
                )
            except Exception as error:
                self.last_error = self.orchestrator.redactor.text("{}: {}".format(type(error).__name__, error))
                self.mode = "operator_attention"
                self._wake.clear()
                await self._wake.wait()
                continue
            self.last_error = None
            if state["status"] in {"completed", "blocked", "awaiting_acceptance"}:
                continue
            if self.draining:
                continue
            gate_wait = any(task.get("gate_wait") for task in state["tasks"].values())
            self.mode = "awaiting_gate" if gate_wait else "waiting"
            # A waiting run requires an external-state change or operator retry;
            # do not busy-loop probes against an unchanged world.
            if state.get("capacity_waits") and capacity_cursor is not None and not gate_wait:
                await self._wait_capacity_change(state, capacity_cursor)
            elif gate_wait or state["last_seq"] == before or any(task["status"] == "waiting" for task in state["tasks"].values()):
                await self._wake.wait()

    async def _wait_capacity_change(self, state, before_cursor):
        """Wake for another controller's supply/release without probe polling."""
        deadlines = [parse_timestamp(item["wake_at"], "capacity wake")
            for item in state.get("capacity_waits", {}).values() if item.get("wake_at")]
        while not self._wake.is_set():
            if self.runner.capacity.change_cursor() != before_cursor:
                return
            now = self.orchestrator.clock()
            if deadlines and min(deadlines) <= now:
                return
            delay = min(1.0, max(0.01, (min(deadlines) - now).total_seconds())) if deadlines else 1.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _force_shutdown(self, requested_by: str, *, expected_run_id=None, expected_plan_digest=None) -> None:
        if expected_run_id is not None and (self.run_id != expected_run_id or self.orchestrator.state(self.run_id)["plan_digest"] != expected_plan_digest):
            self.last_error = "force-stop target changed before dispatch"
            self.mode = "operator_attention"
            return
        if self._driver_task is not None:
            self._driver_task.cancel()
            try:
                await self._driver_task
            except asyncio.CancelledError:
                pass
        try:
            await self._terminate_orphans()
        except SupervisorError as error:
            self.last_error = str(error)
            self.mode = "operator_attention"
            return
        if expected_run_id is not None and (self.run_id != expected_run_id or self.orchestrator.state(self.run_id)["plan_digest"] != expected_plan_digest):
            self.last_error = "force-stop target changed during process cancellation"
            self.mode = "operator_attention"
            return
        if self.runner is not None and self.run_id is not None:
            self.runner.force_interrupt(self.run_id, requested_by=requested_by)
        self.mode = "stopped"
        self._shutdown.set()

    async def _terminate_orphans(self) -> None:
        # A persisted ps/lstart marker has only second-resolution and is not a
        # non-reusable process handle. It cannot authorize a signal after this
        # supervisor restarted; retain operator attention instead of guessing.
        if self.orphans:
            raise SupervisorError("persisted orphan ownership cannot be proven by the legacy birth marker; inspect and reconcile externally before resuming")

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        version = request.get("schema_version")
        fields = (
            ("schema", "schema_version", "token", "command", "requested_by")
            if version == 1 else
            ("schema", "schema_version", "token", "request_id", "command", "requested_by", "params")
        )
        if version == 3:
            fields += ("expected_run_id", "expected_plan_digest")
        reject_unknown_fields(request, fields, "control request")
        if (
            request.get("schema") != "camol.control_request"
            or type(request.get("schema_version")) is not int
            or request.get("schema_version") not in {1, 2, 3}
        ):
            raise SupervisorError("unsupported control request")
        if not isinstance(request.get("token"), str) or not hmac.compare_digest(request["token"], self.token):
            raise SupervisorError("control authentication failed")
        if version == 3:
            # Every mutating branch below remains synchronous through dispatch.
            # The only awaited control is read-only event polling. New awaited
            # mutations must revalidate this binding after their final await.
            state = self.orchestrator.state(self.run_id)
            if (request.get("expected_run_id") != self.run_id
                    or request.get("expected_plan_digest") != state["plan_digest"]):
                raise SupervisorError("control target run or frozen plan has changed")
        command = request.get("command")
        if not isinstance(command, str):
            raise SupervisorError("control command must be a string")
        params: Dict[str, Any] = {}
        if version in {2, 3}:
            if not isinstance(request.get("request_id"), str) or not request["request_id"]:
                raise SupervisorError("control request_id is required")
            if not isinstance(request.get("params"), dict):
                raise SupervisorError("control params must be an object")
            params = request["params"]
        elif "requested_by" not in request:
            request["requested_by"] = "operator"
        if command == "ping":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control ping params")
            return {"ok": True, "pid": os.getpid()}
        if command in {"status", "boxes"}:
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control {} params".format(command))
            return {"ok": True, "result": self.status() if command == "status" else self._boxes()}
        if command == "drain":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control drain params")
            self.draining = True
            self.mode = "draining"
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "resume":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control resume params")
            if self.orphans:
                raise SupervisorError("live orphan invocation requires external operator reconciliation; its persisted birth marker is not signal authority")
            self.draining = False
            self.stop_after_drain = False
            self.mode = "running"
            self._wake.set()
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "approve":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control approve params")
            state = self.orchestrator.state(self.run_id)
            if state["status"] != "draft":
                raise SupervisorError("run is not awaiting plan approval")
            approved_by = str(request.get("requested_by") or "")
            if not approved_by:
                raise SupervisorError("approval requires requested_by")
            self.orchestrator.approve_plan(self.run_id, approved_by, state["plan_digest"])
            self._wake.set()
            return {"ok": True, "result": {"plan_digest": state["plan_digest"], "approved_by": approved_by}}
        if version in {2, 3} and command == "plan":
            reject_unknown_fields(params, (), "control plan params")
            return {"ok": True, "result": self._plan()}
        if version in {2, 3} and command.startswith("worker-"):
            params = dict(params)
            for name, expected in (("expected_workspace", str(self.workspace)), ("expected_database", str(self.paths.database))):
                if name in params and params.pop(name) != expected:
                    raise SupervisorError("worker control expected workspace or database differs from this supervisor")
            contracts = {
                "worker-gateway-status": set(),
                "worker-gateway-configure": {"policy", "approval_digest", "approved_by"},
                "worker-gateway-stop": {"configuration_id", "policy_digest", "approved_by"},
                "worker-stream-prepare": {"stream", "approved_by"},
                "worker-stream-approve": {"proposal", "review_digest", "approved_by"},
                "worker-stream-revoke": {"scope", "reason", "approved_by"},
                "worker-stream-inspect": {"scope"},
                "worker-stream-records": {"scope", "after", "limit"},
                "worker-stream-import": {"scope", "request_id", "limit", "approved_by"},
            }
            if command not in contracts or set(params) != contracts[command]:
                raise SupervisorError("invalid worker gateway command or fields")
            if "approved_by" in params and params["approved_by"] != request.get("requested_by"):
                raise SupervisorError("worker approval identity differs from the control actor")
            gateway = self.worker_gateway
            if gateway is None:
                raise SupervisorError("worker gateway runtime is unavailable")
            if command == "worker-gateway-status":
                result = gateway.status()
            elif command == "worker-gateway-configure":
                result = gateway.configure(params["policy"], by=params["approved_by"], approval_digest=params["approval_digest"])
                if self._worker_task is None or self._worker_task.done():
                    self._worker_task = asyncio.create_task(self._worker_driver())
            elif command == "worker-gateway-stop":
                result = gateway.stop(by=params["approved_by"], configuration_id=params["configuration_id"], policy_digest=params["policy_digest"])
            elif command == "worker-stream-prepare":
                result = gateway.streams.prepare(params["stream"], by=params["approved_by"])
            elif command == "worker-stream-approve":
                result = gateway.streams.approve(params["proposal"], by=params["approved_by"], review_digest=params["review_digest"])
            elif command == "worker-stream-revoke":
                result = gateway.streams.revoke(params["scope"], by=params["approved_by"], reason=params["reason"])
            elif command == "worker-stream-records":
                result = gateway.streams.records(params["scope"], after=params["after"], limit=params["limit"])
            elif command == "worker-stream-import":
                result = gateway.streams.import_received(params["scope"], by=params["approved_by"], request_id=params["request_id"], limit=params["limit"])
            else:
                result = gateway.streams.inspect(params["scope"])
            return {"ok": True, "result": result}
        if version in {2, 3} and command.startswith("watch-"):
            fields = {
                "watch-inspect": set(),
                "watch-create": {"spec", "approval_digest", "approved_by"},
                "watch-schedule": {"schedule", "approval_digest", "approved_by"},
                "watch-stop": {"watcher_id", "reason", "approved_by"},
                "watch-reopen": {"watcher_id", "reason", "cursor", "approved_by"},
            }
            if command not in fields or set(params) != fields[command]:
                raise SupervisorError("invalid watcher control command or fields")
            if command != "watch-inspect" and params["approved_by"] != request.get("requested_by"):
                raise SupervisorError("watcher approval identity must match the control request actor")
            if command == "watch-inspect":
                result = self.watch_runtime.inspect()
            elif command == "watch-create":
                spec = WatchSpec.from_dict(params["spec"])
                if params["approval_digest"] != canonical_digest(spec.to_dict()):
                    raise SupervisorError("approve the exact watcher specification digest")
                result = Watcher.create(self.orchestrator, self.run_id, spec, approved_by=params["approved_by"]).inspect()
            elif command == "watch-schedule":
                result = self.watch_runtime.configure(params["schedule"], approved_by=params["approved_by"], approval_digest=params["approval_digest"])
            elif command == "watch-stop":
                result = self.watch_runtime.stop(params["watcher_id"], approved_by=params["approved_by"], reason=params["reason"])
            else:
                result = Watcher(self.orchestrator, self.run_id, params["watcher_id"]).reopen(approved_by=params["approved_by"], reason=params["reason"], cursor=params["cursor"])
            return {"ok": True, "result": result}
        if version in {2, 3} and command == "acceptance":
            reject_unknown_fields(params, (), "control acceptance params")
            state = self.orchestrator.state(self.run_id)
            return {"ok": True, "result": {
                "run_id": self.run_id, "status": state["status"], "plan_digest": state["plan_digest"],
                "integration_head": state.get("integration_head"), "acceptance": state.get("acceptance"),
                "pending_gates": {task_id: task["gate_wait"] for task_id, task in state["tasks"].items() if task.get("gate_wait")},
            }}
        if version in {2, 3} and command in {"accept", "gate-approve"}:
            fields = ("approved_by", "outcome_digest") if command == "accept" else ("approved_by", "task_id", "assessment_digest")
            if set(params) != set(fields) or any(not isinstance(params.get(name), str) or not params[name] for name in fields):
                raise SupervisorError("{} requires exact fields: {}".format(command, ", ".join(fields)))
            if params["approved_by"] != request.get("requested_by"):
                raise SupervisorError("approval identity must match the authenticated control request actor")
            try:
                if command == "accept":
                    self.orchestrator.accept_run(self.run_id, params["approved_by"], params["outcome_digest"])
                else:
                    if params["task_id"] not in self.orchestrator.state(self.run_id)["tasks"]:
                        raise SupervisorError("unknown gate task")
                    self.orchestrator.approve_task_gate(self.run_id, params["task_id"], params["approved_by"], params["assessment_digest"])
            except ValueError as error:
                raise SupervisorError(str(error)) from error
            self._wake.set()
            return {"ok": True, "result": {"status": self.orchestrator.state(self.run_id)["status"], **params}}
        if version in {2, 3} and command == "events":
            reject_unknown_fields(params, ("after_seq", "limit", "wait_ms"), "control events params")
            after_seq = params.get("after_seq", 0)
            limit = params.get("limit", 100)
            wait_ms = params.get("wait_ms", 0)
            if type(after_seq) is not int or after_seq < 0:
                raise SupervisorError("events after_seq must be a non-negative integer")
            if type(limit) is not int or not 1 <= limit <= 500:
                raise SupervisorError("events limit must be between 1 and 500")
            if type(wait_ms) is not int or not 0 <= wait_ms <= 30_000:
                raise SupervisorError("events wait_ms must be between 0 and 30000")
            return {"ok": True, "result": await self._events(after_seq, limit, wait_ms)}
        if version in {2, 3} and command == "box":
            reject_unknown_fields(params, ("box_id", "after_seq", "limit", "tail"), "control box params")
            box_id = params.get("box_id")
            after_seq = params.get("after_seq", 0)
            limit = params.get("limit", 100)
            tail = params.get("tail", False)
            if not isinstance(box_id, str) or not box_id:
                raise SupervisorError("box_id is required")
            if type(after_seq) is not int or after_seq < 0:
                raise SupervisorError("box after_seq must be a non-negative integer")
            if type(limit) is not int or not 1 <= limit <= 500:
                raise SupervisorError("box limit must be between 1 and 500")
            if type(tail) is not bool:
                raise SupervisorError("box tail must be a boolean")
            return {"ok": True, "result": self._box(box_id, after_seq, limit, tail=tail)}
        if version in {2, 3} and command in {"box-observe", "box-message", "box-inbox"}:
            from .mailbox import Mailbox
            common = {"run_id", "plan_digest", "box_id"}
            extra = {"request_id", "target", "body", "kind", "correlation_id", "ttl_seconds"} if command == "box-message" else ({"offset", "limit"} if command == "box-inbox" else set())
            reject_unknown_fields(params, common | extra, "box mailbox params")
            state = self.orchestrator.state(self.run_id)
            if params.get("run_id") != self.run_id or params.get("plan_digest") != state["plan_digest"]:
                raise SupervisorError("mailbox target run or plan changed")
            if not isinstance(params.get("box_id"), str) or not params["box_id"]:
                raise SupervisorError("mailbox requires an exact string box ID")
            mailbox = Mailbox(self.orchestrator, self.run_id)
            if command == "box-inbox":
                result = mailbox.inbox(params.get("box_id"), offset=params.get("offset", 0), limit=params.get("limit", 100))
            else:
                if self.orphans or self.draining or self.mode in {"stopped", "operator_attention"}:
                    raise SupervisorError("mailbox delivery requires a connected, reconciled controller")
                if command == "box-observe":
                    result = mailbox.observe(params.get("box_id"))
                else:
                    target = params.get("target")
                    if not isinstance(target, dict) or not isinstance(target.get("subject"), dict) or target["subject"].get("box_id") != params.get("box_id"):
                        raise SupervisorError("message target differs from its exact box")
                    result = mailbox.post(target, request_id=params.get("request_id"), body=params.get("body"),
                        sender=request["requested_by"], kind=params.get("kind", "information"),
                        correlation_id=params.get("correlation_id"), ttl_seconds=params.get("ttl_seconds", 300))
            return {"ok": True, "result": result}
        if command == "stop":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control stop params")
            self.draining = True
            self.stop_after_drain = True
            self.mode = "draining"
            self._wake.set()
            return {"ok": True, "result": {"mode": self.mode}}
        if command == "force-stop":
            if version in {2, 3}:
                reject_unknown_fields(params, (), "control force-stop params")
            if self.orphans:
                raise SupervisorError("persisted orphan ownership cannot be proven; force-stop refuses to signal it")
            if self._force_task is None:
                self._force_task = asyncio.create_task(self._force_shutdown(str(request.get("requested_by") or "operator"),
                    expected_run_id=self.run_id, expected_plan_digest=self.orchestrator.state(self.run_id)["plan_digest"]))
            return {"ok": True, "result": {"mode": "stopping"}}
        raise SupervisorError("unknown control command")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw or len(raw) > self.MAX_REQUEST_BYTES:
                raise SupervisorError("control request is empty or too large")
            request = decode_contract(raw, max_bytes=self.MAX_REQUEST_BYTES)
            if not isinstance(request, dict):
                raise SupervisorError("control request must be an object")
            response = await self._dispatch(request)
        except (json.JSONDecodeError, SupervisorError, StateTransitionError, SchemaError, WatcherError, OSError, MailboxError, DeliveryError, ConcurrentAppendError) as error:
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
            self.runbook = load_runbook(self.runbook_path)
            state = self.orchestrator.initialize(self.runbook)
            self.run_id = state["run_id"]
            persisted_source = load_binding(self.paths.state_dir, self.run_id)
            approved_source = state.get("source_binding")
            if self.expected_source is not None:
                proposed_source = make_binding(self.expected_source, self.run_id, state["plan_digest"])
                if persisted_source is not None and persisted_source != proposed_source:
                    raise SupervisorError("explicit source differs from the persisted launch binding")
                persisted_source = proposed_source
            if approved_source is not None:
                if persisted_source is not None and persisted_source != approved_source:
                    raise SupervisorError("source launch binding differs from the approved event ledger")
                persisted_source = approved_source
            if self.source_binding_digest is not None and (persisted_source is None or canonical_digest(persisted_source) != self.source_binding_digest):
                raise SupervisorError("required source launch binding is missing or changed")
            if persisted_source is not None:
                if persisted_source["run_id"] != self.run_id or persisted_source["plan_digest"] != state["plan_digest"]:
                    raise SupervisorError("source launch binding belongs to a different frozen plan")
                assert_source(persisted_source["source"], self.workspace)
                persist_binding(self.paths.state_dir, persisted_source)
                self.orchestrator.bind_source(self.run_id, persisted_source["source"])
                state = self.orchestrator.state(self.run_id)
            if state["status"] == "draft" and self.approve_by:
                self.orchestrator.approve_plan(self.run_id, self.approve_by, state["plan_digest"])
                state = self.orchestrator.state(self.run_id)
            if state["status"] == "running":
                self.orchestrator.mark_interrupted_effects_unknown(self.run_id)
            self.runner = HarnessRunner(self.orchestrator, self.workspace, state_dir=self.paths.state_dir)
            self.watch_runtime = WatchRuntime(self.orchestrator, self.run_id)
            self.worker_gateway = WorkerGateway(self.orchestrator, self.run_id, self.paths.state_dir)
            self.orphans = self._scan_invocations()
            self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.paths.socket))
            os.chmod(self.paths.socket, 0o600)
            self._write_pid()
            self._driver_task = asyncio.create_task(self._driver())
            self._watch_task = asyncio.create_task(self._watch_driver())
            if state.get("worker_gateway", {}).get("active_id") is not None:
                self._worker_task = asyncio.create_task(self._worker_driver())
            loop = asyncio.get_running_loop()
            for caught in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(caught, lambda: self._schedule_stop())
                except NotImplementedError:
                    pass
            async with self.server:
                await self._shutdown.wait()
        finally:
            if self._worker_task is not None:
                self._worker_task.cancel()
                await asyncio.gather(self._worker_task, return_exceptions=True)
            if self.worker_gateway is not None:
                await self.worker_gateway.close()
            if self._watch_task is not None and not self._watch_task.done():
                self._watch_task.cancel()
                try:
                    await self._watch_task
                except asyncio.CancelledError:
                    pass
            if self._driver_task is not None and not self._driver_task.done():
                self._driver_task.cancel()
                try:
                    await self._driver_task
                except asyncio.CancelledError:
                    pass
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            if self.runner is not None:
                self.runner.close()
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
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket), limit=CONTROL_RESPONSE_LIMIT
    )
    request = {
        "schema": "camol.control_request",
        "schema_version": 1,
        "token": token,
        "command": command,
        "requested_by": requested_by,
    }
    try:
        writer.write((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
    finally:
        writer.close()
        await writer.wait_closed()
    if not response.get("ok"):
        raise SupervisorError(response.get("error") or "control request failed")
    return response


async def send_control_v2(
    state_dir: Path,
    command: str,
    *,
    requested_by: str = "operator",
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 35,
    expected_run_id: Optional[str] = None,
    expected_plan_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a typed V2 request while preserving the public V1 control client."""
    if (expected_run_id is None) != (expected_plan_digest is None):
        raise SupervisorError("bound control requires both run and plan identity")
    if expected_run_id is not None:
        from .schema import require_identifier, require_digest
        require_identifier(expected_run_id, "control run")
        require_digest(expected_plan_digest, "control plan")
    paths = SupervisorPaths.under(state_dir)
    token = _read_control_token(paths.token)
    if not paths.socket.exists() or paths.socket.is_symlink():
        raise SupervisorError("Camol supervisor socket is not available")
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket), limit=CONTROL_RESPONSE_LIMIT
    )
    request_id = "request-" + uuid4().hex
    request = {
        "schema": "camol.control_request",
        "schema_version": 2,
        "token": token,
        "request_id": request_id,
        "command": command,
        "requested_by": requested_by,
        "params": dict(params or {}),
    }
    if expected_run_id is not None:
        request.update(schema_version=3, expected_run_id=expected_run_id, expected_plan_digest=expected_plan_digest)
    try:
        writer.write((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=timeout))
    finally:
        writer.close()
        await writer.wait_closed()
    if not response.get("ok"):
        raise SupervisorError(response.get("error") or "control request failed")
    response["request_id"] = request_id
    return response


def spawn_supervisor(
    runbook: Path,
    workspace: Path,
    state_dir: Path,
    *,
    database: Optional[Path] = None,
    approve_by: Optional[str] = None,
    timeout: float = 10,
    expected_source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Start one detached authoritative supervisor and prove its socket is live."""
    workspace = Path(workspace).resolve()
    state_dir = Path(state_dir).resolve()
    WorkspaceManager(workspace, state_dir)
    paths = SupervisorPaths.under(state_dir, database)
    if paths.control_dir.is_symlink() or paths.log.is_symlink():
        raise SupervisorError("supervisor control/log path must not be a symlink")
    try:
        paths.database.relative_to(state_dir)
    except ValueError as error:
        raise SupervisorError("supervisor database must stay inside the state directory") from error
    paths.control_dir.mkdir(parents=True, exist_ok=True)
    frozen_runbook = load_runbook(runbook)
    launch_binding = load_binding(state_dir, frozen_runbook["run"]["id"])
    if expected_source is not None:
        launch_binding = make_binding(expected_source, frozen_runbook["run"]["id"], runbook_digest(frozen_runbook))
        assert_source(launch_binding["source"], workspace)
        persist_binding(state_dir, launch_binding)
    package_root = str(Path(__file__).resolve().parents[1])
    bootstrap = "import sys;sys.path.insert(0,{});from camol.cli import main;raise SystemExit(main())".format(repr(package_root))
    command = [
        sys.executable,
        "-I",
        "-c",
        bootstrap,
        "serve",
        str(Path(runbook).resolve()),
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state_dir),
    ]
    if database:
        command.extend(["--db", str(Path(database).resolve())])
    if approve_by:
        command.extend(["--approve-by", approve_by])
    if launch_binding is not None:
        command.extend(["--source-binding-digest", canonical_digest(launch_binding)])
    child_environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    with paths.log.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            env=child_environment,
        )
    _DETACHED_CHILDREN[:] = [child for child in _DETACHED_CHILDREN if child.poll() is None]
    _DETACHED_CHILDREN.append(process)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SupervisorError("supervisor exited during startup; inspect {}".format(paths.log))
        if paths.socket.exists() and paths.token.exists():
            try:
                response = asyncio.run(send_control(state_dir, "ping"))
                if response.get("pid") != process.pid:
                    raise SupervisorError("control endpoint is not the newly launched supervisor")
                actual_plan = asyncio.run(send_control_v2(state_dir, "plan"))["result"]
                if (actual_plan.get("run_id") != frozen_runbook["run"]["id"]
                        or actual_plan.get("plan_digest") != runbook_digest(frozen_runbook)
                        or (launch_binding is not None and actual_plan.get("source_binding") != launch_binding)):
                    raise SupervisorError("control endpoint does not bind the exact launched plan and source")
            except (SupervisorError, OSError, asyncio.TimeoutError, json.JSONDecodeError):
                # A previous crash can leave both files behind. The new child
                # owns the leader lock and will replace the socket; keep
                # polling until that endpoint responds or the child exits.
                pass
            else:
                return {
                    "started": True,
                    "pid": response["pid"],
                    "socket": str(paths.socket),
                    "log": str(paths.log),
                }
        time.sleep(0.05)
    process.terminate()
    raise SupervisorError("supervisor did not become ready within {} seconds".format(timeout))
