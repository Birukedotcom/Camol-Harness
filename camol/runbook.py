"""Loading, normalization, and validation for executable JSON runbooks.

Four schema versions are readable:

* ``schema_version: 1`` is the pre-M0 contract. Its normalization is unchanged so
  every previously frozen plan digest still reproduces byte-for-byte, including
  the legacy ``run.max_agents`` alias.
* ``schema_version: 2`` is the M0 contract. It removes the legacy alias, rejects
  unknown fields at every object level, rejects booleans in integer fields, and
  adds explicit ``run.readiness_policy`` and per-agent ``trust_tier`` fields.
  Those fields are contract data only: the M0 scheduler records them in the
  frozen plan but does not yet enforce them. There is deliberately no field that
  disables readiness proof; READY_TO_LEASE is not plan-configurable.
* ``schema_version: 3`` adds provider-neutral adapter profile references.  A
  process adapter keeps its v2 shape; a hosted adapter names a versioned profile
  whose digest is subsequently bound by readiness evidence.
* ``schema_version: 4`` adds an explicit per-task ``evaluator_assets`` list.
  Existing files below those paths are frozen outside builder authority and a
  candidate that changes one is rejected before evaluator execution.

A v1 document is never reinterpreted as v2 implicitly. Use
:func:`migrate_runbook_v1_to_v2` with explicit values for every new field.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .events import EVIDENCE_KINDS
from .readiness import TRUST_TIERS
from .schema import canonical_digest


class RunbookError(ValueError):
    pass


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4)
LATEST_SCHEMA_VERSION = 4

_ROOT_FIELDS_V2 = ("schema_version", "run", "rules", "agents", "tasks")
_RUN_FIELDS_V2 = ("id", "objective", "max_concurrency", "completion", "token_policy", "readiness_policy")
_TOKEN_POLICY_FIELDS = ("max_tokens_per_turn", "checkpoint_reserve", "max_total_tokens", "max_turns_per_task")
_READINESS_POLICY_FIELDS = ("receipt_ttl_seconds",)
_RULE_FIELDS = ("id", "text", "enforcement")
_AGENT_FIELDS_V2 = ("id", "role", "box", "capabilities", "adapter", "trust_tier")
_ADAPTER_FIELDS = ("kind", "argv", "timeout_seconds")
_ADAPTER_FIELDS_V3 = ("kind", "argv", "profile", "profile_snapshot", "timeout_seconds")
_TASK_FIELDS = (
    "id",
    "goal",
    "depends_on",
    "capabilities",
    "acceptance",
    "required_evidence",
    "max_attempts",
    "steps",
    "verification",
)
_TASK_FIELDS_V4 = _TASK_FIELDS + ("evaluator_assets",)
_STEP_FIELDS = ("id", "instruction", "commands", "completion")
_COMMAND_FIELDS = ("purpose", "argv")
_VERIFICATION_COMMAND_FIELDS_V4 = _COMMAND_FIELDS + ("cwd",)


def _reject_unknown(payload: Dict[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise RunbookError("{} has unknown fields: {}".format(label, ", ".join(unknown)))


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
    """Validate and normalize a runbook of any supported schema version.

    The returned document keeps the input's ``schema_version``; validation never
    upgrades a v1 document to v2.
    """
    root = _object(raw, "runbook")
    version = root.get("schema_version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RunbookError(
            "unsupported schema_version {!r}; supported versions: {}".format(
                version, ", ".join(str(item) for item in SUPPORTED_SCHEMA_VERSIONS)
            )
        )
    return _validate(root, version=version)


def _strict_int(value: Any, label: str, *, minimum: int) -> int:
    """Schema v2 integer: rejects bool (a subclass of int) and values below ``minimum``."""
    if type(value) is not int or value < minimum:
        kind = "positive" if minimum > 0 else "non-negative"
        raise RunbookError("{} must be a {} integer".format(label, kind))
    return value


def _validate_readiness_policy(value: Any) -> Dict[str, Any]:
    policy = _object(value, "run.readiness_policy")
    if "require_readiness_receipt" in policy:
        raise RunbookError(
            "run.readiness_policy.require_readiness_receipt is not a supported field: "
            "readiness proof cannot be disabled by a plan"
        )
    _reject_unknown(policy, _READINESS_POLICY_FIELDS, "run.readiness_policy")
    ttl = _strict_int(policy.get("receipt_ttl_seconds"), "run.readiness_policy.receipt_ttl_seconds", minimum=1)
    return {"receipt_ttl_seconds": ttl}


def _validate(root: Dict[str, Any], *, version: int) -> Dict[str, Any]:
    strict = version >= 2
    if strict:
        _reject_unknown(root, _ROOT_FIELDS_V2, "runbook")

    run = _object(root.get("run"), "run")
    if strict:
        if "max_agents" in run:
            raise RunbookError("run.max_agents is a schema v1 alias; schema v2 requires run.max_concurrency")
        _reject_unknown(run, _RUN_FIELDS_V2, "run")
    run_id = _identifier(run.get("id"), "run.id")
    objective = _string(run.get("objective"), "run.objective")
    if strict:
        concurrency_field = "max_concurrency"
    else:
        if (
            "max_concurrency" in run
            and "max_agents" in run
            and run["max_concurrency"] != run["max_agents"]
        ):
            raise RunbookError("run.max_concurrency conflicts with legacy run.max_agents")
        concurrency_field = "max_concurrency" if "max_concurrency" in run else "max_agents"
    max_concurrency = run.get(concurrency_field)
    if (
        not isinstance(max_concurrency, int)
        or isinstance(max_concurrency, bool)
        or max_concurrency <= 0
    ):
        raise RunbookError("run.max_concurrency must be a positive integer")

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
    if strict:
        _reject_unknown(token_policy, _TOKEN_POLICY_FIELDS, "run.token_policy")
    for field in ("max_tokens_per_turn", "max_total_tokens", "max_turns_per_task"):
        value = token_policy.get(field)
        if strict:
            _strict_int(value, "run.token_policy.{}".format(field), minimum=1)
        elif not isinstance(value, int) or value <= 0:
            raise RunbookError("run.token_policy.{} must be a positive integer".format(field))
    checkpoint_reserve = token_policy.get("checkpoint_reserve", 0)
    if strict:
        _strict_int(checkpoint_reserve, "run.token_policy.checkpoint_reserve", minimum=0)
    elif not isinstance(checkpoint_reserve, int) or checkpoint_reserve < 0:
        raise RunbookError("run.token_policy.checkpoint_reserve must be a non-negative integer")
    if checkpoint_reserve >= token_policy["max_tokens_per_turn"]:
        raise RunbookError("checkpoint_reserve must be smaller than max_tokens_per_turn")
    readiness_policy = None
    if strict:
        if "readiness_policy" not in run:
            raise RunbookError("run.readiness_policy is required in schema v2")
        readiness_policy = _validate_readiness_policy(run["readiness_policy"])
    elif "readiness_policy" in run:
        raise RunbookError("run.readiness_policy is a schema v2 field; migrate the runbook to schema_version 2")

    rules = root.get("rules", [])
    if not isinstance(rules, list):
        raise RunbookError("rules must be an array")
    normalized_rules = []
    for index, rule_value in enumerate(rules):
        rule = _object(rule_value, "rules[{}]".format(index))
        if strict:
            _reject_unknown(rule, _RULE_FIELDS, "rules[{}]".format(index))
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
    if not isinstance(agents, list) or not agents:
        raise RunbookError("agents must define at least one registered worker")
    if max_concurrency > len(agents):
        raise RunbookError("run.max_concurrency cannot exceed registered workers")
    normalized_agents = []
    for index, agent_value in enumerate(agents):
        agent = _object(agent_value, "agents[{}]".format(index))
        if strict:
            _reject_unknown(agent, _AGENT_FIELDS_V2, "agents[{}]".format(index))
        elif "trust_tier" in agent:
            raise RunbookError(
                "agents[{}].trust_tier is a schema v2 field; migrate the runbook to schema_version 2".format(index)
            )
        adapter = _object(agent.get("adapter"), "agents[{}].adapter".format(index))
        if strict:
            _reject_unknown(
                adapter,
                _ADAPTER_FIELDS_V3 if version >= 3 else _ADAPTER_FIELDS,
                "agents[{}].adapter".format(index),
            )
        kind = adapter.get("kind")
        if kind not in ({"process", "claude_cli"} if version >= 3 else {"process"}):
            raise RunbookError(
                "agents[{}].adapter.kind must be {}".format(
                    index, "process or claude_cli" if version >= 3 else "process"
                )
            )
        argv = None
        profile = None
        if kind == "process":
            if strict and "profile_snapshot" in adapter:
                raise RunbookError("agents[{}].adapter.profile_snapshot is only valid for hosted adapters".format(index))
            if version >= 3 and "profile" in adapter:
                raise RunbookError("agents[{}].adapter.profile is only valid for hosted adapters".format(index))
            argv = _string_list(adapter.get("argv"), "agents[{}].adapter.argv".format(index), allow_empty=False)
        else:
            if "argv" in adapter:
                raise RunbookError("agents[{}].adapter.argv is not accepted for claude_cli; the profile owns invocation policy".format(index))
            profile = _relative_path(adapter.get("profile"), "agents[{}].adapter.profile".format(index))
            profile_snapshot = None
            if "profile_snapshot" in adapter:
                try:
                    from .providers import ModelProfile
                    profile_snapshot = ModelProfile.from_dict(adapter["profile_snapshot"]).to_dict()
                except Exception as error:
                    raise RunbookError(
                        "agents[{}].adapter.profile_snapshot is invalid: {}".format(index, error)
                    ) from error
                if profile_snapshot["adapter_kind"] != kind:
                    raise RunbookError("agents[{}].adapter.profile_snapshot kind does not match".format(index))
        timeout_seconds = adapter.get("timeout_seconds", 1800)
        if strict:
            _strict_int(timeout_seconds, "agents[{}].adapter.timeout_seconds".format(index), minimum=1)
        elif not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise RunbookError("agents[{}].adapter.timeout_seconds must be positive".format(index))
        normalized_agent = {
                "id": _identifier(agent.get("id"), "agents[{}].id".format(index)),
                "role": _string(agent.get("role"), "agents[{}].role".format(index)),
                "box": _relative_path(agent.get("box"), "agents[{}].box".format(index)),
                "capabilities": _string_list(
                    agent.get("capabilities"),
                    "agents[{}].capabilities".format(index),
                    allow_empty=False,
                ),
                "adapter": dict(
                    {"kind": kind, "timeout_seconds": timeout_seconds},
                    **(
                        {"argv": argv}
                        if kind == "process"
                        else dict(
                            {"profile": profile},
                            **({"profile_snapshot": profile_snapshot} if profile_snapshot is not None else {})
                        )
                    )
                ),
        }
        if strict:
            trust_tier = agent.get("trust_tier")
            if trust_tier not in TRUST_TIERS:
                raise RunbookError(
                    "agents[{}].trust_tier must be one of: {}".format(index, ", ".join(sorted(TRUST_TIERS)))
                )
            normalized_agent["trust_tier"] = trust_tier
        normalized_agents.append(normalized_agent)
    _unique((agent["id"] for agent in normalized_agents), "agent ids")
    _unique((agent["box"] for agent in normalized_agents), "agent boxes")

    tasks = root.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RunbookError("tasks must contain at least one task")
    normalized_tasks = []
    seen_task_ids = set()
    for task_index, task_value in enumerate(tasks):
        task = _object(task_value, "tasks[{}]".format(task_index))
        if strict:
            _reject_unknown(
                task, _TASK_FIELDS_V4 if version >= 4 else _TASK_FIELDS,
                "tasks[{}]".format(task_index),
            )
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
            if strict:
                _reject_unknown(step, _STEP_FIELDS, "task {} step {}".format(task_id, step_index))
            commands = step.get("commands", [])
            if not isinstance(commands, list):
                raise RunbookError("task {} step commands must be an array".format(task_id))
            normalized_commands = []
            for command_index, command_value in enumerate(commands):
                command = _object(command_value, "task {} command {}".format(task_id, command_index))
                if strict:
                    _reject_unknown(command, _COMMAND_FIELDS, "task {} command {}".format(task_id, command_index))
                normalized_command = {
                    "purpose": _string(command.get("purpose"), "command purpose"),
                    "argv": _string_list(command.get("argv"), "command argv", allow_empty=False),
                }
                normalized_commands.append(normalized_command)
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
            if strict:
                _reject_unknown(
                    command, _VERIFICATION_COMMAND_FIELDS_V4 if version >= 4 else _COMMAND_FIELDS,
                    "task {} verification {}".format(task_id, command_index),
                )
            normalized_command = {
                "purpose": _string(command.get("purpose"), "verification purpose"),
                "argv": _string_list(command.get("argv"), "verification argv", allow_empty=False),
            }
            if "cwd" in command:
                if version < 4 or not isinstance(command["cwd"], str) or command["cwd"] not in {"box", "workspace_root"}:
                    raise RunbookError("verification cwd must be box or workspace_root in schema v4")
                normalized_command["cwd"] = command["cwd"]
            normalized_verification.append(normalized_command)

        max_attempts = task.get("max_attempts", 3)
        if strict:
            _strict_int(max_attempts, "task {} max_attempts".format(task_id), minimum=1)
        elif not isinstance(max_attempts, int) or max_attempts <= 0:
            raise RunbookError("task {} max_attempts must be positive".format(task_id))
        normalized_task = {
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
        if version >= 4:
            if "evaluator_assets" not in task:
                raise RunbookError("task {} evaluator_assets is required in schema v4".format(task_id))
            assets = [
                _relative_path(item, "task {} evaluator asset".format(task_id))
                for item in _string_list(task["evaluator_assets"], "task {} evaluator_assets".format(task_id))
            ]
            _unique(assets, "evaluator assets for task {}".format(task_id))
            normalized_task["evaluator_assets"] = assets
        normalized_tasks.append(normalized_task)

    normalized_run = {
        "id": run_id,
        "objective": objective,
        concurrency_field: max_concurrency,
        "completion": completion,
        "token_policy": {
            "max_tokens_per_turn": token_policy["max_tokens_per_turn"],
            "checkpoint_reserve": checkpoint_reserve,
            "max_total_tokens": token_policy["max_total_tokens"],
            "max_turns_per_task": token_policy["max_turns_per_task"],
        },
    }
    if readiness_policy is not None:
        normalized_run["readiness_policy"] = readiness_policy
    normalized = {
        "schema_version": version,
        "run": normalized_run,
        "rules": normalized_rules,
        "agents": normalized_agents,
        "tasks": normalized_tasks,
    }
    return normalized


def migrate_runbook_v1_to_v2(
    runbook: Dict[str, Any],
    *,
    readiness_policy: Dict[str, Any],
    trust_tiers: Dict[str, str],
) -> Dict[str, Any]:
    """Explicitly migrate a schema v1 runbook (raw or normalized) to schema v2.

    Nothing is defaulted: the caller must supply the readiness policy and a
    trust tier for every registered agent. ``max_agents`` becomes
    ``max_concurrency`` with the same value. The result is re-validated as v2,
    and the input is never mutated. The v2 digest is necessarily different from
    the v1 digest because the plan now contains more frozen decisions.
    """
    source = validate_runbook(runbook)
    if source["schema_version"] != 1:
        raise RunbookError("migrate_runbook_v1_to_v2 requires a schema_version 1 runbook")
    if not isinstance(trust_tiers, dict):
        raise RunbookError("trust_tiers must map every agent id to a trust tier")
    agent_ids = [agent["id"] for agent in source["agents"]]
    missing = sorted(set(agent_ids) - set(trust_tiers))
    extra = sorted(set(trust_tiers) - set(agent_ids))
    if missing:
        raise RunbookError("trust_tiers is missing agents: {}".format(", ".join(missing)))
    if extra:
        raise RunbookError("trust_tiers names unknown agents: {}".format(", ".join(extra)))

    migrated = copy.deepcopy(source)
    migrated["schema_version"] = 2
    run = migrated["run"]
    if "max_agents" in run:
        run["max_concurrency"] = run.pop("max_agents")
    run["readiness_policy"] = copy.deepcopy(readiness_policy)
    for agent in migrated["agents"]:
        agent["trust_tier"] = trust_tiers[agent["id"]]
    return validate_runbook(migrated)


def migrate_runbook_v2_to_v3(runbook: Dict[str, Any]) -> Dict[str, Any]:
    """Explicitly migrate a normalized or raw v2 plan to the v3 adapter schema.

    Existing process adapters need no new choices, but the version change is
    still explicit so a frozen v2 digest is never silently reinterpreted.
    """
    source = validate_runbook(runbook)
    if source["schema_version"] != 2:
        raise RunbookError("migrate_runbook_v2_to_v3 requires a schema_version 2 runbook")
    migrated = copy.deepcopy(source)
    migrated["schema_version"] = 3
    return validate_runbook(migrated)


def migrate_runbook_v3_to_v4(
    runbook: Dict[str, Any], *, evaluator_assets: Dict[str, List[str]]
) -> Dict[str, Any]:
    """Explicitly add the protected evaluator-asset decision for every task."""
    source = validate_runbook(runbook)
    if source["schema_version"] != 3:
        raise RunbookError("migrate_runbook_v3_to_v4 requires a schema_version 3 runbook")
    if not isinstance(evaluator_assets, dict):
        raise RunbookError("evaluator_assets must map every task id to an array")
    task_ids = {task["id"] for task in source["tasks"]}
    missing = sorted(task_ids - set(evaluator_assets))
    extra = sorted(set(evaluator_assets) - task_ids)
    if missing or extra:
        raise RunbookError("evaluator_assets task mismatch (missing={}, extra={})".format(missing, extra))
    migrated = copy.deepcopy(source)
    migrated["schema_version"] = 4
    for task in migrated["tasks"]:
        task["evaluator_assets"] = copy.deepcopy(evaluator_assets[task["id"]])
    return validate_runbook(migrated)


def load_runbook(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return validate_runbook(json.load(handle))


def runbook_digest(runbook: Dict[str, Any]) -> str:
    """Canonical digest of a normalized runbook.

    Delegates to :func:`camol.schema.canonical_digest`, whose byte layout equals
    the pre-M0 ``json.dumps(sort_keys=True, separators=(",", ":"))`` formula, so
    existing frozen plan digests are unchanged (pinned in ``tests/test_runbook.py``).
    """
    return canonical_digest(runbook)
