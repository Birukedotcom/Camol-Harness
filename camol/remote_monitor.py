"""Read-only, bounded pane inspection through the pinned SSH control adapter."""

import asyncio
import json
from datetime import datetime, timezone

from .probes import Redactor
from .schema import canonical_digest
from .ssh_protocol import RESPONSE_LIMIT, SSHTransportError, strict_json, text


MAX_BOXES = 10000
DISPLAY_LIMIT = 64000


def display_text(value, maximum=DISPLAY_LIMIT):
    """Remote data stays literal; credentials and terminal controls are not UI."""
    value = Redactor().text(value)
    value = "".join(char if char in "\n\t" or char.isprintable() else "\\u{:04x}".format(ord(char)) for char in value)
    return value if len(value) <= maximum else value[:maximum] + "\n[display truncated]"


class RemoteMonitor:
    """One immutable target scope; no generic command dispatch or mutation API.

    SSH authenticates the remote host's reports, not the truth of a compromised
    host. Snapshots are observations, never readiness or lease capabilities.
    """

    def __init__(self, client):
        self.client = client
        self.target = client.target
        if not {"status", "box"}.issubset(self.target.allowed_commands):
            raise SSHTransportError("POLICY_DENIED", "remote monitor requires status and box in the approved target allowlist")
        self.scope = self.target.digest()
        self._lock = None
        self._loop = None

    async def _read(self, command, params=None):
        if command not in {"status", "box"} or self.client.target.digest() != self.scope:
            raise SSHTransportError("POLICY_DENIED", "remote monitor target changed")
        response = await self.client.request(command, params=params)
        if self.client.target.digest() != self.scope:
            raise SSHTransportError("POLICY_DENIED", "remote monitor target changed during inspection")
        # Also bound injected clients; production SSH has its own frame limit.
        try:
            raw = json.dumps(response, allow_nan=False).encode()
            if len(raw) > RESPONSE_LIMIT:
                raise ValueError("oversize")
            response = strict_json(raw)
        except (TypeError, ValueError, RecursionError) as error:
            raise SSHTransportError("PROTOCOL_DENIED", "invalid remote monitor response") from error
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise SSHTransportError("REMOTE_REJECTED", "remote supervisor rejected read-only inspection")
        return response["result"]

    def _bound(self, value):
        if (value.get("run_id"), value.get("plan_digest")) != (self.target.run_id, self.target.plan_digest):
            raise SSHTransportError("PROTOCOL_DENIED", "remote observation belongs to another run or plan")

    async def refresh(self, box_id=None):
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            if self._lock is not None and self._lock.locked():
                raise SSHTransportError("POLICY_DENIED", "remote monitor is already active on another event loop")
            self._loop, self._lock = loop, asyncio.Lock()
        async with self._lock:
            status = await self._read("status")
            run = status.get("run")
            if not isinstance(run, dict) or status.get("schema") != "camol.supervisor_status" or type(status.get("schema_version")) is not int or status["schema_version"] != 1:
                raise SSHTransportError("PROTOCOL_DENIED", "unsupported remote supervisor status")
            self._bound(run)
            usage = run.get("total_tokens")
            if usage is not None and (type(usage) is not int or usage < 0):
                raise SSHTransportError("PROTOCOL_DENIED", "invalid remote usage count")
            agents = run.get("agents")
            if not isinstance(agents, dict) or len(agents) > MAX_BOXES:
                raise SSHTransportError("PROTOCOL_DENIED", "remote worker inventory is invalid or exceeds the monitor limit")
            rows = []
            for identity, agent in sorted(agents.items()):
                text(identity, "box identity")
                if not isinstance(agent, dict) or not isinstance(agent.get("status"), str) or not isinstance(agent.get("role"), str) or (agent.get("task_id") is not None and not isinstance(agent["task_id"], str)):
                    raise SSHTransportError("PROTOCOL_DENIED", "invalid remote worker summary")
                rows.append(dict(box_id=identity, status=agent["status"], role=agent["role"], task_id=agent.get("task_id")))
            detail = None
            if box_id is not None:
                if box_id not in agents:
                    raise SSHTransportError("POLICY_DENIED", "selected box is absent from this target's current run")
                detail = await self._read("box", dict(box_id=box_id, after_seq=0, limit=100, tail=True))
                self._bound(detail)
                if (detail.get("schema") != "camol.control_box" or type(detail.get("schema_version")) is not int
                        or detail["schema_version"] != 1 or detail.get("box_id") != box_id
                        or detail.get("snapshot_digest") != canonical_digest({key: value for key, value in detail.items() if key != "snapshot_digest"})):
                    raise SSHTransportError("PROTOCOL_DENIED", "remote box snapshot identity or digest differs")
            return dict(scope=self.scope, run_id=self.target.run_id, plan_digest=self.target.plan_digest,
                        observed_at=datetime.now(timezone.utc).isoformat(), rows=rows,
                        status=run.get("status", "unknown"), mode=status.get("mode", "unknown"),
                        total_tokens=run.get("total_tokens"), detail=detail, selected_box=box_id,
                        basis="authenticated_remote_report_not_readiness", read_only=True)


def render_snapshot(snapshot):
    if snapshot["detail"] is not None:
        return display_text(json.dumps(snapshot["detail"], indent=2, ensure_ascii=False))
    return display_text("REMOTE OVERVIEW — read-only\nRun: {}\nPlan: {}\nStatus: {} / {}\n"
                        "Registered boxes: {}\nReported model usage: {} tokens\nObserved: {}\n\n"
                        "Select a box to inspect its retained context, events and evidence.\n"
                        "An authenticated response does not prove worker readiness.\n"
                        "Closing this monitor never stops or changes the remote run.".format(
                            snapshot["run_id"], snapshot["plan_digest"], snapshot["status"], snapshot["mode"],
                            len(snapshot["rows"]), snapshot["total_tokens"], snapshot["observed_at"]))
