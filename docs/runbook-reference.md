# Executable runbook reference

Camol's runbook is the contract the human and orchestrator agree on before a
long-running build starts. Approval freezes its canonical SHA-256 digest in the event
ledger. A changed file cannot silently resume the old run.

See [examples/three-agent-runbook.json](../examples/three-agent-runbook.json) for a
complete runnable example.

## Schema versions

Four `schema_version` values are readable. Any other value is rejected with
`unsupported schema_version`.

| Version | Status | Differences |
|---|---|---|
| `1` | frozen; digests pinned in `tests/test_runbook.py` | Accepts the legacy `run.max_agents` alias, ignores unknown fields, and has no readiness fields. Normalization is byte-identical to the pre-M0 kernel, so existing frozen plans still resume. |
| `2` | frozen | Requires `run.max_concurrency` (the alias is rejected), rejects unknown fields at every object level, rejects booleans in integer fields, requires `run.readiness_policy`, and requires `trust_tier` on every agent. |
| `3` | frozen | Preserves v2 semantics and adds a provider-neutral hosted-adapter shape: `kind`, workspace-relative `profile`, optional strict `profile_snapshot`, and `timeout_seconds`. Process adapters retain `kind`, `argv`, and `timeout_seconds`. |
| `4` | current | Preserves v3 semantics, requires an explicit `evaluator_assets` list on every task, and permits an explicit verification-command `cwd` of `box` or `workspace_root`. Protected asset manifests/bytes and evaluator location are digest-bound; changed workspace copies cannot pass. |

A v1 file that contains a v2 field (`readiness_policy` or `trust_tier`) is rejected
rather than partially reinterpreted. Upgrading is explicit:

```python
from camol.runbook import migrate_runbook_v1_to_v2

v2 = migrate_runbook_v1_to_v2(
    v1_runbook,
    readiness_policy={"receipt_ttl_seconds": 300},
    trust_tiers={"strategist": "developer_trusted", "builder": "developer_trusted"},
)
```

Nothing is defaulted during migration; every agent needs a trust tier and the
readiness policy must be supplied. The migrated plan has a different digest from its
v1 source because it freezes more decisions.

Migration from v2 to v3 is also explicit:

```python
from camol.runbook import migrate_runbook_v2_to_v3

v3 = migrate_runbook_v2_to_v3(v2)
```

For existing process workers no new choices are defaulted. To use Claude CLI,
replace that worker's adapter only after migration:

```json
{
  "kind": "claude_cli",
  "profile": "profiles/models/claude-fable-5-1.yaml",
  "timeout_seconds": 1800
}
```

Hosted adapters cannot supply arbitrary `argv`; invocation authority lives in
the versioned profile and adapter implementation. See
[Provider and model adapters](provider-adapters.md).

Product-generated executable plans include `profile_snapshot`, a complete
digest-bound effective profile after approved effort and cost/token/turn ceilings
have been applied. Hand-authored legacy runbooks may continue to reference only the
profile path. Process adapters cannot carry a hosted profile snapshot.

Migration from v3 to v4 requires an explicit choice for every task—even when a
self-contained evaluator has no external assets:

```python
from camol.runbook import migrate_runbook_v3_to_v4

v4 = migrate_runbook_v3_to_v4(
    v3,
    evaluator_assets={"build": ["tests/frozen"], "docs": []},
)
```

The v2 additions look like this:

```json
{
  "schema_version": 2,
  "run": {
    "max_concurrency": 3,
    "readiness_policy": {
      "receipt_ttl_seconds": 300
    }
  },
  "agents": [
    {"id": "builder", "trust_tier": "developer_trusted"}
  ]
}
```

`trust_tier` is one of `developer_trusted`, `developer_sandboxed`, or `sandboxed`
and describes the worker's process/credential posture. It is distinct from a
workspace's `filesystem_policy` (`read_only`, `isolated_worktree_write`,
`shared_checkout_write`), which describes actual source access.

There is no plan field that turns readiness proof off. `require_readiness_receipt`
is rejected explicitly, whatever its value; `READY_TO_LEASE` is a kernel invariant,
not a runbook option.

