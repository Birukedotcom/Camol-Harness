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
    ("verification", "What exact command should independently verify the result?"),
    ("resources", "What box, token, time, and provider limits should the plan respect?"),
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


def proposal_from_grill(grill: GrillState) -> Dict[str, Any]:
    if grill.status != "complete":
        raise PlanningError("finish every grill question before proposing a plan")
    answers = dict(grill.answers)
    invariants = [
        "No work begins before the exact plan digest is approved.",
        "The source checkout is not modified directly by a worker.",
        "Completion requires independent evaluator evidence.",
    ] + _items(answers["invariants"])
    proposal = {
        "schema": "camol.plan_proposal",
        "schema_version": 1,
        "goal": grill.goal,
        "outcomes": _items(answers["outcome"]),
        "exclusions": _items(answers["exclusions"]),
        "invariants": list(dict.fromkeys(invariants)),
        "verification_argv": _verification_argv(answers["verification"]),
        "resource_statement": answers["resources"],
        "tasks": [
            {
                "id": "implement",
                "goal": grill.goal,
                "depends_on": [],
                "acceptance": _items(answers["outcome"]),
            }
        ],
        "maturity": "PROPOSED_MANUAL_GRILL",
    }
    if not proposal["outcomes"] or not proposal["exclusions"]:
        raise PlanningError("outcome and exclusion answers must contain at least one item")
    return proposal


def compile_runbook(
    proposal: Mapping[str, Any], *, run_id: str, adapter: Mapping[str, Any],
    worker_id: str = "builder", model_capabilities: Sequence[str] = ("inspect", "code", "test"),
) -> Dict[str, Any]:
    """Compile a reviewed proposal to the kernel's current executable contract."""
    if proposal.get("schema") != "camol.plan_proposal" or proposal.get("schema_version") != 1:
        raise PlanningError("plan proposal schema is unsupported")
    task = proposal["tasks"][0]
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
            "max_concurrency": 1,
            "completion": [
                "all_tasks_succeeded", "all_required_evidence_present",
                "all_verifications_green", "no_open_blockers", "no_open_debug_cases",
            ],
            "token_policy": {
                "max_tokens_per_turn": 8_000,
                "checkpoint_reserve": 600,
                "max_total_tokens": 48_000,
                "max_turns_per_task": 6,
            },
            "readiness_policy": {"receipt_ttl_seconds": 300},
        },
        "rules": rules,
        "agents": [
            {
                "id": worker_id,
                "role": "Implement and evidence the approved task",
                "box": "camol-boxes/{}".format(worker_id),
                "capabilities": list(model_capabilities),
                "adapter": dict(adapter),
                "trust_tier": "developer_trusted",
            }
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
        ],
    }
    return validate_runbook(runbook)


def proposal_digest(proposal: Mapping[str, Any]) -> str:
    return canonical_digest(proposal)
