"""Deterministic process adapter used only to prove the harness loop."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    packet_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    packet_bytes = packet_path.read_bytes()
    packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
    packet = json.loads(packet_bytes)
    task = packet["task"]
    evidence = []
    completed_steps = []

    for step in task["remaining_steps"]:
        for command in step["commands"]:
            process = subprocess.run(
                command["argv"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evidence.append(
                {
                    "kind": "command",
                    "data": {
                        "purpose": command["purpose"],
                        "argv": command["argv"],
                        "exit_code": process.returncode,
                        "stdout_sha256": hashlib.sha256(process.stdout).hexdigest(),
                        "stderr_sha256": hashlib.sha256(process.stderr).hexdigest(),
                    },
                }
            )
            if process.returncode != 0:
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "blocked",
                            "packet_sha256": packet_sha256,
                            "checkpoint": "Command failed while executing step {}.".format(step["id"]),
                            "completed_step_ids": completed_steps,
                            "input_tokens": max(1, len(packet_bytes) // 4),
                            "output_tokens": 80,
                            "evidence": evidence,
                            "blocker": {
                                "kind": "command_failed",
                                "step_id": step["id"],
                                "exit_code": process.returncode,
                            },
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return 0
        completed_steps.append(step["id"])

    artifact_dir = Path.cwd() / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "{}.txt".format(task["id"])
    artifact_path.write_text(
        "Task {} completed under plan {}.\n".format(task["id"], packet["run"]["plan_digest"]),
        encoding="utf-8",
    )
    artifact_bytes = artifact_path.read_bytes()
    evidence.extend(
        [
            {
                "kind": "artifact",
                "data": {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                },
            },
            {
                "kind": "claim",
                "data": {
                    "summary": "All declared steps completed; verification remains external."
                },
            },
        ]
    )
    result_path.write_text(
        json.dumps(
            {
                "status": "complete",
                "packet_sha256": packet_sha256,
                "checkpoint": "All remaining steps complete; artifact and claim emitted.",
                "completed_step_ids": completed_steps,
                "input_tokens": max(1, len(packet_bytes) // 4),
                "output_tokens": 140,
                "evidence": evidence,
                "summary": "Completed {} and emitted a content-addressed artifact.".format(task["id"]),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
