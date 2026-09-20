"""Matched, vector-valued benchmark trial contracts."""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from .schema import (
    reject_unknown_fields,
    require_bool,
    require_digest,
    require_identifier,
    require_non_negative_int,
    require_schema_header,
    require_string,
    require_timestamp,
)


class BenchmarkError(ValueError):
    """A trial is malformed or two arms are not actually comparable."""


ARMS = frozenset({"claude_direct", "camol_one", "camol_adaptive"})
OUTCOMES = frozenset({"accepted", "rejected", "error"})
MATCHED_FIELDS = (
    "task_id", "task_digest", "source_revision", "evaluator_digest",
    "model", "model_version", "effort", "tool_policy_digest", "budget_digest",
)


@dataclass(frozen=True)
class BenchmarkTrial:
    SCHEMA = "camol.benchmark_trial"
    SCHEMA_VERSION = 1
    FIELDS = (
        "schema", "schema_version", "trial_id", "arm", "task_id", "task_digest",
        "source_revision", "evaluator_digest", "model", "model_version", "effort",
        "tool_policy_digest", "budget_digest", "outcome", "accepted_behavior",
        "invariant_violations", "regressions", "tokens", "cost_usd_micros",
        "elapsed_ms", "retries", "human_interventions", "tool_calls",
        "recovery_success", "evidence_complete", "recorded_at",
    )

    trial_id: str
    arm: str
    task_id: str
    task_digest: str
    source_revision: str
    evaluator_digest: str
    model: str
    model_version: str
    effort: str
    tool_policy_digest: str
    budget_digest: str
    outcome: str
    accepted_behavior: int
    invariant_violations: int
    regressions: int
    tokens: int
    cost_usd_micros: int
    elapsed_ms: int
    retries: int
    human_interventions: int
    tool_calls: int
    recovery_success: bool
    evidence_complete: bool
    recorded_at: str

    def __post_init__(self) -> None:
        for name in ("trial_id", "task_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "benchmark " + name))
        if self.arm not in ARMS:
            raise BenchmarkError("benchmark arm is invalid")
        if self.outcome not in OUTCOMES:
            raise BenchmarkError("benchmark outcome is invalid")
        for name in ("task_digest", "evaluator_digest", "tool_policy_digest", "budget_digest"):
            object.__setattr__(self, name, require_digest(getattr(self, name), "benchmark " + name))
        for name in ("source_revision", "model", "model_version", "effort"):
            object.__setattr__(self, name, require_string(getattr(self, name), "benchmark " + name))
        for name in (
            "accepted_behavior", "invariant_violations", "regressions", "tokens",
            "cost_usd_micros", "elapsed_ms", "retries", "human_interventions", "tool_calls",
        ):
            object.__setattr__(self, name, require_non_negative_int(getattr(self, name), "benchmark " + name))
        object.__setattr__(self, "recovery_success", require_bool(self.recovery_success, "benchmark recovery_success"))
        object.__setattr__(self, "evidence_complete", require_bool(self.evidence_complete, "benchmark evidence_complete"))
        object.__setattr__(self, "recorded_at", require_timestamp(self.recorded_at, "benchmark recorded_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
            **{name: getattr(self, name) for name in self.FIELDS if name not in {"schema", "schema_version"}},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BenchmarkTrial":
        if not isinstance(payload, dict):
            raise BenchmarkError("benchmark trial must be an object")
        require_schema_header(payload, cls.SCHEMA, cls.SCHEMA_VERSION, "benchmark trial")
        reject_unknown_fields(payload, cls.FIELDS, "benchmark trial")
        if set(payload) != set(cls.FIELDS):
            raise BenchmarkError("benchmark trial is missing fields")
        return cls(**{key: payload[key] for key in cls.FIELDS if key not in {"schema", "schema_version"}})


def compare_trials(direct: BenchmarkTrial, camol: BenchmarkTrial) -> Dict[str, Any]:
    """Compare two matched arms without reducing safety and quality to one score."""
    if direct.arm != "claude_direct" or camol.arm not in {"camol_one", "camol_adaptive"}:
        raise BenchmarkError("comparison requires claude_direct and a Camol arm")
    mismatches = {
        name: {"direct": getattr(direct, name), "camol": getattr(camol, name)}
        for name in MATCHED_FIELDS if getattr(direct, name) != getattr(camol, name)
    }
    if mismatches:
        raise BenchmarkError("trials are not matched: {}".format(", ".join(sorted(mismatches))))
    metrics: Tuple[Tuple[str, int], ...] = (
        ("accepted_behavior", camol.accepted_behavior - direct.accepted_behavior),
        ("invariant_violations", camol.invariant_violations - direct.invariant_violations),
        ("regressions", camol.regressions - direct.regressions),
        ("tokens", camol.tokens - direct.tokens),
        ("cost_usd_micros", camol.cost_usd_micros - direct.cost_usd_micros),
        ("elapsed_ms", camol.elapsed_ms - direct.elapsed_ms),
        ("retries", camol.retries - direct.retries),
        ("human_interventions", camol.human_interventions - direct.human_interventions),
        ("tool_calls", camol.tool_calls - direct.tool_calls),
    )
    return {
        "schema": "camol.benchmark_comparison", "schema_version": 1,
        "direct_trial_id": direct.trial_id, "camol_trial_id": camol.trial_id,
        "camol_arm": camol.arm,
        "matched": {name: getattr(direct, name) for name in MATCHED_FIELDS},
        "outcomes": {"direct": direct.outcome, "camol": camol.outcome},
        "delta_camol_minus_direct": {name: value for name, value in metrics},
        "guardrails": {
            "direct_invariant_violations": direct.invariant_violations,
            "camol_invariant_violations": camol.invariant_violations,
            "direct_regressions": direct.regressions,
            "camol_regressions": camol.regressions,
        },
        "recovery": {"direct": direct.recovery_success, "camol": camol.recovery_success},
        "evidence_complete": {"direct": direct.evidence_complete, "camol": camol.evidence_complete},
        "trial_count_per_arm": 1,
        "statistical_claim": False,
        "note": "One matched pair is descriptive evidence only; it cannot establish significance.",
    }
