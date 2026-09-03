# Camol Harness

Camol is a restart-safe Python control plane for one human-guided orchestrator and an
N-box worker pool. Each run activates only the boxes its plan can justify within a
human-approved concurrency, cost, infrastructure, and authority envelope.

The consolidated product direction, trust model, transparent tool-log protocol,
invariant gates, cmux/GCP compatibility target, and implementation roadmap live in
[SPEC.md](SPEC.md). That document is authoritative for product decisions; the files
under `docs/` are narrower executable protocol references.

Its operating loop is:

```text
human + orchestrator freeze the plan
               |
               v
       required boxes execute
               |
               v
 checkpoints + evidence + token use
               |
               v
      independent verification
          |              |
        green           red
          |              |
       advance    compact feedback + retry
          |              |
          +-------<------+
               |
               v
  declared completion set becomes empty
```

The orchestrator owns state, task leases, evidence, verification, budgets, retries,
and completion. Agents work inside separate boxes and cannot declare themselves done.

## What is executable now

- A JSON runbook describes the objective, registered workers, concurrency, boxes,
  rules, per-step instructions and commands, evidence requirements, verification
  commands, retry limits, token budgets, and terminal conditions.
- A SQLite event ledger reconstructs the run after process restart.
- The scheduler fills compatible boxes up to the run's approved concurrency and holds
  dependent work until its receipts are green.
- Each turn gets a compact context packet containing the task, remaining steps,
  dependency receipts, latest checkpoint, latest verifier result, and remaining token
  budget—never an automatically growing transcript.
- Every result is bound to the exact packet hash. If the orchestrator dies after an
  agent writes a result, it consumes that result after restart instead of paying for
  and executing the turn twice.
- Verification commands are run by the orchestrator. Missing evidence, failed checks,
  invalid leases, token overruns, exhausted attempts, and scheduler deadlocks cannot
  become green completion.
- Debug cases require observed behavior, target behavior, reproduction steps, and a
  complete evidence contract before they can be promoted into evals.
- Hill climbs compare named measurement vectors; a gain on one dimension cannot hide
  a forbidden regression on another.

The current executable architecture is in
[docs/architecture.md](docs/architecture.md), the runbook fields and command flow are
in [docs/runbook-reference.md](docs/runbook-reference.md), and the debug ratchet is in
[docs/debugger-protocol.md](docs/debugger-protocol.md). The planned dependency rail,
repository crawler, graph model, and terminal graph interactions are specified in
[docs/repository-graph.md](docs/repository-graph.md). The iteration canaries, external
coding-suite adapters, long-horizon campaigns, and matched harness comparisons are in
[docs/evaluation-program.md](docs/evaluation-program.md).

The distinction between terminals, execution targets, workers, workspaces, boxes, and
leases—and the N-box scaling contract—is in
[docs/execution-topology.md](docs/execution-topology.md).

## Run the deterministic three-agent proof

Python 3.9+ is sufficient; the harness has no runtime dependencies.

```bash
python3 -m unittest discover -v
python3 -m camol validate examples/three-agent-runbook.json
python3 -m camol run examples/three-agent-runbook.json \
  --db .camol/demo.sqlite3 \
  --workspace . \
  --approve-by "$USER"
```

Inspect the durable projection or raw event history:

```bash
python3 -m camol status --db .camol/demo.sqlite3 --run-id three-agent-demo
python3 -m camol events --db .camol/demo.sqlite3 --run-id three-agent-demo
```

The example uses [examples/fake_agent.py](examples/fake_agent.py) so the orchestration
semantics are deterministic and free. Replace each agent's `adapter.argv` with a
wrapper for the real agent runtime. The wrapper receives `{packet}` and `{result}`;
it must use the packet as its bounded prompt and write the structured result contract.

## Current boundary

The framework launches process adapters and gives every one a separate box path. It
does not yet create Git worktrees, provision VMs, or contain a vendor-specific Codex,
Claude, cmux, or exe.dev wrapper. Those are the next adapter layer and do not need to
change the state machine.
