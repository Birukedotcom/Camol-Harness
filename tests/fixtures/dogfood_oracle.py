"""Frozen external evaluator asset for the deterministic V0 dogfood."""

from camol.hillclimb import guardrails_green


assert guardrails_green({"regressions": []}) is True
assert guardrails_green({"regressions": [{"name": "security"}]}) is False
assert guardrails_green({}) is False
