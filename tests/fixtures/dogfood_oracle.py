"""Frozen external evaluator asset for the deterministic V0 dogfood."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from camol.hillclimb import guardrails_green


assert guardrails_green({"regressions": []}) is True
assert guardrails_green({"regressions": [{"name": "security"}]}) is False
assert guardrails_green({}) is False
