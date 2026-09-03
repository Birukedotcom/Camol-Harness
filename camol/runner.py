"""Long-running execution-slot scheduler and verification loop."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .admission import AdmissionBundle, AdmissionController, AdmissionError
from .adapter import AdapterError, create_agent_adapter
from .artifacts import ArtifactError, ArtifactRef, ArtifactStore
from .orchestrator import Orchestrator, StateTransitionError
from .readiness import WaitingReason
from .sandbox import SandboxError, select_backend
from .workspace import WorkspaceError, WorkspaceHandle, WorkspaceManager
from .providers import ProviderError, load_model_profile


class HarnessRunner:
    def __init__(self, orchestrator: Orchestrator, workspace: Path, *, state_dir: Optional[Path] = None):
        self.orchestrator = orchestrator
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else None
        self.workspaces = WorkspaceManager(self.workspace, self.state_dir) if self.state_dir else None
        self.artifacts = ArtifactStore(self.state_dir, redactor=self.orchestrator.redactor) if self.state_dir else None
        self._handles: Dict[Tuple[str, str], WorkspaceHandle] = {}

    def _agent_for(self, state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
        return state["agents"][agent_id]

    @staticmethod
    def _provider_cost_remaining(state: Dict[str, Any], agent: Dict[str, Any], task_id: str, workspace: Path) -> Optional[int]:
        if agent["adapter"]["kind"] == "process":
            return None
        profile = load_model_profile(workspace, agent["adapter"]["profile"])
        run_used_micros = 0
        task_used_micros = 0
        for evidence in state.get("evidence", {}).values():
            if evidence.get("kind") != "model_usage":
                continue
            amount = evidence.get("data", {}).get("cost_usd_micros", 0)
            if type(amount) is not int or amount < 0:
                continue
            run_used_micros += amount
            if evidence.get("task_id") == task_id:
                task_used_micros += amount
        run_remaining = max(0, profile.max_run_usd_cents - (run_used_micros + 9_999) // 10_000)
        task_remaining = max(0, profile.max_task_usd_cents - (task_used_micros + 9_999) // 10_000)
        return min(profile.max_turn_usd_cents, task_remaining, run_remaining)

    async def _bounded_stream(self, stream: asyncio.StreamReader, limit: int = 1 << 20):
        retained = bytearray()
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if len(retained) < limit:
                retained.extend(chunk[: limit - len(retained)])
        return bytes(retained), "sha256:" + digest.hexdigest(), total, total > len(retained)

    async def _run_check(
        self,
        box: Path,
        command: Dict[str, Any],
        assignment: Dict[str, str],
        timeout_seconds: int = 900,
    ) -> Dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command["argv"],
                cwd=str(box),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_task = asyncio.create_task(self._bounded_stream(process.stdout))
            stderr_task = asyncio.create_task(self._bounded_stream(process.stderr))
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            references = []
            if self.artifacts is not None:
                invocation_id = "verify-{}".format(hashlib.sha256(repr(command["argv"]).encode()).hexdigest()[:20])
                producer = {
                    "run_id": assignment["fence"]["run_id"],
                    "task_id": assignment["task_id"],
                    "agent_id": assignment["agent_id"],
                    "lease_id": assignment["lease_id"],
                    "invocation_id": invocation_id,
                    "role": "verifier",
                }
                for channel, capture in (("stdout", stdout), ("stderr", stderr)):
                    reference = self.artifacts.put_bytes(
                        capture[0],
                        producer=dict(producer, channel=channel),
                        media_type="text/plain",
                        redact=True,
                        source_sha256=capture[1],
                        source_bytes=capture[2],
                        truncated=capture[3],
                    )
                    references.append(reference.to_dict())
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": process.returncode,
                "passed": process.returncode == 0,
                "stdout_sha256": stdout[1],
                "stderr_sha256": stderr[1],
                "stdout_bytes": stdout[2],
                "stderr_bytes": stderr[2],
                "stdout_truncated": stdout[3],
                "stderr_truncated": stderr[3],
                "artifact_refs": references,
            }
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            if "stdout_task" in locals():
                await asyncio.gather(stdout_task, stderr_task)
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": None,
                "passed": False,
                "error": "verification_timeout",
            }
        except OSError as error:
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": None,
                "passed": False,
                "error": "verification_launch_error: {}".format(error),
            }

    async def _verify(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> bool:
        state = self.orchestrator.state(run_id)
        task = state["tasks"][assignment["task_id"]]
        agent = state["agents"][assignment["agent_id"]]
        execution_workspace = self._execution_workspace(run_id, assignment)
        box = (execution_workspace / agent["box"]).resolve()
        checks = []
        for command in task["verification"]:
            check = await self._run_check(box, command, assignment)
            checks.append(check)
            self.orchestrator.record_observed_evidence(
                run_id,
                assignment,
                kind="command",
                data={key: value for key, value in check.items() if key != "artifact_refs"},
                epistemic_status="EXECUTED",
                producer="verifier",
                artifact_refs=tuple(
                    {
                        item["digest"]: ArtifactRef.from_dict(item)
                        for item in check.get("artifact_refs", [])
                    }.values()
                ),
            )
        all_references = {
            item["digest"]: ArtifactRef.from_dict(item)
            for check in checks
            for item in check.get("artifact_refs", [])
        }
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind="test_result",
            data={
                "passed": bool(checks) and all(check["passed"] for check in checks),
                "checks": [{key: value for key, value in check.items() if key != "artifact_refs"} for check in checks],
            },
            epistemic_status="EXECUTED",
            producer="verifier",
            artifact_refs=tuple(all_references.values()),
        )
        return self.orchestrator.record_verification(run_id, assignment, checks)

    def _record_observed(
        self, run_id: str, assignment: Dict[str, str], evidence: Dict[str, Any]
    ) -> None:
        expected = {"kind", "data", "epistemic_status", "producer", "artifact_refs"}
        if not isinstance(evidence, dict) or set(evidence) != expected:
            raise AdapterError("adapter evidence envelope is malformed")
        references = tuple(
            {
                item["digest"]: ArtifactRef.from_dict(item)
                for item in evidence["artifact_refs"]
            }.values()
        )
        if self.artifacts is not None:
            for reference in references:
                self.artifacts.verify(reference)
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind=evidence["kind"],
            data=evidence["data"],
            epistemic_status=evidence["epistemic_status"],
            producer=evidence["producer"],
            artifact_refs=references,
        )

    @staticmethod
    def _text_artifact(path: Path) -> bool:
        return path.suffix.lower() in {
            ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml",
            ".sh", ".zsh", ".bash", ".sql", ".csv", ".log",
        }

    def _capture_worker_artifact(
        self,
        run_id: str,
        assignment: Dict[str, str],
        execution_workspace: Path,
        evidence: Dict[str, Any],
    ) -> None:
        if self.artifacts is None:
            return
        data = evidence.get("data")
        path_value = data.get("path") if isinstance(data, dict) else None
        if not isinstance(path_value, str) or not path_value:
            raise ArtifactError("worker artifact evidence must name a path")
        raw = Path(path_value)
        candidate = (raw if raw.is_absolute() else execution_workspace / raw).resolve()
        try:
            relative = candidate.relative_to(execution_workspace.resolve())
        except ValueError as error:
            raise ArtifactError("worker artifact path escapes its task workspace") from error
        if candidate.is_symlink() or not candidate.is_file():
            raise ArtifactError("worker artifact must be a regular non-symlink file")
        reference = self.artifacts.put_file(
            candidate,
            producer={
                "run_id": run_id,
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "channel": "worker-artifact",
                "role": "collector",
            },
            media_type="text/plain" if self._text_artifact(candidate) else "application/octet-stream",
            redact=self._text_artifact(candidate),
        )
        claimed = data.get("sha256")
        if isinstance(claimed, str):
            normalized = claimed if claimed.startswith("sha256:") else "sha256:" + claimed
            if normalized != reference.source_sha256:
                raise ArtifactError("worker artifact hash claim does not match observed bytes")
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind="artifact",
            data={
                "path": str(relative),
                "source_sha256": reference.source_sha256,
                "source_bytes": reference.source_bytes,
                "worker_hash_matched": isinstance(claimed, str),
            },
            epistemic_status="OBSERVED",
            producer="collector",
            artifact_refs=(reference,),
        )

    def _capture_workspace_diff(self, run_id: str, assignment: Dict[str, str]) -> None:
        if self.workspaces is None or self.artifacts is None:
            return
        handle = self._handles[(assignment["task_id"], assignment["agent_id"])]
        patch, files, status = self.workspaces.diff_snapshot(handle)
        producer = {
            "run_id": run_id,
            "task_id": assignment["task_id"],
            "agent_id": assignment["agent_id"],
            "lease_id": assignment["lease_id"],
            "role": "collector",
        }
        references = [
            self.artifacts.put_bytes(
                patch,
                producer=dict(producer, channel="git-diff"),
                media_type="text/x-diff",
                redact=True,
            )
        ]
        file_records = []
        for path in files:
            reference = self.artifacts.put_file(
                path,
                producer=dict(producer, channel="workspace-file"),
                media_type="text/plain" if self._text_artifact(path) else "application/octet-stream",
                redact=self._text_artifact(path),
            )
            references.append(reference)
            file_records.append(
                {
                    "path": str(path.relative_to(handle.path)),
                    "source_sha256": reference.source_sha256,
                    "source_bytes": reference.source_bytes,
                }
            )
        unique = {reference.digest: reference for reference in references}
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind="diff",
            data={"status": list(status), "files": file_records},
            epistemic_status="OBSERVED",
            producer="collector",
            artifact_refs=tuple(unique.values()),
        )

    async def _execute_assignment(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> None:
        state = self.orchestrator.state(run_id)
        agent = self._agent_for(state, assignment["agent_id"])
        execution_workspace = self._execution_workspace(run_id, assignment)
        if self.state_dir is not None:
            bundle = self._admission_for(state, assignment)
            adapter = create_agent_adapter(
                agent["adapter"]["kind"],
                execution_workspace,
                run_id,
                state_dir=self.state_dir,
                sandbox_backend=select_backend(bundle.sandbox_policy),
                sandbox_policy=bundle.sandbox_policy,
                artifact_store=self.artifacts,
                redactor=self.orchestrator.redactor,
            )
        else:
            adapter = create_agent_adapter(agent["adapter"]["kind"], execution_workspace, run_id)
        task_status = state["tasks"][assignment["task_id"]]["status"]
        if task_status == "leased":
            if self.workspaces is not None:
                observed_workspace = self.workspaces.refresh_receipt(
                    self._handles[(assignment["task_id"], assignment["agent_id"])]
                )
                if observed_workspace.digest() != bundle.workspace.digest():
                    self.orchestrator.reject_launch(
                        run_id,
                        assignment,
                        WaitingReason(
                            code="WORKSPACE_CONFLICT",
                            detail="workspace changed after admission and before process launch",
                            wake_condition="workspace state is re-probed and a new fenced lease is issued",
                            task_id=assignment["task_id"],
                            box_id=assignment["agent_id"],
                        ),
                    )
                    return
            if not self.orchestrator.start_task(run_id, assignment):
                return
        elif task_status != "running":
            raise StateTransitionError("cannot execute task from {}".format(task_status))

        while True:
            self.orchestrator.heartbeat(run_id, assignment)
            state = self.orchestrator.state(run_id)
            task = state["tasks"][assignment["task_id"]]
            packet = self.orchestrator.context_packet(run_id, assignment)
            try:
                result = await adapter.execute_turn(
                    agent,
                    assignment,
                    packet,
                    task["turn_count"] + 1,
                    cost_budget_cents=self._provider_cost_remaining(state, agent, task["id"], execution_workspace),
                )
            except (AdapterError, SandboxError, ArtifactError, ProviderError, OSError) as error:
                for observed in getattr(error, "observed_evidence", ()):
                    self._record_observed(run_id, assignment, observed)
                self.orchestrator.retry_or_block(run_id, assignment, "adapter_error: {}".format(error))
                return

            try:
                observed_evidence = result.pop("_camol_observed_evidence", [])
                violations = self.orchestrator.record_turn(run_id, assignment, result)
                for observed in observed_evidence:
                    self._record_observed(run_id, assignment, observed)
                for evidence in result["evidence"]:
                    self.orchestrator.record_evidence(run_id, assignment, evidence)
                    if evidence.get("kind") == "artifact":
                        self._capture_worker_artifact(run_id, assignment, execution_workspace, evidence)
                self._capture_workspace_diff(run_id, assignment)
                for message in result.get("messages", []):
                    self.orchestrator.route_message(
                        run_id,
                        assignment,
                        message.get("to_task_id"),
                        message.get("kind", "information"),
                        message.get("body"),
                    )
            except (ValueError, StateTransitionError) as error:
                self.orchestrator.retry_or_block(
                    run_id, assignment, "invalid_agent_result: {}".format(error)
                )
                return
            if violations:
                self.orchestrator.retry_or_block(
                    run_id, assignment, "budget violations: {}".format(", ".join(violations))
                )
                return
            if result["status"] == "blocked":
                self.orchestrator.block_task(run_id, assignment, result["blocker"])
                return
            if result["status"] == "continue":
                continue

            try:
                self.orchestrator.submit_task(run_id, assignment, result["summary"])
                verification_passed = await self._verify(run_id, assignment)
                if not verification_passed:
                    self.orchestrator.retry_or_block(
                        run_id, assignment, "one or more verification commands failed"
                    )
                    return
                self.orchestrator.succeed_task(run_id, assignment)
            except StateTransitionError as error:
                self.orchestrator.retry_or_block(run_id, assignment, str(error))
            return

    async def _resume_verification(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> None:
        state = self.orchestrator.state(run_id)
        task = state["tasks"][assignment["task_id"]]
        if task["verification_history"] and task["verification_history"][-1]["passed"] is True:
            try:
                self.orchestrator.succeed_task(run_id, assignment)
            except StateTransitionError as error:
                self.orchestrator.retry_or_block(run_id, assignment, str(error))
            return
        passed = await self._verify(run_id, assignment)
        if not passed:
            self.orchestrator.retry_or_block(run_id, assignment, "verification failed after resume")
            return
        try:
            self.orchestrator.succeed_task(run_id, assignment)
        except StateTransitionError as error:
            self.orchestrator.retry_or_block(run_id, assignment, str(error))

    def _active_assignments(self, run_id: str) -> Any:
        state = self.orchestrator.state(run_id)
        return [
            {
                "task_id": task["id"],
                "agent_id": task["agent_id"],
                "lease_id": task["lease_id"],
                "status": task["status"],
                "fence_digest": task["fence_digest"],
                "fence": task["lease_fence"],
                "admission_digest": task.get("admission_digest"),
            }
            for task in state["tasks"].values()
            if task["status"] in {"leased", "running", "verifying"}
        ]

    def _admission_for(self, state: Dict[str, Any], assignment: Dict[str, Any]) -> AdmissionBundle:
        payload = state["admissions"].get("{}:{}".format(assignment["task_id"], assignment["agent_id"]))
        if payload is None:
            raise StateTransitionError("active assignment has no admission bundle")
        bundle = AdmissionBundle.from_dict(payload)
        if bundle.digest() != assignment.get("admission_digest"):
            raise StateTransitionError("active assignment admission digest changed")
        return bundle

    def _execution_workspace(self, run_id: str, assignment: Dict[str, Any]) -> Path:
        if self.workspaces is None:
            return self.workspace
        key = (assignment["task_id"], assignment["agent_id"])
        handle = self._handles.get(key)
        if handle is None:
            handle = self.workspaces.prepare_task(run_id, assignment["task_id"], assignment["agent_id"])
            self._handles[key] = handle
        return handle.path

    def _ensure_admissions(self, run_id: str) -> None:
        if self.workspaces is None:
            return
        state = self.orchestrator.state(run_id)
        runbook = state["runbook"]
        maximum = runbook["run"].get("max_concurrency", runbook["run"].get("max_agents"))
        active_reservations = set(self.orchestrator.active_reservation_ids(run_id))
        remaining = max(0, maximum - len(active_reservations))
        if remaining == 0:
            return
        reserved_workers = {
            AdmissionBundle.from_dict(payload).binding.worker_id
            for payload in state["admissions"].values()
            if AdmissionBundle.from_dict(payload).reservation.reservation_id in active_reservations
        }
        controller = AdmissionController(
            runbook,
            self.workspaces,
            clock=self.orchestrator.clock,
        )
        tasks = [
            task for task in state["tasks"].values()
            if task["status"] in {"pending", "waiting"}
            and all(state["tasks"][item]["status"] == "succeeded" for item in task["depends_on"])
        ]
        idle_agents = [
            agent for agent in state["agents"].values()
            if agent["status"] == "idle" and agent["id"] not in reserved_workers
        ]
        for task in tasks:
            if remaining == 0:
                break
            already = []
            for agent in idle_agents:
                payload = state["admissions"].get("{}:{}".format(task["id"], agent["id"]))
                if payload is not None:
                    bundle = AdmissionBundle.from_dict(payload)
                    if bundle.reservation.reservation_id in active_reservations:
                        already.append(agent["id"])
            if already:
                continue
            eligible = [
                agent for agent in idle_agents
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            if not eligible:
                continue
            agent = sorted(eligible, key=lambda candidate: self.orchestrator._agent_order(candidate, task))[0]
            try:
                bundle, handle = controller.prepare(
                    plan_digest=state["plan_digest"],
                    task=task,
                    agent=agent,
                    granted_by=state["approved_by"],
                )
            except (AdmissionError, WorkspaceError, SandboxError) as error:
                code = "WORKSPACE_CONFLICT" if isinstance(error, WorkspaceError) else "POLICY_DENIED"
                self.orchestrator.wait_task(
                    run_id,
                    task["id"],
                    WaitingReason(
                        code=code,
                        detail="admission preparation failed: {}".format(error),
                        wake_condition="repair the admission prerequisite and retry probing",
                        task_id=task["id"],
                        box_id=agent["id"],
                    ),
                )
                continue
            self._handles[(task["id"], agent["id"])] = handle
            decision = self.orchestrator.record_admission(run_id, bundle)
            state = self.orchestrator.state(run_id)
            if decision.ready:
                remaining -= 1
                idle_agents = [item for item in idle_agents if item["id"] != agent["id"]]

    def _deadlock_details(self, run_id: str) -> Dict[str, Any]:
        state = self.orchestrator.state(run_id)
        details = {}
        for task in state["tasks"].values():
            if task["status"] not in {"pending", "waiting"}:
                continue
            unmet = [
                dependency
                for dependency in task["depends_on"]
                if state["tasks"][dependency]["status"] != "succeeded"
            ]
            eligible = [
                agent["id"]
                for agent in state["agents"].values()
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            details[task["id"]] = {
                "unmet_dependencies": unmet,
                "eligible_agents": eligible,
                "attempts": task["attempts"],
            }
        return details

    async def run_until_terminal(self, run_id: str) -> Dict[str, Any]:
        self.orchestrator.start(run_id)
        if self.workspaces is not None:
            self.workspaces.prepare_integration(run_id)
        while True:
            state = self.orchestrator.state(run_id)
            if state["status"] in {"completed", "blocked"}:
                return state
            if self.orchestrator.maybe_finish(run_id):
                return self.orchestrator.state(run_id)

            active = self._active_assignments(run_id)
            if active:
                await asyncio.gather(
                    *(
                        self._resume_verification(run_id, assignment)
                        if assignment["status"] == "verifying"
                        else self._execute_assignment(run_id, assignment)
                        for assignment in active
                    )
                )
                continue

            try:
                self._ensure_admissions(run_id)
            except (AdmissionError, WorkspaceError, SandboxError, StateTransitionError) as error:
                self.orchestrator.block_run(run_id, "admission_failure", {"detail": str(error)})
                return self.orchestrator.state(run_id)
            assignments = self.orchestrator.lease_ready_tasks(run_id)
            if not assignments:
                details = self._deadlock_details(run_id)
                state = self.orchestrator.state(run_id)
                if any(task["status"] == "waiting" for task in state["tasks"].values()):
                    return state
                self.orchestrator.block_run(run_id, "scheduler_deadlock", details)
                return self.orchestrator.state(run_id)

            await asyncio.gather(
                *(self._execute_assignment(run_id, assignment) for assignment in assignments)
            )


def summary(state: Dict[str, Any]) -> Dict[str, Any]:
    run_config = state["runbook"]["run"]
    return {
        "run_id": state["run_id"],
        "status": state["status"],
        "max_concurrency": run_config.get("max_concurrency", run_config.get("max_agents")),
        "registered_workers": len(state["agents"]),
        "plan_digest": state["plan_digest"],
        "approved_by": state["approved_by"],
        "total_tokens": state["total_tokens"],
        "agents": {
            agent_id: {
                "role": agent["role"],
                "status": agent["status"],
                "task_id": agent["task_id"],
                "stats": agent["stats"],
            }
            for agent_id, agent in state["agents"].items()
        },
        "tasks": {
            task_id: {
                "status": task["status"],
                "agent_id": task["agent_id"],
                "attempts": task["attempts"],
                "turn_count": task["turn_count"],
                "completed_step_ids": task["completed_step_ids"],
                "blocker": task["blocker"],
            }
            for task_id, task in state["tasks"].items()
        },
        "terminal": state.get("terminal"),
    }
