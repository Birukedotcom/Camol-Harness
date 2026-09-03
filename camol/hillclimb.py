"""Vector-based hill-climb adjudication with explicit guardrails."""

from typing import Any, Dict, List


def compare_vectors(
    baseline: Dict[str, float],
    candidate: Dict[str, float],
    dimensions: List[Dict[str, Any]],
) -> Dict[str, Any]:
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
