"""Vector-based hill-climb adjudication with explicit guardrails."""

import math
from typing import Any, Dict, List


def compare_vectors(
    baseline: Dict[str, float],
    candidate: Dict[str, float],
    dimensions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("measurement vectors must be objects")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("at least one measurement dimension is required")
    names = set()
    for dimension in dimensions:
        if not isinstance(dimension, dict) or not {"name", "direction"}.issubset(dimension) or set(dimension) - {"name", "direction", "minimum_improvement", "regression_tolerance"}:
            raise ValueError("measurement dimension has unsupported fields")
        name = dimension.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("measurement dimensions must have unique non-empty names")
        names.add(name)
        for label, value in (("baseline", baseline.get(name)), ("candidate", candidate.get(name)),
                             ("minimum_improvement", dimension.get("minimum_improvement", 0)),
                             ("regression_tolerance", dimension.get("regression_tolerance", 0))):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("{} {} must be a finite number".format(name, label))
            if label in {"minimum_improvement", "regression_tolerance"} and value < 0:
                raise ValueError("measurement thresholds must be non-negative")
    if set(baseline) != names or set(candidate) != names:
        raise ValueError("measurement vectors must match the frozen dimension set exactly")
    improvements = []
    regressions = []
    unchanged = []

    for dimension in dimensions:
        name = dimension["name"]
        direction = dimension["direction"]
        minimum_improvement = float(dimension.get("minimum_improvement", 0))
        regression_tolerance = float(dimension.get("regression_tolerance", 0))
        if name not in baseline or name not in candidate:
            raise ValueError("missing measurement dimension: {}".format(name))
        if direction not in {"minimize", "maximize"}:
            raise ValueError("invalid direction for {}".format(name))
        signed_delta = (
            candidate[name] - baseline[name]
            if direction == "maximize"
            else baseline[name] - candidate[name]
        )
        if not math.isfinite(signed_delta):
            raise ValueError("measurement delta overflowed for {}".format(name))
        reading = {"name": name, "signed_delta": signed_delta}
        if signed_delta > 0 and signed_delta >= minimum_improvement:
            improvements.append(reading)
        elif signed_delta < -regression_tolerance:
            regressions.append(reading)
        else:
            unchanged.append(reading)

    return {
        "promotable": bool(improvements) and not regressions,
        "improvements": improvements,
        "regressions": regressions,
        "unchanged": unchanged,
    }


def agent_efficiency_vector(agent: Dict[str, Any]) -> Dict[str, float]:
    stats = agent["stats"]
    tokens = stats["tokens"]
    return {
        "successful_tasks": float(stats["successful_tasks"]),
        "failed_attempts": float(stats["failed_attempts"]),
        "verified_steps_per_1k_tokens": (
            stats["verified_steps"] * 1000.0 / tokens if tokens else 0.0
        ),
        "total_tokens": float(tokens),
    }
