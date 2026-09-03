"""Human-transparent `/grill` state and schema-v4 plan compilation."""

import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .runbook import validate_runbook
from .schema import canonical_digest


class PlanningError(RuntimeError):
    """A grill answer or plan proposal is invalid."""


GRILL_SCHEMA = "camol.grill"
GRILL_VERSION = 1
QUESTIONS: Tuple[Tuple[str, str], ...] = (
    ("outcome", "What observable outcome would make this finished?"),
    ("exclusions", "What must this work not change or attempt?"),
    ("invariants", "Which properties must remain true at every step?"),
    (
        "topology",
        "Break the work into task lines as `id | goal | after=id,id` (use one line if it should not split).",
    ),
    ("verification", "What exact command should independently verify the result?"),
    (
        "resources",
        "What limits should apply? You can specify `boxes=N`, `turns=N`, `tokens=N`; also name time/provider constraints.",
    ),
)


def _required_text(value: Any, label: str, limit: int = 12_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningError("{} must be non-empty".format(label))
    text = value.strip()
    if len(text) > limit:
        raise PlanningError("{} is too large".format(label))
    return text


def _items(text: str) -> List[str]:
    values = [item.strip(" \t-*0123456789.") for item in re.split(r"[\n;]+", text)]
    return [item for item in values if item]


def _verification_argv(text: str) -> List[str]:
    try:
        argv = shlex.split(text)
    except ValueError as error:
        raise PlanningError("verification command has unmatched quoting") from error
    if not argv:
        raise PlanningError("verification command must not be empty")
    forbidden = {"|", "||", "&&", ";", ">", ">>", "<", "2>", "&"}
    if any(item in forbidden for item in argv):
        raise PlanningError("verification command must be argv, not a shell pipeline")
    return argv


def _task_lines(text: str, acceptance: Sequence[str]) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    for index, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip(" \t-*")
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 1:
            task_id = "task-{}".format(index)
            goal = parts[0]
            depends_on: List[str] = []
        elif len(parts) in {2, 3}:
            task_id, goal = parts[:2]
            depends_on = []
            if len(parts) == 3:
                if not parts[2].startswith("after="):
                    raise PlanningError("task dependency must use after=id,id")
                depends_on = [item.strip() for item in parts[2][len("after="):].split(",") if item.strip()]
        else:
            raise PlanningError("each task line must have id | goal | after=id,id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
            raise PlanningError("task id {!r} is invalid".format(task_id))
        _required_text(goal, "task goal", limit=2_000)
        tasks.append({
            "id": task_id,
            "goal": goal,
            "depends_on": depends_on,
            "acceptance": list(acceptance),
        })
    if not tasks:
        raise PlanningError("topology must contain at least one task")
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise PlanningError("task ids must be unique")
    for task in tasks:
        unknown = sorted(set(task["depends_on"]) - set(ids))
        if unknown:
            raise PlanningError("task {} has unknown dependencies: {}".format(task["id"], ", ".join(unknown)))
        if task["id"] in task["depends_on"]:
            raise PlanningError("task {} cannot depend on itself".format(task["id"]))
    # A small DFS rejects cycles before the kernel sees the proposal.
    dependencies = {task["id"]: task["depends_on"] for task in tasks}
    visiting = set()
    visited = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanningError("task dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)
    return tasks


def _resource_limit(text: str, name: str, default: int, *, minimum: int, maximum: int) -> int:
    match = re.search(r"(?:^|[\s,;]){}\s*=\s*(\d+)(?:$|[\s,;])".format(re.escape(name)), text, re.I)
    value = int(match.group(1)) if match else default
    if not minimum <= value <= maximum:
        raise PlanningError("{} must be between {} and {}".format(name, minimum, maximum))
    return value


def validate_proposal(value: Mapping[str, Any]) -> Dict[str, Any]:
    fields = {
        "schema", "schema_version", "goal", "outcomes", "exclusions", "invariants",
        "verification_argv", "resource_statement", "resource_limits", "execution", "tasks", "maturity",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise PlanningError("plan proposal has the wrong fields")
    if value["schema"] != "camol.plan_proposal" or value["schema_version"] != 1:
        raise PlanningError("plan proposal schema is unsupported")
    for name in ("goal", "resource_statement", "maturity"):
        _required_text(value[name], "plan " + name)
    for name in ("outcomes", "exclusions", "invariants", "verification_argv"):
        if not isinstance(value[name], list) or not value[name] or any(not isinstance(item, str) or not item for item in value[name]):
            raise PlanningError("plan {} must contain non-empty strings".format(name))
    limits = value["resource_limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_concurrency", "max_turns_per_task", "max_total_tokens"}:
        raise PlanningError("plan resource_limits has the wrong fields")
    for name, number in limits.items():
        if type(number) is not int or number <= 0:
            raise PlanningError("plan resource limit {} must be positive".format(name))
    execution = value["execution"]
    if not isinstance(execution, dict) or set(execution) != {"model", "effort"}:
        raise PlanningError("plan execution has the wrong fields")
    _required_text(execution["model"], "plan execution model")
    if execution["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise PlanningError("plan execution effort is unsupported")
    if not isinstance(value["tasks"], list):
        raise PlanningError("plan tasks must be an array")
    # Reuse the topology validator's graph checks without accepting alternate shapes.
    ids = []
    dependencies = {}
    normalized_tasks = []
    for task in value["tasks"]:
        if not isinstance(task, dict) or set(task) != {"id", "goal", "depends_on", "acceptance"}:
            raise PlanningError("plan task has the wrong fields")
        task_id = _required_text(task["id"], "task id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
            raise PlanningError("task id is invalid")
        if not isinstance(task["depends_on"], list) or any(not isinstance(item, str) for item in task["depends_on"]):
            raise PlanningError("task dependencies must be strings")
        if not isinstance(task["acceptance"], list) or not task["acceptance"] or any(not isinstance(item, str) or not item for item in task["acceptance"]):
            raise PlanningError("task acceptance must contain non-empty strings")
        ids.append(task_id)
        dependencies[task_id] = list(task["depends_on"])
        normalized_tasks.append(dict(task))
    if not ids or len(ids) != len(set(ids)):
        raise PlanningError("plan must have unique tasks")
    visiting = set()
    visited = set()
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanningError("task dependency graph contains a cycle")
        if task_id in visited:
            return
        if task_id not in dependencies:
            raise PlanningError("task dependency is unknown")
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in ids:
        visit(task_id)
    normalized = dict(value)
    normalized["tasks"] = normalized_tasks
    normalized["resource_limits"] = dict(limits)
    return normalized


@dataclass(frozen=True)
class GrillState:
    goal: str
    answers: Mapping[str, str]
    question_index: int
    status: str = "questioning"

    def __post_init__(self) -> None:
        _required_text(self.goal, "grill goal")
        if self.status not in {"questioning", "complete"}:
            raise PlanningError("grill status is invalid")
        if type(self.question_index) is not int or not 0 <= self.question_index <= len(QUESTIONS):
            raise PlanningError("grill question index is invalid")
        expected = {name for name, _ in QUESTIONS[: self.question_index]}
        if set(self.answers) != expected:
            raise PlanningError("grill answers do not match its question position")
        for name, value in self.answers.items():
            _required_text(value, "grill answer " + name)
        if (self.question_index == len(QUESTIONS)) != (self.status == "complete"):
            raise PlanningError("grill terminal state is inconsistent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": GRILL_SCHEMA,
            "schema_version": GRILL_VERSION,
            "goal": self.goal,
            "answers": dict(self.answers),
            "question_index": self.question_index,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GrillState":
        if not isinstance(value, dict) or set(value) != {
            "schema", "schema_version", "goal", "answers", "question_index", "status"
        }:
            raise PlanningError("grill record has the wrong fields")
        if value["schema"] != GRILL_SCHEMA or value["schema_version"] != GRILL_VERSION:
            raise PlanningError("grill record has an unsupported schema")
        if not isinstance(value["answers"], dict):
            raise PlanningError("grill answers must be an object")
        return cls(
            goal=value["goal"], answers=dict(value["answers"]),
            question_index=value["question_index"], status=value["status"],
        )

    @classmethod
    def start(cls, goal: str) -> "GrillState":
        return cls(goal=_required_text(goal, "grill goal"), answers={}, question_index=0)

    def question(self) -> Optional[str]:
        return QUESTIONS[self.question_index][1] if self.question_index < len(QUESTIONS) else None

    def answer(self, text: str) -> "GrillState":
        if self.status != "questioning":
            raise PlanningError("grill is already complete")
        answer = _required_text(text, "grill answer")
        name = QUESTIONS[self.question_index][0]
        answers = dict(self.answers)
        answers[name] = answer
        position = self.question_index + 1
        return GrillState(
            goal=self.goal,
            answers=answers,
            question_index=position,
            status="complete" if position == len(QUESTIONS) else "questioning",
        )


def proposal_from_grill(grill: GrillState, *, model: str = "manual", effort: str = "high") -> Dict[str, Any]:
    if grill.status != "complete":
        raise PlanningError("finish every grill question before proposing a plan")
    answers = dict(grill.answers)
    invariants = [
        "No work begins before the exact plan digest is approved.",
        "The source checkout is not modified directly by a worker.",
        "Completion requires independent evaluator evidence.",
    ] + _items(answers["invariants"])
    outcomes = _items(answers["outcome"])
    tasks = _task_lines(answers["topology"], outcomes)
    resource_statement = answers["resources"]
    proposal = {
        "schema": "camol.plan_proposal",
        "schema_version": 1,
        "goal": grill.goal,
        "outcomes": outcomes,
        "exclusions": _items(answers["exclusions"]),
        "invariants": list(dict.fromkeys(invariants)),
        "verification_argv": _verification_argv(answers["verification"]),
        "resource_statement": resource_statement,
        "resource_limits": {
            "max_concurrency": min(
                len(tasks), _resource_limit(resource_statement, "boxes", len(tasks), minimum=1, maximum=64)
            ),
            "max_turns_per_task": _resource_limit(resource_statement, "turns", 6, minimum=1, maximum=100),
            "max_total_tokens": _resource_limit(resource_statement, "tokens", 48_000, minimum=1_000, maximum=10_000_000),
        },
        "execution": {"model": _required_text(model, "execution model"), "effort": effort},
        "tasks": tasks,
        "maturity": "PROPOSED_MANUAL_GRILL",
    }
    if not proposal["outcomes"] or not proposal["exclusions"]:
        raise PlanningError("outcome and exclusion answers must contain at least one item")
    return validate_proposal(proposal)


def compile_runbook(
    proposal: Mapping[str, Any], *, run_id: str, adapter: Mapping[str, Any],
    worker_id: str = "builder", model_capabilities: Sequence[str] = ("inspect", "code", "test"),
) -> Dict[str, Any]:
    """Compile a reviewed proposal to the kernel's current executable contract."""
    proposal = validate_proposal(proposal)
    rules = [
        {"id": "approval-gate", "text": proposal["invariants"][0], "enforcement": "hard"},
        {"id": "source-isolation", "text": proposal["invariants"][1], "enforcement": "hard"},
        {"id": "evidence-gate", "text": proposal["invariants"][2], "enforcement": "hard"},
    ]
    for index, invariant in enumerate(proposal["invariants"][3:], 1):
        rules.append({"id": "human-invariant-{}".format(index), "text": invariant, "enforcement": "hard"})
    runbook = {
        "schema_version": 4,
        "run": {
            "id": run_id,
            "objective": proposal["goal"],
            "max_concurrency": proposal["resource_limits"]["max_concurrency"],
            "completion": [
                "all_tasks_succeeded", "all_required_evidence_present",
                "all_verifications_green", "no_open_blockers", "no_open_debug_cases",
            ],
            "token_policy": {
                "max_tokens_per_turn": 8_000,
                "checkpoint_reserve": 600,
                "max_total_tokens": proposal["resource_limits"]["max_total_tokens"],
                "max_turns_per_task": proposal["resource_limits"]["max_turns_per_task"],
            },
            "readiness_policy": {"receipt_ttl_seconds": 300},
        },
        "rules": rules,
        "agents": [
            {
                "id": worker_id if index == 0 else "{}-{}".format(worker_id, index + 1),
                "role": "Implement and evidence the approved task",
                "box": "camol-boxes/{}".format(worker_id if index == 0 else "{}-{}".format(worker_id, index + 1)),
                "capabilities": list(model_capabilities),
                "adapter": dict(adapter),
                "trust_tier": "developer_trusted",
            }
            for index in range(proposal["resource_limits"]["max_concurrency"])
        ],
        "tasks": [
            {
                "id": task["id"],
                "goal": task["goal"],
                "depends_on": list(task["depends_on"]),
                "capabilities": list(model_capabilities),
                "acceptance": list(task["acceptance"]),
                "required_evidence": ["command", "artifact", "claim", "test_result"],
                "max_attempts": 3,
                "steps": [
                    {
                        "id": "build",
                        "instruction": task["goal"],
                        "commands": [],
                        "completion": list(task["acceptance"]),
                    }
                ],
                "verification": [
                    {"purpose": "Run the human-selected evaluator", "argv": list(proposal["verification_argv"])}
                ],
                "evaluator_assets": [],
            }
            for task in proposal["tasks"]
        ],
    }
    return validate_runbook(runbook)


def proposal_digest(proposal: Mapping[str, Any]) -> str:
    return canonical_digest(proposal)
