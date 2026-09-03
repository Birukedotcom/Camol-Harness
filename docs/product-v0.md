# Camol Product V0 operator guide

Product V0 is the terminal client on top of the existing plan, readiness, lease,
evaluation, evidence, integration, and recovery kernel. It is not a web app or a
terminal multiplexer. The terminal may disappear; the supervisor remains the single
writer and keeps running.

## One lifecycle

1. Run bare `camol` from the root of the Git repository you want Camol to work on.
2. Wait for the top rail's `↻` connection scan to settle, then inspect
   `/connections` for details. A filled connection glyph proves authentication or
   reachability only.
3. Select a planning model with `/model`. `manual` is the no-call default.
4. Start `/grill GOAL` and answer every question. The topology answer uses one line
   per task: `id | goal | after=dependency,dependency`. One line creates one task;
   many lines create an arbitrary N-task graph.
5. Inspect `/plan`. It shows outcomes, exclusions, invariants, resource ceilings,
   task dependencies, evaluator argv, the product-plan digest, the complete canonical
   product-plan JSON, and (when executable) the kernel runbook digest.
6. Type `/approve yes` or the exact full product-plan digest. `/approve` by itself
   only prints the confirmation challenge.
7. Run `/run --accept-spend --worker-cents N [--preflight-cents N]` for the Claude
   profile. `--worker-cents` must exactly repeat the total ceiling in the approved
   plan; the separately capped preflight accepts 1–100 cents. This validates a
   clean immutable source input before the no-tools preflight. Starting
   the supervisor still does not bypass readiness: each task/box pair needs a fresh
   admission bundle, reservation, grant, and fence.
8. Inspect `/status`, `/boxes`, `/box ID`, and `/events`. `Alt+0` returns to the
   orchestrator, `Alt+1` through `Alt+9` select visible boxes, and `[` / `]` cycle.
9. `/quit` detaches. Running `camol` again in the same repository reloads the session
   and reconnects. `/stop` drains and stops the supervisor without deleting evidence.

Changing `/model` or `/effort` after a proposal clears that proposal and its approval.
`/btw` is durable context but intentionally cannot mutate a frozen plan. If a note
changes scope, an invariant, a task, or an evaluator, run `/grill` again and approve
the new digest.

The resources answer is intentionally machine-readable rather than aspirational prose:
`boxes=N turns=N tokens=N cost_cents=N turn_timeout_seconds=N`. Put no-deploy,
no-network, provider, file-scope, and other behavioral boundaries in exclusions or
invariants. Unrecognized resource prose is rejected instead of silently ignored.
Intermediate tasks run a patch-integrity check; the exact human evaluator runs in a
generated final stage only after every declared task has integrated. That evaluator
runs from the integration worktree root, so repository-wide commands observe the
whole assembled result instead of one worker subdirectory.

New proposals use `camol.plan_proposal` schema V2, which freezes box-pool size,
concurrency, turn, token, worker-cost, and timeout ceilings. Saved V1 sessions remain
readable with either V1 shape that Product V0 previously emitted: the original
three-field resources or the transitional six-field resources. Missing ceilings in
the three-field shape receive documented compatibility defaults only when Camol must
derive an effective display or execution policy. Camol never rewrites a saved V1
proposal.

## Connection versus readiness

The dependency rail is an inventory. Git/Docker presence and provider login are not
lease authority. `/connections` records only:

- provider and runtime names and versions;
- authentication/reachability status;
- an opaque local credential reference such as `cli:claude` or
  `env:OPENAI_API_KEY`;
- a keyed, non-reversible fingerprint when provider status exposes an identity; and
- capability labels and observation time.

It does not read, copy, parse, or store provider credential caches. On startup, the
TUI refreshes this inventory in a disposable background thread; `↻` means probing,
not ready. `/login` suspends the TUI and gives the terminal directly to
`claude auth login` or `codex login`, then refreshes the rail automatically.

Task readiness is established later by the kernel and includes the exact plan,
workspace revision, evaluator bundle, authority, capacity reservation, sandbox,
adapter, provider capability where required, and expiry.

## V0 support matrix

| Connection | Planning dialogue | Fenced worker | V0 status |
|---|---:|---:|---|
| Manual/offline | deterministic `/grill` only | no | testable, no model evidence |
| Claude CLI | yes, planning-only call | yes, Fable profile | implementation present; live account proof pending |
| Codex CLI | yes, read-only ephemeral `codex exec` | no | planning-only |
| Local OpenAI-compatible | yes, loopback `/chat/completions` | no | planning-only |
| OpenAI Platform key reference | no | no | presence discovery only |

Automatic model downloads, a fenced Codex/local worker, OpenAI Responses execution,
containers/VMs/GCP targets, repository-graph views, and the spatial build visualizer
remain post-V0 work.

## State and recovery

On macOS, the default session root is
`~/Library/Application Support/Camol/projects/<workspace-key>`. Other platforms use
`$XDG_STATE_HOME/camol` or `~/.local/state/camol`. `CAMOL_STATE_HOME` overrides it.
Files and directories are owner-only. The session record contains redacted
conversation text, the candidate plan, its digest and approval, selected model and
effort, box cursor, and event cursor. It never contains credentials.

Each run has an external state directory with its SQLite ledger, content-addressed
artifacts, packets, worktrees, and authenticated control files. Long state paths use
a private owner-only hashed runtime directory for the Unix socket so macOS path
limits cannot prevent startup; the token and all authoritative state remain under
the run directory.

The V2 local control protocol returns the exact runbook and digest, bounded events
after a sequence cursor, and bounded per-box event/evidence views. Reattaching cannot
create a second run because the daemon remains the sole writer and the client checks
the remote plan digest before accepting an existing socket. Box association is
reconstructed from the complete assignment history before the requested display
cursor is applied, so later task-only events do not disappear after a reconnect.
