#!/usr/bin/env python3
"""Repeat real local N-box DAG execution with verifier-boundary interruption.

No model calls, inherited credentials, or production repository modifications.
Prints a machine-readable report. Every run uses its own disposable Git fixture.
"""

import argparse
import asyncio
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camol.artifacts import RunArchive
from camol.orchestrator import Orchestrator
from camol.runbook import load_runbook
from camol.runner import HarnessRunner
from camol.store import SQLiteEventStore


def git(workspace, *args):
    return subprocess.run(
        ["git", "-C", str(workspace), "-c", "core.hooksPath=/dev/null"] + list(args),
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def runbook(boxes, iteration):
    payload = load_runbook(ROOT / "examples/local-n-box-runbook.json")
    payload["run"]["id"] = "soak-{}".format(iteration)
    payload["run"]["max_concurrency"] = boxes
    payload["run"]["token_policy"].update(max_tokens_per_turn=100000, max_total_tokens=1000000)
    agent = payload["agents"][0]
    task = payload["tasks"][0]
    payload["agents"] = []
    payload["tasks"] = []
    for index in range(boxes):
        worker = copy.deepcopy(agent)
        worker.update(id="worker-{}".format(index), box="box-{}".format(index), capabilities=["code"])
        payload["agents"].append(worker)
    for index in range(boxes * 2 + 1):
        item = copy.deepcopy(task)
        identifier = "task-{}".format(index)
        dependencies = [] if index < boxes else (["task-{}".format(index - boxes)] if index < boxes * 2 else ["task-{}".format(key) for key in range(boxes, boxes * 2)])
        item.update(id=identifier, depends_on=dependencies, capabilities=["code"])
        code = "from pathlib import Path; "
        code += "assert all(Path('../build-' + name + '.txt').read_text() == 'verified' for name in {!r}); ".format(dependencies)
        code += "Path('../build-{}.txt').write_text('verified')".format(identifier)
        item["steps"][0]["commands"][0]["argv"] = ["python3", "-c", code]
        item["verification"] = [{
            "purpose": "Check dependency build output", "cwd": "workspace_root",
            "argv": ["python3", "-c", "from pathlib import Path; assert Path('build-{}.txt').read_text() == 'verified'".format(identifier)],
        }]
        payload["tasks"].append(item)
    return payload


async def trial(boxes, iteration, interrupt):
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="camol-runtime-soak-") as temporary:
        root = Path(temporary)
        source, state_dir = root / "source", root / "state"
        (source / "examples").mkdir(parents=True)
        shutil.copy(ROOT / "examples/fake_agent.py", source / "examples/fake_agent.py")
        git(source, "init", "-q")
        git(source, "config", "user.name", "Camol Runtime Fixture")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "add", ".")
        git(source, "commit", "-q", "-m", "fixture")
        original_head = git(source, "rev-parse", "HEAD")
        database = state_dir / "events.sqlite3"
        store = SQLiteEventStore(database)
        try:
            orchestrator = Orchestrator(store)
            state = orchestrator.initialize(runbook(boxes, iteration))
            run_id = state["run_id"]
            orchestrator.approve_plan(run_id, "fixture-owner", state["plan_digest"])
            if interrupt:
                reached = asyncio.Event()

                class InterruptedRunner(HarnessRunner):
                    async def _verify(self, *args, **kwargs):
                        reached.set()
                        await asyncio.Event().wait()

                runner = InterruptedRunner(orchestrator, source, state_dir=state_dir)
                pending = asyncio.create_task(runner.run_until_terminal(run_id))
                try:
                    await asyncio.wait_for(reached.wait(), timeout=45)
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                store.close()
                store = SQLiteEventStore(database)
                orchestrator = Orchestrator(store)
            runner = HarnessRunner(orchestrator, source, state_dir=state_dir)
            final = await asyncio.wait_for(runner.run_until_terminal(run_id), timeout=120)
            if final["status"] != "completed":
                raise AssertionError({"status": final["status"], "tasks": {key: value["status"] for key, value in final["tasks"].items()}, "terminal": final.get("terminal")})
            integration = Path(final["integrations"][-1]["workspace"]["path"])
            for key in final["tasks"]:
                assert (integration / ("build-" + key + ".txt")).read_text() == "verified"
            assert git(source, "status", "--porcelain") == ""
            assert git(source, "rev-parse", "HEAD") == original_head
            archive = root / "archive"
            manifest = RunArchive.export(run_id, store.read(run_id), runner.artifacts, archive)
            replayed = RunArchive.replay(archive)
            assert replayed == final
            return {
                "iteration": iteration, "boxes": boxes, "tasks": len(final["tasks"]),
                "verifier_restart": interrupt, "status": final["status"],
                "events": final["last_seq"], "artifacts": len(manifest["artifact_digests"]),
                "source_unchanged": True, "export_replay_equal": True,
                "seconds": round(time.monotonic() - began, 3),
            }
        finally:
            store.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--boxes", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100 or not 1 <= args.boxes <= 16:
        parser.error("iterations must be 1..100 and boxes 1..16")
    reports = []
    for iteration in range(args.iterations):
        print(json.dumps({"schema": "camol.runtime_soak.progress", "iteration": iteration, "boxes": args.boxes, "status": "starting"}), file=sys.stderr, flush=True)
        reports.append(await trial(args.boxes, iteration, interrupt=iteration % 2 == 1))
        print(json.dumps(dict(reports[-1], schema="camol.runtime_soak.progress")), file=sys.stderr, flush=True)
    print(json.dumps({"schema": "camol.runtime_soak", "schema_version": 1, "passed": True, "trials": reports}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
