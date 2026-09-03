"""Loading, normalization, and validation for executable JSON runbooks."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .events import EVIDENCE_KINDS


class RunbookError(ValueError):
    pass


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise RunbookError("{} must be an object".format(label))
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunbookError("{} must be a non-empty string".format(label))
    return value


def _identifier(value: Any, label: str) -> str:
    text = _string(value, label)
    if not ID_PATTERN.fullmatch(text):
        raise RunbookError("{} must contain only letters, numbers, dot, underscore, or dash".format(label))
    return text


def _string_list(value: Any, label: str, *, allow_empty: bool = True) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RunbookError("{} must be an array of non-empty strings".format(label))
    if not allow_empty and not value:
        raise RunbookError("{} must not be empty".format(label))
    return list(value)


def _unique(items: Iterable[str], label: str) -> None:
    values = list(items)
    if len(values) != len(set(values)):
        raise RunbookError("{} must be unique".format(label))


def _relative_path(value: Any, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise RunbookError("{} must stay inside the harness workspace".format(label))
    return text


def validate_runbook(raw: Dict[str, Any]) -> Dict[str, Any]:
    root = _object(raw, "runbook")
    if root.get("schema_version") != 1:
        raise RunbookError("schema_version must be 1")

    run = _object(root.get("run"), "run")
    run_id = _identifier(run.get("id"), "run.id")
    objective = _string(run.get("objective"), "run.objective")
    if run.get("max_agents") != 3:
        raise RunbookError("run.max_agents must be exactly 3 in the v1 harness")

    completion = _string_list(run.get("completion", []), "run.completion", allow_empty=False)
    supported_completion = {
        "all_tasks_succeeded",
        "all_required_evidence_present",
        "all_verifications_green",
        "no_open_blockers",
        "no_open_debug_cases",
    }
    unknown_completion = sorted(set(completion) - supported_completion)
    if unknown_completion:
        raise RunbookError("unsupported completion conditions: {}".format(", ".join(unknown_completion)))

    token_policy = _object(run.get("token_policy"), "run.token_policy")
    for field in ("max_tokens_per_turn", "max_total_tokens", "max_turns_per_task"):
        value = token_policy.get(field)
        if not isinstance(value, int) or value <= 0:
            raise RunbookError("run.token_policy.{} must be a positive integer".format(field))
    checkpoint_reserve = token_policy.get("checkpoint_reserve", 0)
    if not isinstance(checkpoint_reserve, int) or checkpoint_reserve < 0:
        raise RunbookError("run.token_policy.checkpoint_reserve must be a non-negative integer")
    if checkpoint_reserve >= token_policy["max_tokens_per_turn"]:
        raise RunbookError("checkpoint_reserve must be smaller than max_tokens_per_turn")

    rules = root.get("rules", [])
    if not isinstance(rules, list):
        raise RunbookError("rules must be an array")
    normalized_rules = []
    for index, rule_value in enumerate(rules):
        rule = _object(rule_value, "rules[{}]".format(index))
        enforcement = rule.get("enforcement")
        if enforcement not in {"hard", "review"}:
            raise RunbookError("rules[{}].enforcement must be hard or review".format(index))
        normalized_rules.append(
            {
                "id": _identifier(rule.get("id"), "rules[{}].id".format(index)),
                "text": _string(rule.get("text"), "rules[{}].text".format(index)),
                "enforcement": enforcement,
            }
        )
    _unique((rule["id"] for rule in normalized_rules), "rule ids")

    agents = root.get("agents")
    if not isinstance(agents, list) or len(agents) != 3:
        raise RunbookError("agents must define exactly three registered v1 slots")
    normalized_agents = []
    for index, agent_value in enumerate(agents):
        agent = _object(agent_value, "agents[{}]".format(index))
        adapter = _object(agent.get("adapter"), "agents[{}].adapter".format(index))
        if adapter.get("kind") != "process":
            raise RunbookError("agents[{}].adapter.kind must be process".format(index))
        argv = _string_list(adapter.get("argv"), "agents[{}].adapter.argv".format(index), allow_empty=False)
        timeout_seconds = adapter.get("timeout_seconds", 1800)
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise RunbookError("agents[{}].adapter.timeout_seconds must be positive".format(index))
        normalized_agents.append(
            {
                "id": _identifier(agent.get("id"), "agents[{}].id".format(index)),
                "role": _string(agent.get("role"), "agents[{}].role".format(index)),
                "box": _relative_path(agent.get("box"), "agents[{}].box".format(index)),
                "capabilities": _string_list(
                    agent.get("capabilities"),
                    "agents[{}].capabilities".format(index),
                    allow_empty=False,
                ),
                "adapter": {
                    "kind": "process",
                    "argv": argv,
                    "timeout_seconds": timeout_seconds,
                },
            }
        )
    _unique((agent["id"] for agent in normalized_agents), "agent ids")
    _unique((agent["box"] for agent in normalized_agents), "agent boxes")

    tasks = root.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RunbookError("tasks must contain at least one task")
    normalized_tasks = []
    seen_task_ids = set()
    for task_index, task_value in enumerate(tasks):
        task = _object(task_value, "tasks[{}]".format(task_index))
        task_id = _identifier(task.get("id"), "tasks[{}].id".format(task_index))
        if task_id in seen_task_ids:
            raise RunbookError("task ids must be unique")
        dependencies = _string_list(task.get("depends_on", []), "tasks[{}].depends_on".format(task_index))
        unknown_dependencies = [dependency for dependency in dependencies if dependency not in seen_task_ids]
        if unknown_dependencies:
            raise RunbookError(
                "task {} depends on tasks that must be declared earlier: {}".format(
                    task_id, ", ".join(unknown_dependencies)
                )
            )
        seen_task_ids.add(task_id)

        required_evidence = _string_list(
            task.get("required_evidence", []),
            "tasks[{}].required_evidence".format(task_index),
            allow_empty=False,
        )
        unknown_evidence = sorted(set(required_evidence) - EVIDENCE_KINDS)
        if unknown_evidence:
            raise RunbookError("task {} has unknown evidence kinds: {}".format(task_id, ", ".join(unknown_evidence)))

        steps = task.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RunbookError("task {} must define at least one step".format(task_id))
        normalized_steps = []
        for step_index, step_value in enumerate(steps):
            step = _object(step_value, "task {} step {}".format(task_id, step_index))
            commands = step.get("commands", [])
            if not isinstance(commands, list):
                raise RunbookError("task {} step commands must be an array".format(task_id))
            normalized_commands = []
            for command_index, command_value in enumerate(commands):
                command = _object(command_value, "task {} command {}".format(task_id, command_index))
                normalized_commands.append(
                    {
                        "purpose": _string(command.get("purpose"), "command purpose"),
                        "argv": _string_list(command.get("argv"), "command argv", allow_empty=False),
                    }
                )
            normalized_steps.append(
                {
                    "id": _identifier(step.get("id"), "task {} step id".format(task_id)),
                    "instruction": _string(step.get("instruction"), "task {} step instruction".format(task_id)),
                    "commands": normalized_commands,
                    "completion": _string_list(
                        step.get("completion", []),
                        "task {} step completion".format(task_id),
                        allow_empty=False,
                    ),
                }
            )
        _unique((step["id"] for step in normalized_steps), "step ids for task {}".format(task_id))

        verification = task.get("verification", [])
        if not isinstance(verification, list) or not verification:
            raise RunbookError("task {} must define at least one verification command".format(task_id))
        normalized_verification = []
        for command_index, command_value in enumerate(verification):
            command = _object(command_value, "task {} verification {}".format(task_id, command_index))
            normalized_verification.append(
                {
                    "purpose": _string(command.get("purpose"), "verification purpose"),
                    "argv": _string_list(command.get("argv"), "verification argv", allow_empty=False),
                }
            )

        max_attempts = task.get("max_attempts", 3)
        if not isinstance(max_attempts, int) or max_attempts <= 0:
            raise RunbookError("task {} max_attempts must be positive".format(task_id))
        normalized_tasks.append(
            {
                "id": task_id,
                "goal": _string(task.get("goal"), "task {} goal".format(task_id)),
                "depends_on": dependencies,
                "capabilities": _string_list(
                    task.get("capabilities", []),
                    "task {} capabilities".format(task_id),
                    allow_empty=False,
                ),
                "acceptance": _string_list(
                    task.get("acceptance", []),
                    "task {} acceptance".format(task_id),
                    allow_empty=False,
                ),
                "required_evidence": required_evidence,
                "max_attempts": max_attempts,
                "steps": normalized_steps,
                "verification": normalized_verification,
            }
        )

    normalized = {
        "schema_version": 1,
        "run": {
            "id": run_id,
            "objective": objective,
            "max_agents": 3,
            "completion": completion,
            "token_policy": {
                "max_tokens_per_turn": token_policy["max_tokens_per_turn"],
                "checkpoint_reserve": checkpoint_reserve,
                "max_total_tokens": token_policy["max_total_tokens"],
                "max_turns_per_task": token_policy["max_turns_per_task"],
            },
        },
        "rules": normalized_rules,
        "agents": normalized_agents,
        "tasks": normalized_tasks,
    }
    return normalized


def load_runbook(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return validate_runbook(json.load(handle))


def runbook_digest(runbook: Dict[str, Any]) -> str:
    canonical = json.dumps(runbook, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