The scheduler enforces both fields. A task cannot lease without a fresh task/worker/
target-bound admission bundle, and the selected sandbox backend must satisfy the
declared trust tier.

Plan digests use `camol.schema.canonical_digest`: sorted keys, `,`/`:` separators,
ASCII-escaped UTF-8, SHA-256, and a hard rejection of NaN, infinities, non-string
keys, and non-JSON values. The same function hashes readiness receipts, workspace
receipts, reservations, grants, and lease fences (`camol.readiness`).

## Run

```json
{
  "id": "stable-run-id",
  "objective": "The result this run must produce",
  "max_concurrency": 8,
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

The executable format accepts any non-empty registered worker list.
`max_concurrency` is a positive run-specific ceiling and cannot exceed the currently
registered workers. The scheduler may use fewer according to ready work,
dependencies, capability matching, policy, and budget. Legacy `max_agents` input is
accepted as an alias and retained in its canonical plan for existing digest/replay
compatibility; new runbooks use `max_concurrency`.

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

Agent IDs and `role` strings describe capabilities or current policy; they do not
reserve permanent builder/verifier/watcher positions. Different workers may receive
different tasks. Competing implementations of one logical objective are encoded as
distinct task IDs so the exclusive lease invariant is preserved. A first-class
candidate/comparison-group field is planned but is not implemented in the current
schema.

Supported placeholders are:

```text
{workspace} {box} {packet} {result} {run_id} {task_id} {agent_id}
```

A real execution-target wrapper can use the same contract: deliver the packet to a
local host, VM, container, pod, or remote worker, run the agent inside its prepared
workspace, and return the result file. Target, transport, worker, runtime, workspace,
box, agent, and readiness identities remain separate structured records.

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
- `verification` — commands the orchestrator runs outside the agent claim;
- `evaluator_assets` (v4) — repository-relative evaluator files/directories whose
  canonical bytes are stored outside builder write authority.

Tasks may only depend on tasks declared earlier. This makes cycles structurally
impossible in every schema version.

In v4, `verification` commands and human-approved acceptance/rule text are compiled
with the expanded evaluator-asset manifest. Their digest is bound into readiness and
the lease. The candidate runs in a separate verifier worktree; a protected asset
change is a counterexample and the command is not launched.

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

In schema V4, verification commands may set `cwd`; it is optional and defaults to
`box`. `workspace_root` runs the evaluator from the root of the independent verifier
or integration worktree. Product V0 uses that explicit root for its generated final
gate so a repository-wide check sees every accepted task. Step commands do not
accept `cwd`: workers always operate inside their isolated box, and changing that
requires a future versioned worker-execution contract. Older schema versions reject
`cwd`; their verification location remains the historical box default.

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
latest evaluator counterexample
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

Evidence kinds are `command`, `tool_call`, `model_request`, `model_usage`,
`transcript`, `environment`, `artifact`, `diff`, `test_result`, and `claim`. Store
sensitive or large bodies outside SQLite; put redacted metadata and content hashes
in the result.

In production, the adapter wrapper should populate token counts from provider/runtime
usage receipts rather than asking the reasoning model to estimate its own consumption.

Messages may target only declared tasks and are persisted by the orchestrator before
appearing in a recipient's packet. Workers do not establish authoritative side
channels.

## State machine

```text
RUN:  draft -> ready -> running -> completed | blocked

TASK: pending <-> waiting
         |
         +-> leased -> running -> verifying -> succeeded
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
python3 -m camol run runbook.json --workspace . \
  --state-dir /absolute/path/outside/repository/state \
  --db /absolute/path/outside/repository/state/run.sqlite3

# Prove task-specific readiness without starting work (read-only; exit 0/2/3)
python3 -m camol doctor runbook.json --workspace . --state-dir /outside/repo --json

# Read projection or immutable history
python3 -m camol status --db .camol/run.sqlite3 --run-id RUN
python3 -m camol events --db .camol/run.sqlite3 --run-id RUN --after 0
```
