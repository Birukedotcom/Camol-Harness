"""Public, terminal-independent embedding API for the same execution kernel."""

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .artifacts import ArtifactStore, RunArchive
from .debugger import Debugger
from .watchers import WatchSpec, Watcher
from .orchestrator import Orchestrator, StateTransitionError
from .runbook import load_runbook
from .revisions import RevisionError, collect_revision_lineage
from .runner import HarnessRunner, summary
from .store import SQLiteEventStore
from .supervisor import LeaderLock, SupervisorPaths
from .workspace import WorkspaceManager


class Harness:
    """Own a Camol run in a Python application using a synchronous context manager.

    No process is started by construction, ``prepare`` or ``approve``. Call
    ``await run_async()`` (or ``run()`` outside an event loop) to execute the exact
    approved plan. The same leader lock as the daemon prevents two execution owners.
    Event consumers use durable sequence cursors; reconnecting never requires a TUI.
    """

    def __init__(self, workspace: Path, state_dir: Path, *, database: Optional[Path] = None):
        self.workspace = Path(workspace).resolve()
        self.paths = SupervisorPaths.under(state_dir, database=database)
        try:
            self.paths.database.relative_to(self.paths.state_dir)
        except ValueError as error:
            raise StateTransitionError("embedded database must stay inside its leader-locked state directory") from error
        manager = WorkspaceManager(self.workspace, self.paths.state_dir)
        for protected in (manager.source, manager.git_common_dir):
            try:
                self.paths.database.relative_to(protected)
            except ValueError:
                continue
            raise StateTransitionError("execution database must be outside the source repository and Git common directory")
        self.store = None
        self.orchestrator = None
        self.runner = None
        self.run_id: Optional[str] = None
        self._running = False
        self._pause_requested = False
        self._lock = LeaderLock(self.paths.lock)

    def __enter__(self) -> "Harness":
        if self.store is not None:
            raise StateTransitionError("the embedded harness is already open")
        if self.paths.control_dir.is_symlink() or self.paths.lock.is_symlink():
            raise StateTransitionError("embedded harness control paths cannot be symlinks")
        self.paths.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.paths.control_dir, 0o700)
        self._lock.acquire()
        try:
            self.store = SQLiteEventStore(self.paths.database)
            self.orchestrator = Orchestrator(self.store)
            self.runner = HarnessRunner(self.orchestrator, self.workspace, state_dir=self.paths.state_dir)
            self.run_id = self.store.latest_run_id()
        except BaseException:
            if self.store is not None:
                self.store.close()
            self.store = self.orchestrator = self.runner = None
            self.run_id = None
            self._lock.release()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._running:
            raise StateTransitionError("await run_async or its cancellation before closing the harness")
        if self.runner is not None:
            self.runner.close()
        if self.store is not None:
            self.store.close()
            self.store = None
        self.orchestrator = self.runner = None
        self._lock.release()

    def _require_open(self) -> None:
        if self.store is None:
            raise StateTransitionError("use Harness as a context manager")

    def _require_run(self) -> str:
        self._require_open()
        if self.run_id is None:
            raise StateTransitionError("prepare a runbook first")
        return self.run_id

    def prepare(self, runbook: Any) -> Dict[str, Any]:
        self._require_open()
        if self._running:
            raise StateTransitionError("cannot replace an executing plan")
        document = load_runbook(Path(runbook)) if isinstance(runbook, (str, Path)) else runbook
        if self.run_id is not None and document.get("run", {}).get("id") != self.run_id:
            raise StateTransitionError("use a separate state directory for another run")
        state = self.orchestrator.initialize(document)
        self.run_id = state["run_id"]
        return state

    def approve(self, *, by: str, digest: str) -> Dict[str, Any]:
        run_id = self._require_run()
        self.orchestrator.approve_plan(run_id, by, digest)
        return self.state()

    def propose_revision(self, runbook: Any, *, reason: str, effect_reruns: Optional[list] = None) -> Dict[str, Any]:
        run_id = self._require_run()
        document = load_runbook(Path(runbook)) if isinstance(runbook, (str, Path)) else runbook
        return self.orchestrator.propose_revision(run_id, document, reason, effect_reruns=effect_reruns)

    def apply_revision(self, *, by: str, proposal_digest: str) -> Dict[str, Any]:
        run_id = self._require_run()
        if self._running:
            raise RevisionError("drain the embedded execution before applying a plan revision")
        current = self.state()
        proposal = current.get("revision_proposals", {}).get(proposal_digest)
        if proposal is None:
            raise RevisionError("unknown revision proposal")
        manager = WorkspaceManager(self.workspace, self.paths.state_dir)
        manager.assert_source_ready()
        head = proposal["integration_head"]
        if head is not None:
            resolved = manager._git("-C", str(manager.source), "rev-parse", "--verify", head + "^{commit}")
            if resolved != head:
                raise RevisionError("revision baseline is not the exact reachable commit")
            if current["integrations"]:
                from .evaluation import IntegrationReceipt
                receipt = IntegrationReceipt.from_dict(current["integrations"][-1])
                if receipt.revision != head or receipt.workspace.repository_id != manager._repository_id():
                    raise RevisionError("revision baseline does not match the source integration receipt")
        successor = self.orchestrator.apply_revision(run_id, proposal_digest, by)
        self.run_id = successor["run_id"]
        previous_runner = self.runner
        self.runner = HarnessRunner(self.orchestrator, self.workspace, state_dir=self.paths.state_dir)
        previous_runner.close()
        return successor

    def state(self) -> Dict[str, Any]:
        run_id = self._require_run()
        return self.orchestrator.state(run_id)

    def status(self) -> Dict[str, Any]:
        return summary(self.state())

    def events(self, *, after_seq: int = 0, limit: Optional[int] = None) -> list:
        """Read one durable cursor page; omit limit for a complete legacy snapshot.

        Long-running consumers should pass a finite limit and resume after the
        last returned sequence. The bound is applied by SQLite before decoding.
        """
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError("limit must be an integer between 1 and 10000")
        run_id = self._require_run()
        return self.store.read(run_id, after_seq=after_seq, limit=limit)

    def usage(self) -> Dict[str, Any]:
        from .usage import usage_report
        return usage_report(self.events())

    def inspect_box(self, box_id: str, *, after_seq: int = 0, limit: int = 200, tail: bool = False) -> Dict[str, Any]:
        """Inspect the exact selected run without changing its execution state.

        Use BoxInspector directly to inspect retained state after closing Harness.
        """
        from .box_inspection import BoxInspector
        return BoxInspector(self.paths.state_dir, database=self.paths.database).read(
            self._require_run(), box_id, after_seq=after_seq, limit=limit, tail=tail)

    @property
    def debugger(self) -> Debugger:
        run_id = self._require_run()
        return Debugger(self.orchestrator, run_id)

    def watch(self, spec: WatchSpec, *, approved_by: str) -> Watcher:
        """Create an explicitly approved, cursor-based observation contract."""
        return Watcher.create(self.orchestrator, self._require_run(), spec, approved_by=approved_by)

    def watcher(self, watcher_id: str) -> Watcher:
        watcher = Watcher(self.orchestrator, self._require_run(), watcher_id)
        watcher.inspect()
        return watcher

    def observers(self, *, sources=None):
        """Return an explicitly configured observation loop; never starts it."""
        from .watch_runtime import WatchRuntime
        return WatchRuntime(self.orchestrator, self._require_run(), sources=sources)

    def approve_gate(self, task_id: str, *, by: str, assessment_digest: str) -> Dict[str, Any]:
        run_id = self._require_run()
        self.orchestrator.approve_task_gate(run_id, task_id, by, assessment_digest)
        return self.state()

    def accept(self, *, by: str, outcome_digest: str) -> Dict[str, Any]:
        run_id = self._require_run()
        self.orchestrator.accept_run(run_id, by, outcome_digest)
        return self.state()

    def pause(self) -> None:
        """Request drain at the next task boundary; accepted work stays durable."""
        self._require_open()
        self._pause_requested = True

    async def run_async(self, *, should_pause: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        run_id = self._require_run()
        if self._running:
            raise StateTransitionError("this harness is already executing")
        current = self.state()
        if current["status"] == "draft":
            raise StateTransitionError("approve the exact plan digest before execution")
        if current["status"] in {"completed", "blocked", "awaiting_acceptance", "superseded"}:
            return current
        self._running = True
        self._pause_requested = False
        try:
            return await self.runner.run_until_terminal(
                run_id, should_drain=lambda: self._pause_requested or bool(should_pause and should_pause()),
            )
        except asyncio.CancelledError:
            # Runner cancellation waits for process-group shutdown before this
            # synchronous salvage/fencing step. Resume uses recorded work only.
            self.runner.force_interrupt(run_id, requested_by="embedding-client")
            raise
        finally:
            self._running = False

    def run(self, *, should_pause: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(should_pause=should_pause))
        raise StateTransitionError("inside an event loop, use await harness.run_async()")

    def export(self, destination: Path) -> dict:
        run_id = self._require_run()
        return RunArchive.export(run_id, self.events(), ArtifactStore(self.paths.state_dir), Path(destination),
                                 lineage_events=collect_revision_lineage(self.store, run_id))

    def mailbox(self):
        """Owner-scoped observation/post/inbox API; no implicit execution."""
        from .mailbox import Mailbox
        return Mailbox(self.orchestrator, self._require_run())
