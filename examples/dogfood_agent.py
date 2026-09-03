"""Deterministic coding-agent fixture for the Camol-on-Camol V0 proof."""

import hashlib
import json
import sys
from pathlib import Path


FUNCTION = '''\n\ndef guardrails_green(verdict):\n    """Return true only when a hill-climb verdict has no guardrail regressions."""\n    return isinstance(verdict, dict) and verdict.get("regressions") == []\n'''


def main() -> int:
    packet_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    packet_bytes = packet_path.read_bytes()
    packet = json.loads(packet_bytes)
    target = Path("camol/hillclimb.py")
    source = target.read_text(encoding="utf-8")
    if "def guardrails_green(" not in source:
        target.write_text(source.rstrip() + FUNCTION + "\n", encoding="utf-8")
    content = target.read_bytes()
    result = {
        "status": "complete",
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "checkpoint": "Guardrail helper implemented; frozen evaluator remains external.",
        "completed_step_ids": ["implement-helper"],
        "input_tokens": max(1, len(packet_bytes) // 4),
        "output_tokens": 120,
        "evidence": [
            {"kind": "command", "data": {"operation": "edit", "path": str(target)}},
            {
                "kind": "artifact",
                "data": {"path": str(target), "sha256": hashlib.sha256(content).hexdigest()},
            },
            {"kind": "claim", "data": {"summary": "Added strict guardrail predicate."}},
        ],
        "summary": "Implemented a strict helper in Camol's hill-climb module.",
    }
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
