# Executable runbook reference

Camol's runbook is the contract the human and orchestrator agree on before a
long-running build starts. Approval freezes its canonical SHA-256 digest in the event
ledger. A changed file cannot silently resume the old run.

See [examples/three-agent-runbook.json](../examples/three-agent-runbook.json) for a
complete runnable example.

## Run

```json
{
  "id": "stable-run-id",
  "objective": "The result this run must produce",
  "max_agents": 3,
  "completion": [
    "all_tasks_succeeded",
    "all_required_evidence_present",
    "all_verifications_green",
    "no_open_blockers",
    "no_open_debug_cases"
  ],
  "token_policy": {
    "max_tokens_per_turn": 4000,
    "checkpoint_reserve": 400,
    "max_total_tokens": 30000,
    "max_turns_per_task": 6
  }
}
```

The v1 contract requires exactly three agents. Concurrency can be lower when the task
graph has fewer ready nodes, but never higher.

Completion is a conjunction of named conditions, not a confidence score. The run
stops green only when every selected condition is true.

## Rules

Rules are visible to every turn:

```json
{
  "id": "evidence-before-claim",
  "text": "Return the required evidence before asking for verification.",
  "enforcement": "hard"
}
```

`hard` means the control plane must eventually enforce or reject the behavior.
`review` means a human or verifier adjudicates it. Prose that is correctness-critical
should migrate to an executable gate.

## Agents and boxes

```json
{
  "id": "builder",
  "role": "Implement bounded changes",
  "box": ".camol/boxes/builder",
  "capabilities": ["inspect", "code", "test"],
  "adapter": {
    "kind": "process",
    "argv": ["agent-wrapper", "{packet}", "{result}"],
    "timeout_seconds": 1800
  }
}
```

All boxes must be distinct, relative paths inside the declared workspace. The process
adapter performs argv execution directly; it does not use an implicit shell.

Supported placeholders are:

```text
{workspace} {box} {packet} {result} {run_id} {task_id} {agent_id}
```

A real VM wrapper can use the same contract: copy the packet to the VM, run the agent
inside its prepared worktree, and return the result file. Provider identity, agent
identity, and readiness should remain separate structured probes.

## Tasks

Each task declares:

- `id` — stable identifier;
- `goal` — one bounded outcome;
- `depends_on` — earlier tasks whose verified receipts are required;
- `capabilities` — scheduler requirements;
- `acceptance` — observable conditions for review;
- `required_evidence` — required kinds before success;
- `max_attempts` — bounded retry count;
- `steps` — ordered agent instructions and expected commands;
- `verification` — commands the orchestrator runs outside the agent claim.

Tasks may only depend on tasks declared earlier. This makes cycles structurally
impossible in v1.

## Steps and commands

```json
{
  "id": "focused-test",
  "instruction": "Reproduce the failure against the smallest responsible seam.",
  "commands": [
    {
      "purpose": "Run the focused reproduction",
      "argv": ["python3", "-m", "unittest", "tests.test_case"]
    }
  ],
  "completion": [
    "The pre-fix behavior is captured as evidence",
    "The first divergent boundary is named"
  ]
}
```

The command list is an explicit route through the task, not permission to fake the
result. A reasoning agent may discover that a declared command is stale; it should
return a typed blocker or a checkpoint explaining the proposed correction. The plan
changes through a new reviewed digest, never by silently editing an active run.

Commands should be idempotent because a machine can die midway through one. A fully
written result file is packet-hash bound and reused on restart; a partially executed
command with no result may run again.

## Turn packet

Every invocation receives one `camol-agent-turn/v1` JSON packet:

```text
run identity + plan digest
hard/review rules
lease identity
task goal + acceptance + remaining steps
verified dependency receipts
latest checkpoint
latest verification failure
turn and run token budgets
structured return contract
```

Raw prior turns are not copied forward. The checkpoint is the durable working memory.
This keeps context growth bounded and forces each turn to leave the next turn a useful
starting state.

## Result contract

The adapter must write JSON to `{result}`:

```json
{
  "packet_sha256": "hash of the exact packet bytes",
  "status": "continue | complete | blocked",
  "checkpoint": "What is now true, what remains, and the next best action",
  "completed_step_ids": ["focused-test"],
  "input_tokens": 1200,
  "output_tokens": 500,
  "evidence": [
    {
      "kind": "artifact",
      "data": {"path": "artifact reference", "sha256": "..."}
    }
  ],
  "messages": [
    {
      "to_task_id": "integration-task",
      "kind": "proposal",
      "body": "Use the new boundary discovered in this task."
    }
  ],
  "summary": "Required when status is complete",
  "blocker": {"kind": "missing_authority", "detail": "Required when blocked"}
}
```

Evidence kinds are `command`, `tool_call`, `transcript`, `environment`, `artifact`,
`diff`, `test_result`, and `claim`. Store sensitive or large bodies outside SQLite;
put redacted metadata and content hashes in the result.

In production, the adapter wrapper should populate token counts from provider/runtime
usage receipts rather than asking the reasoning model to estimate its own consumption.

Messages may target only declared tasks and are persisted by the orchestrator before
appearing in a recipient's packet. Workers do not establish authoritative side
channels.

## State machine

```text
RUN:  draft -> ready -> running -> completed | blocked

TASK: pending -> leased -> running -> verifying -> succeeded
                             |           |
                             +-> retry <-+
                             |
                             +-> blocked
```

On retry, accepted step IDs, the latest checkpoint, and verifier output remain. The
next agent receives only those compact receipts. Agent selection is deterministic and
prefers capability fit, prior verified success, fewer failed attempts, more verified
steps per 1,000 tokens, and lower total token use—in that order.

## Operational commands

```bash
# Check the file before it can become state
python3 -m camol validate runbook.json

# Freeze it as a draft and receive the exact digest
python3 -m camol init runbook.json --db .camol/run.sqlite3

# Human approval of that digest
python3 -m camol approve --db .camol/run.sqlite3 --run-id RUN --by NAME

# Execute or resume until a declared terminal state
python3 -m camol run runbook.json --db .camol/run.sqlite3 --workspace .

# Read projection or immutable history
python3 -m camol status --db .camol/run.sqlite3 --run-id RUN
python3 -m camol events --db .camol/run.sqlite3 --run-id RUN --after 0
```
