"""Long-running execution-slot scheduler and verification loop."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Dict

from .adapter import AdapterError, ProcessAgentAdapter
from .orchestrator import Orchestrator, StateTransitionError


class HarnessRunner:
    def __init__(self, orchestrator: Orchestrator, workspace: Path):
        self.orchestrator = orchestrator
        self.workspace = Path(workspace).resolve()

    def _agent_for(self, state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
        return state["agents"][agent_id]

    async def _run_check(
        self, box: Path, command: Dict[str, Any], timeout_seconds: int = 900
    ) -> Dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command["argv"],
                cwd=str(box),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": process.returncode,
                "passed": process.returncode == 0,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            }
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
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
        box = (self.workspace / agent["box"]).resolve()
        checks = []
        for command in task["verification"]:
            checks.append(await self._run_check(box, command))
        self.orchestrator.record_evidence(
            run_id,
            assignment,
            {
                "kind": "test_result",
                "data": {
                    "passed": bool(checks) and all(check["passed"] for check in checks),
                    "checks": checks,
                },
            },
        )
        return self.orchestrator.record_verification(run_id, assignment, checks)

    async def _execute_assignment(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> None:
        state = self.orchestrator.state(run_id)
        agent = self._agent_for(state, assignment["agent_id"])
        adapter = ProcessAgentAdapter(self.workspace, run_id)
        task_status = state["tasks"][assignment["task_id"]]["status"]
        if task_status == "leased":
            self.orchestrator.start_task(run_id, assignment)
        elif task_status != "running":
            raise StateTransitionError("cannot execute task from {}".format(task_status))

        while True:
            state = self.orchestrator.state(run_id)
            task = state["tasks"][assignment["task_id"]]
            packet = self.orchestrator.context_packet(run_id, assignment)
            try:
                result = await adapter.execute_turn(
                    agent,
                    assignment,
                    packet,
                    task["turn_count"] + 1,
                )
            except (AdapterError, OSError) as error:
                self.orchestrator.retry_or_block(run_id, assignment, "adapter_error: {}".format(error))
                return

            try:
                violations = self.orchestrator.record_turn(run_id, assignment, result)
                for evidence in result["evidence"]:
                    self.orchestrator.record_evidence(run_id, assignment, evidence)
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
            }
            for task in state["tasks"].values()
            if task["status"] in {"leased", "running", "verifying"}
        ]

    def _deadlock_details(self, run_id: str) -> Dict[str, Any]:
        state = self.orchestrator.state(run_id)
        details = {}
        for task in state["tasks"].values():
            if task["status"] != "pending":
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

            assignments = self.orchestrator.lease_ready_tasks(run_id)
            if not assignments:
                details = self._deadlock_details(run_id)
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
