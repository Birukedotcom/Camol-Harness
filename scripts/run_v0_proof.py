#!/usr/bin/env python3
"""Run the deterministic Camol-on-Camol proof from a clean detached clone."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def command(argv, *, cwd, env=None):
    result = subprocess.run(
        argv, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "command failed ({}):\nstdout:\n{}\nstderr:\n{}".format(
                " ".join(argv), result.stdout, result.stderr
            )
        )
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    harness = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    try:
        output.relative_to(harness)
    except ValueError:
        pass
    else:
        raise SystemExit("proof output must be outside the source repository")
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=harness).strip():
        raise SystemExit("proof requires a clean harness checkout")
    revision = command(["git", "rev-parse", "HEAD"], cwd=harness).strip()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="camol-v0-proof.") as temporary:
        root = Path(temporary)
        source = root / "source"
        state = root / "state"
        command(["git", "clone", "--quiet", "--no-hardlinks", str(harness), str(source)], cwd=root)
        command(["git", "checkout", "--quiet", "--detach", revision], cwd=source)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(harness), environment.get("PYTHONPATH", "")) if item
        )
        run_summary = json.loads(command([
            sys.executable, "-m", "camol", "run", str(source / "examples/camol-dogfood-runbook.json"),
            "--workspace", str(source), "--state-dir", str(state), "--approve-by", "v0-proof-owner",
        ], cwd=source, env=environment))
        archive = output / "archive"
        command([
            sys.executable, "-m", "camol", "export", "--db", str(state / "camol.sqlite3"),
            "--state-dir", str(state), "--run-id", "camol-v0-dogfood", "--output", str(archive),
        ], cwd=harness, env=environment)
        verified = json.loads(command(
            [sys.executable, "-m", "camol", "verify-export", str(archive)],
            cwd=harness, env=environment,
        ))
        connection = sqlite3.connect(str(state / "camol.sqlite3"))
        try:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM events WHERE run_id = ? ORDER BY seq",
                ("camol-v0-dogfood",),
            ).fetchall()
        finally:
            connection.close()
        counts = {}
        payloads = []
        for event_type, payload_json in rows:
            counts[event_type] = counts.get(event_type, 0) + 1
            payloads.append((event_type, json.loads(payload_json)))
        integrations = [payload["receipt"] for event_type, payload in payloads if event_type == "INTEGRATION_ACCEPTED"]
        archive_manifest = (archive / "manifest.json").read_bytes()
        proof = {
            "schema": "camol.v0_proof", "schema_version": 1,
            "claim": "deterministic_local_kernel_dogfood",
            "hosted_model_proof": False,
            "harness_commit": revision,
            "source_revision": revision,
            "run_id": run_summary["run_id"], "status": run_summary["status"],
            "plan_digest": run_summary["plan_digest"],
            "integration_head": integrations[-1]["revision"] if integrations else None,
            "candidate_count": counts.get("CANDIDATE_CAPTURED", 0),
            "integration_count": len(integrations),
            "counterexample_count": counts.get("COUNTEREXAMPLE_RECORDED", 0),
            "event_counts": counts,
            "archive_verified": verified.get("valid") is True,
            "archive_manifest_sha256": "sha256:" + hashlib.sha256(archive_manifest).hexdigest(),
            "limits": [
                "The builder is a deterministic process fixture, not a hosted-model turn.",
                "The archive contains controlled evaluator failure and refinement; daemon restart is covered separately.",
                "This one run is evidence for the local kernel path only and carries no statistical claim."
            ],
        }
        if (
            proof["status"] != "completed"
            or not proof["archive_verified"]
            or proof["integration_count"] != 1
            or proof["candidate_count"] != 2
            or proof["counterexample_count"] != 1
            or counts.get("TASK_SUCCEEDED") != 1
        ):
            raise SystemExit("dogfood proof did not satisfy its declared gate")
        (output / "proof.json").write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(proof, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
