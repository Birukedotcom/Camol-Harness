"""Authoritative state transitions for a run over an N-worker pool."""

from typing import Any, Dict, List, Optional
from uuid import uuid4

from .events import EVIDENCE_KINDS, new_event
from .hillclimb import agent_efficiency_vector, compare_vectors
from .runbook import runbook_digest, validate_runbook
from .state import project
from .store import SQLiteEventStore


class StateTransitionError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, store: SQLiteEventStore, actor_id: str = "orchestrator"):
        self.store = store
        self.actor_id = actor_id

    def state(self, run_id: str) -> Dict[str, Any]:
        return project(self.store.read(run_id))

    def _emit(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        actor_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.store.append(
            new_event(
                run_id,
                event_type,
                actor_id or self.actor_id,
                payload,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
        )

    def _require_status(self, run_id: str, *statuses: str) -> Dict[str, Any]:
        state = self.state(run_id)
        if state["status"] not in statuses:
            raise StateTransitionError(
                "run {} must be in {}, not {}".format(run_id, "/".join(statuses), state["status"])
            )
        return state

    def initialize(self, raw_runbook: Dict[str, Any]) -> Dict[str, Any]:
        runbook = validate_runbook(raw_runbook)
        run_id = runbook["run"]["id"]
        digest = runbook_digest(runbook)
        if self.store.has_run(run_id):
            state = self.state(run_id)
            if state["plan_digest"] != digest:
                raise StateTransitionError(
                    "run {} already exists with a different frozen plan".format(run_id)
                )
            return state
        self._emit(run_id, "RUN_CREATED", {"runbook": runbook, "plan_digest": digest})
        return self.state(run_id)

    def approve_plan(self, run_id: str, approved_by: str, expected_digest: str) -> None:
        state = self._require_status(run_id, "draft")
        if state["plan_digest"] != expected_digest:
            raise StateTransitionError("the plan changed after review; re-open planning")
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        self._emit(
            run_id,
            "PLAN_APPROVED",
            {"approved_by": approved_by, "plan_digest": expected_digest},
        )

    def start(self, run_id: str) -> None:
        state = self._require_status(run_id, "ready", "running")
        if state["status"] == "ready":
            self._emit(run_id, "RUN_STARTED", {})

    def ready_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        state = self._require_status(run_id, "running")
        ready = []
        for task in state["tasks"].values():
            if task["status"] != "pending":
                continue
            if all(state["tasks"][dependency]["status"] == "succeeded" for dependency in task["depends_on"]):
                ready.append(task)
        return ready

    @staticmethod
    def _agent_order(agent: Dict[str, Any], task: Dict[str, Any]) -> Any:
        efficiency = agent_efficiency_vector(agent)
        capability_surplus = len(set(agent["capabilities"]) - set(task["capabilities"]))
        return (
            capability_surplus,
            -efficiency["successful_tasks"],
            efficiency["failed_attempts"],
            -efficiency["verified_steps_per_1k_tokens"],
            efficiency["total_tokens"],
            agent["id"],
        )

    def lease_ready_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        state = self._require_status(run_id, "running")
        run_config = state["runbook"]["run"]
        max_concurrency = run_config.get("max_concurrency", run_config.get("max_agents"))
        active_count = sum(agent["status"] != "idle" for agent in state["agents"].values())
        available_capacity = max(0, max_concurrency - active_count)
        if available_capacity == 0:
            return []
        idle_agents = [agent for agent in state["agents"].values() if agent["status"] == "idle"]
        assignments = []
        for task in self.ready_tasks(run_id):
            eligible = [
                agent
                for agent in idle_agents
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            if not eligible:
                continue
            agent = sorted(eligible, key=lambda candidate: self._agent_order(candidate, task))[0]
            lease_id = str(uuid4())
            event = self._emit(
                run_id,
                "TASK_LEASED",
                {"task_id": task["id"], "agent_id": agent["id"], "lease_id": lease_id},
            )
            assignments.append(event["payload"])
            idle_agents = [candidate for candidate in idle_agents if candidate["id"] != agent["id"]]
            if len(assignments) == available_capacity:
                break
        return assignments

    def _require_lease(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        lease_id: str,
        *task_statuses: str,
    ) -> Any:
        state = self._require_status(run_id, "running")
        task = state["tasks"].get(task_id)
        if (
            task is None
            or task["status"] not in task_statuses
            or task["agent_id"] != agent_id
            or task["lease_id"] != lease_id
        ):
            raise StateTransitionError(
                "agent {} does not hold the active lease for task {}".format(agent_id, task_id)
            )
        return state, task

    def start_task(self, run_id: str, assignment: Dict[str, str]) -> None:
        self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
        )
        self._emit(
            run_id,
            "TASK_STARTED",
            dict(assignment),
            actor_id=assignment["agent_id"],
        )

    def context_packet(self, run_id: str, assignment: Dict[str, str]) -> Dict[str, Any]:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            "running",
        )
        runbook = state["runbook"]
        completed = set(task["completed_step_ids"])
        dependency_receipts = []
        for dependency_id in task["depends_on"]:
            dependency = state["tasks"][dependency_id]
            dependency_receipts.append(
                {
                    "task_id": dependency_id,
                    "submission": dependency.get("submission"),
                    "evidence": [
                        state["evidence"][evidence_id]
                        for evidence_id in dependency["evidence_ids"]
                        if state["evidence"][evidence_id]["kind"]
                        in {"artifact", "claim", "test_result"}
                    ],
                }
            )
        last_verification = task["verification_history"][-1] if task["verification_history"] else None
        return {
            "protocol": "camol-agent-turn/v1",
            "run": {
                "id": run_id,
                "objective": runbook["run"]["objective"],
                "plan_digest": state["plan_digest"],
                "completion": runbook["run"]["completion"],
            },
            "lease": {
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
            },
            "rules": runbook["rules"],
            "task": {
                "id": task["id"],
                "goal": task["goal"],
                "acceptance": task["acceptance"],
                "required_evidence": task["required_evidence"],
                "attempt": task["attempts"] + (1 if task["status"] == "leased" else 0),
                "remaining_steps": [step for step in task["steps"] if step["id"] not in completed],
            },
            "dependency_receipts": dependency_receipts,
            "routed_messages": [
                message for message in state["messages"] if message["to_task_id"] == task["id"]
            ][-10:],
            "last_checkpoint": task["checkpoints"][-1] if task["checkpoints"] else None,
            "last_verification": last_verification,
            "token_budget": {
                "turn": runbook["run"]["token_policy"]["max_tokens_per_turn"],
                "checkpoint_reserve": runbook["run"]["token_policy"]["checkpoint_reserve"],
                "run_remaining": max(
                    0,
                    runbook["run"]["token_policy"]["max_total_tokens"] - state["total_tokens"],
                ),
            },
            "return_contract": {
                "status": ["continue", "complete", "blocked"],
                "required": [
                    "status",
                    "checkpoint",
                    "completed_step_ids",
                    "input_tokens",
                    "output_tokens",
                    "evidence",
                ],
            },
        }

    def record_turn(
        self,
        run_id: str,
        assignment: Dict[str, str],
        result: Dict[str, Any],
    ) -> List[str]:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
        )
        completed_step_ids = result.get("completed_step_ids")
        if not isinstance(completed_step_ids, list) or any(
            not isinstance(step_id, str) for step_id in completed_step_ids
        ):
            raise ValueError("completed_step_ids must be an array of strings")
        valid_step_ids = {step["id"] for step in task["steps"]}
        if not set(completed_step_ids).issubset(valid_step_ids):
            raise ValueError("turn claimed an unknown task step")
        checkpoint = result.get("checkpoint")
        if not isinstance(checkpoint, str) or not checkpoint.strip():
            raise ValueError("every turn must leave a non-empty checkpoint")
        input_tokens = result.get("input_tokens")
        output_tokens = result.get("output_tokens")
        if not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("input_tokens must be a non-negative integer")
        if not isinstance(output_tokens, int) or output_tokens < 0:
            raise ValueError("output_tokens must be a non-negative integer")
        previous_steps = set(task["completed_step_ids"])
        newly_completed = len(set(completed_step_ids) - previous_steps)
        used_tokens = input_tokens + output_tokens
        policy = state["runbook"]["run"]["token_policy"]
        violations = []
        if used_tokens > policy["max_tokens_per_turn"]:
            violations.append("turn_token_budget_exceeded")
        if state["total_tokens"] + used_tokens > policy["max_total_tokens"]:
            violations.append("run_token_budget_exceeded")
        if task["turn_count"] + 1 > policy["max_turns_per_task"]:
            violations.append("task_turn_budget_exceeded")
        checkpoint_tokens_estimate = (len(checkpoint) + 3) // 4
        if checkpoint_tokens_estimate > policy["checkpoint_reserve"]:
            violations.append("checkpoint_reserve_exceeded")

        self._emit(
            run_id,
            "AGENT_TURN_RECORDED",
            {
                "task_id": task["id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "status": result.get("status"),
                "checkpoint": checkpoint,
                "completed_step_ids": completed_step_ids,
                "newly_completed_steps": newly_completed,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "checkpoint_tokens_estimate": checkpoint_tokens_estimate,
                "budget_violations": violations,
            },
            actor_id=assignment["agent_id"],
        )
        return violations

    def route_message(
        self,
        run_id: str,
        assignment: Dict[str, str],
        to_task_id: str,
        kind: str,
        body: str,
    ) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
        )
        if to_task_id not in state["tasks"]:
            raise ValueError("message target is not a declared task")
        if kind not in {"information", "question", "proposal", "warning"}:
            raise ValueError("unsupported routed message kind")
        if not isinstance(body, str) or not body.strip() or len(body) > 2000:
            raise ValueError("routed message body must contain 1-2000 characters")
        self._emit(
            run_id,
            "MESSAGE_ROUTED",
            {
                "message_id": str(uuid4()),
                "from_task_id": task["id"],
                "to_task_id": to_task_id,
                "kind": kind,
                "body": body,
            },
            actor_id=assignment["agent_id"],
        )

    def record_evidence(
        self,
        run_id: str,
        assignment: Dict[str, str],
        evidence: Dict[str, Any],
    ) -> str:
        self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
        )
        kind = evidence.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unknown evidence kind: {}".format(kind))
        data = evidence.get("data")
        if not isinstance(data, dict):
            raise ValueError("evidence data must be an object")
        evidence_id = evidence.get("evidence_id") or str(uuid4())
        state = self.state(run_id)
        if evidence_id in state["evidence"]:
            raise ValueError("evidence id already exists: {}".format(evidence_id))
        self._emit(
            run_id,
            "EVIDENCE_RECORDED",
            {
                "evidence_id": evidence_id,
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
                "kind": kind,
                "data": data,
            },
            actor_id=assignment["agent_id"],
        )
        return evidence_id

    def submit_task(
        self,
        run_id: str,
        assignment: Dict[str, str],
        summary: str,
    ) -> None:
        _, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
        )
        required_steps = {step["id"] for step in task["steps"]}
        if not required_steps.issubset(set(task["completed_step_ids"])):
            missing = sorted(required_steps - set(task["completed_step_ids"]))
            raise StateTransitionError("task is missing completed steps: {}".format(", ".join(missing)))
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("submission summary is required")
        self._emit(
            run_id,
            "TASK_SUBMITTED",
            {"task_id": task["id"], "lease_id": assignment["lease_id"], "summary": summary},
            actor_id=assignment["agent_id"],
        )

    def record_verification(
        self,
        run_id: str,
        assignment: Dict[str, str],
        checks: List[Dict[str, Any]],
    ) -> bool:
        self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "verifying",
        )
        passed = bool(checks) and all(check.get("passed") is True for check in checks)
        self._emit(
            run_id,
            "TASK_VERIFICATION_RECORDED",
            {"task_id": assignment["task_id"], "passed": passed, "checks": checks},
        )
        return passed

    def succeed_task(self, run_id: str, assignment: Dict[str, str]) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "verifying",
        )
        if not task["verification_history"] or task["verification_history"][-1]["passed"] is not True:
            raise StateTransitionError("the latest verification is not green")
        present_kinds = {
            state["evidence"][evidence_id]["kind"] for evidence_id in task["evidence_ids"]
        }
        missing = sorted(set(task["required_evidence"]) - present_kinds)
        if missing:
            raise StateTransitionError("task is missing required evidence: {}".format(", ".join(missing)))
        self._emit(run_id, "TASK_SUCCEEDED", {"task_id": task["id"]})

    def retry_or_block(self, run_id: str, assignment: Dict[str, str], reason: str) -> str:
        _, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
        )
        if task["attempts"] >= task["max_attempts"]:
            self._emit(
                run_id,
                "TASK_BLOCKED",
                {"task_id": task["id"], "blocker": {"kind": "attempts_exhausted", "detail": reason}},
            )
            return "blocked"
        self._emit(run_id, "TASK_RETRY_SCHEDULED", {"task_id": task["id"], "reason": reason})
        return "retry"

    def block_task(
        self,
        run_id: str,
        assignment: Dict[str, str],
        blocker: Dict[str, Any],
    ) -> None:
        _, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
        )
        self._emit(run_id, "TASK_BLOCKED", {"task_id": task["id"], "blocker": blocker})

    def completion_report(self, run_id: str) -> Dict[str, Any]:
        state = self.state(run_id)
        tasks = list(state["tasks"].values())
        blockers = [task["id"] for task in tasks if task["status"] == "blocked"]
        incomplete = [task["id"] for task in tasks if task["status"] != "succeeded"]
        missing_evidence = {}
        failed_verification = []
        for task in tasks:
            present = {state["evidence"][item]["kind"] for item in task["evidence_ids"]}
            missing = sorted(set(task["required_evidence"]) - present)
            if missing:
                missing_evidence[task["id"]] = missing
            if not task["verification_history"] or task["verification_history"][-1]["passed"] is not True:
                failed_verification.append(task["id"])
        conditions = {
            "all_tasks_succeeded": not incomplete,
            "all_required_evidence_present": not missing_evidence,
            "all_verifications_green": not failed_verification,
            "no_open_blockers": not blockers,
            "no_open_debug_cases": not any(
                debug_case["status"] == "open" for debug_case in state["debug_cases"].values()
            ),
        }
        required = state["runbook"]["run"]["completion"] if state["runbook"] else []
        return {
            "complete": bool(required) and all(conditions[name] for name in required),
            "conditions": conditions,
            "incomplete_tasks": incomplete,
            "blocked_tasks": blockers,
            "missing_evidence": missing_evidence,
            "failed_verification": failed_verification,
        }

    def maybe_finish(self, run_id: str) -> bool:
        state = self._require_status(run_id, "running")
        report = self.completion_report(run_id)
        if report["complete"]:
            self._emit(
                run_id,
                "RUN_COMPLETED",
                {
                    "verdict": "all declared completion conditions are satisfied",
                    "plan_digest": state["plan_digest"],
                    "total_tokens": state["total_tokens"],
                },
            )
            return True
        return False

    def block_run(self, run_id: str, reason: str, details: Dict[str, Any]) -> None:
        self._require_status(run_id, "running")
        self._emit(run_id, "RUN_BLOCKED", {"reason": reason, "details": details})

    def record_hillclimb(
        self,
        run_id: str,
        hillclimb_id: str,
        baseline: Dict[str, float],
        candidate: Dict[str, float],
        dimensions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self._require_status(run_id, "running")
        verdict = compare_vectors(baseline, candidate, dimensions)
        self._emit(
            run_id,
            "HILLCLIMB_RECORDED",
            {
                "hillclimb_id": hillclimb_id,
                "baseline": baseline,
                "candidate": candidate,
                "dimensions": dimensions,
                "verdict": verdict,
            },
        )
        return verdict

    def open_debug_case(
        self,
        run_id: str,
        case_id: str,
        observed_behavior: str,
        target_behavior: str,
        reproduction: List[str],
        required_evidence: List[str],
    ) -> None:
        state = self._require_status(run_id, "running")
        if case_id in state["debug_cases"]:
            raise StateTransitionError("debug case already exists: {}".format(case_id))
        if not observed_behavior.strip() or not target_behavior.strip():
            raise ValueError("observed and target behavior are required")
        if not reproduction:
            raise ValueError("debug reproduction must not be empty")
        if any(not isinstance(step, str) or not step.strip() for step in reproduction):
            raise ValueError("debug reproduction steps must be non-empty strings")
        if any(
            not isinstance(kind, str) or not kind.strip() for kind in required_evidence
        ):
            raise ValueError("debug evidence kinds must be non-empty strings")
        unknown = sorted(set(required_evidence) - EVIDENCE_KINDS)
        if not required_evidence or unknown:
            raise ValueError("invalid debug evidence contract: {}".format(", ".join(unknown)))
        self._emit(
            run_id,
            "DEBUG_CASE_OPENED",
            {
                "case_id": case_id,
                "observed_behavior": observed_behavior,
                "target_behavior": target_behavior,
                "reproduction": reproduction,
                "required_evidence": required_evidence,
            },
        )

    def record_debug_evidence(
        self,
        run_id: str,
        case_id: str,
        kind: str,
        data: Dict[str, Any],
        actor_id: str,
    ) -> str:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "open":
            raise StateTransitionError("debug case is not open: {}".format(case_id))
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unknown evidence kind: {}".format(kind))
        if not isinstance(data, dict):
            raise ValueError("debug evidence data must be an object")
        evidence_id = str(uuid4())
        self._emit(
            run_id,
            "EVIDENCE_RECORDED",
            {
                "evidence_id": evidence_id,
                "debug_case_id": case_id,
                "kind": kind,
                "data": data,
            },
            actor_id=actor_id,
        )
        return evidence_id

    def verify_debug_case(self, run_id: str, case_id: str, verdict: str) -> None:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "open":
            raise StateTransitionError("debug case is not open: {}".format(case_id))
        present = {
            state["evidence"][evidence_id]["kind"]
            for evidence_id in debug_case["evidence_ids"]
        }
        missing = sorted(set(debug_case["required_evidence"]) - present)
        if missing:
            raise StateTransitionError(
                "debug case is missing required evidence: {}".format(", ".join(missing))
            )
        if not verdict.strip():
            raise ValueError("verification verdict is required")
        self._emit(
            run_id,
            "DEBUG_CASE_VERIFIED",
            {"case_id": case_id, "verdict": verdict},
        )

    def promote_eval(
        self,
        run_id: str,
        case_id: str,
        eval_id: str,
        definition: Dict[str, Any],
    ) -> None:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "verified":
            raise StateTransitionError("debug case must be verified before eval promotion")
        if eval_id in state["evals"]:
            raise StateTransitionError("eval already exists: {}".format(eval_id))
        if not definition.get("fixture") or not definition.get("oracle"):
            raise ValueError("eval definition requires fixture and oracle")
        self._emit(
            run_id,
            "EVAL_PROMOTED",
            {"case_id": case_id, "eval_id": eval_id, "definition": definition},
        )
