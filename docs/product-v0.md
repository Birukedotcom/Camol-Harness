# Camol Product V0 operator guide

Product V0 is the terminal client on top of the existing plan, readiness, lease,
evaluation, evidence, integration, and recovery kernel. It is not a web app or a
terminal multiplexer. The terminal may disappear; the supervisor remains the single
writer and keeps running.

## One lifecycle

1. Run bare `camol` from the root of the Git repository you want Camol to work on.
2. Inspect the cached top rail and `/connections` for observation ages. Startup
   makes no connection probes. Use `/connections refresh [TARGET]` for an explicit
   bounded status/catalog check; neither inventory nor authentication is task
   readiness. Run `/login`, choose Claude or Codex with the arrow keys, and
   press Enter if an account needs authentication.
3. A successfully verified picker login selects that provider's planning model when
   no plan is frozen or running. `/model` remains the explicit selector; `manual` is
   the no-call default.
4. Start `/grill GOAL` and answer every question. The topology answer uses one line
   per task: `id | goal | after=dependency,dependency`. One line creates one task;
   many lines create an arbitrary N-task graph.
   Answers are validated at each question, so invalid input can be corrected
   immediately. `/import PATH` also supports an existing executable runbook,
   including local process workers. Imports preserve the exact kernel contract and
   bind approval to this clean checkout's path and source revision.
5. Inspect `/plan`. It shows outcomes, exclusions, invariants, resource ceilings,
   task dependencies, evaluator argv, the product-plan digest, the complete canonical
   product-plan JSON, and (when executable) the kernel runbook digest.
6. Type `/approve yes` or the exact full product-plan digest. `/approve` by itself
   only prints the confirmation challenge.
7. For any provider-containing plan, `/run [--preflight-cents N]` displays the
   exact launch manifest without a provider call. Copy its `/run --accept-launch
   DIGEST` command and include `--accept-spend` when any worker is hosted. It
   discloses one common worker envelope and a separate deduplicated Claude
   preflight reservation. Only exact acknowledgement can begin preflights. Starting
   the supervisor still does not bypass readiness: each task/box pair needs a fresh
   admission bundle, reservation, grant, and fence.
8. Inspect `/status`, `/boxes`, `/box ID`, and `/events`. `Alt+0` returns to the
   orchestrator, `Alt+1` through `Alt+9` select visible boxes, and `[` / `]` cycle.
   `/box ID status|context|tools|diff|evals|events|evidence|transcript` selects a
   separately refreshed read-only pane. Artifact previews are hash-verified;
   truncation and disconnected cached views are explicitly labeled. `camol export`
   retains access to the full stored evidence.
9. `/quit` detaches. Running `camol` again in the same repository reloads the session
   and reconnects. `/stop` drains and stops the supervisor without deleting evidence.

Enter sends the composer. Shift+Enter or Ctrl+J inserts a newline. Typing `/` opens
the keyboard command-and-skill palette. Its focused input supports continued typing,
including spaces, and filters the visible choices; ↑/↓ changes the selection, Tab
completes it, and Enter accepts it. Safe inspection commands execute when selected,
while commands needing values return to the composer. `/skills` shows the built-in
Camol protocols. `/history [COUNT]` displays retained transcript entries, and
`/clear` clears only the current terminal surface. A reopened client starts with an
explicit reattach summary and does not flood the new terminal with old output. The
V0 terminal theme is deep matrix green, charcoal, gray, and white. Ctrl+C is the
primary safe-detach shortcut and leaves any authoritative supervisor running.
It cancels the client-owned planning request. `/cancel` cancels planning while
keeping the terminal open; another command cannot race an active request. A local
HTTP endpoint may finish an already submitted request after cancellation, so that
call's unreported usage stays unknown.

`/usage` reports durable planning-call tokens and durations; `/usage run` reports
worker/evaluator accounting by task, box and model. Missing provider costs are
unknown, never zero. `/debug list` and `/debug show CASE_ID` inspect retained debug
cases; `/debug inbox` shows rejected-candidate counterexamples without activating
a blocking debug protocol automatically. The full transcript is archived outside the bounded session/context window.
`/models [list|status DIGEST]` reads the passive model-artifact catalog without
downloading, loading, or rehashing large files. `/watch [list|show ID]` replays
recorded watcher state without polling any source. `/repo` renders a fresh bounded
static repository graph; `impact PATH`, `why FROM TO`, and `cycles` inspect source
relationships. Static declarations are not runtime readiness. These commands do
not substitute for the kernel CLI's explicit model and watcher mutations.
Completed runs are reconciled automatically before a new plan, and each new plan
gets a distinct run directory so a second build cannot collide with the first.

Explicit schema-V5 runbooks imported through `/import` carry invariant/evaluator
mappings and a final human acceptance gate. `/gate TASK_ID` displays a pending state
assessment; `/gate TASK_ID DIGEST` approves that exact assessment. `/accept` displays
the integrated final outcome and its digest, and `/accept DIGEST` accepts it. These
commands go through the authoritative supervisor. The existing six-question grill
still creates a legacy V4 contract; it does not infer that a passing command proves
arbitrary human-written prose invariants.

For guided V5 creation without a JSON seed, use `/grill --draft GOAL`. It collects
outcomes, invariants/oracle questions, an exact worker/profile and ceilings; `/draft`
shows the complete creation envelope. After exact envelope confirmation, bare
`/propose` makes one explicit no-tools invocation. Newly proposed commands remain
unapproved and require `/review DIGEST` before exact `/approve DIGEST`. Unknown
coverage becomes follow-up questions. See the [goal-creation wizard](goal-creation-wizard.md)
for its separate runtime-policy acknowledgements and weaker hosted-network limits.

`/propose --from REVIEWED_SEED.json GOAL` makes one explicitly requested planning
invocation to refine a reviewed V5/V6 seed into an unapproved, source-bound candidate.
Its no-tools adapter currently supports a capability-checked Claude CLI or a local
chat endpoint; Codex remains available for normal chat/imported workers, not this
proposal mode. CLI-internal request count and provider cost may remain unknown.
It preserves the seed's authority, profiles, ceilings and normative checks, and
requires human approval at every proposed state gate and final acceptance. New
authority requires questions and a revised reviewed seed, not silent expansion.
Inspect `/plan`, then approve the exact full digest; bare `yes` cannot approve a
model-generated candidate. See [seed-assisted proposals](seed-assisted-proposals.md)
for limits, audit records and cancellation behavior. This is not an unrestricted
prose-to-executable-plan generator or an in-flight amendment UI.

Codex CLI or Codex OSS V5+ runbooks can be imported when every provider adapter
contains its exact `profile_snapshot`. After plan approval, `/run` displays the
full per-worker profile and the exact launch acknowledgement digest.
Use `/run --accept-launch DIGEST --accept-spend` for hosted Codex, or
omit `--accept-spend` for local Codex OSS. No paid capability preflight runs in
this path. Read-only runtime/login/catalog checks still gate kernel admission.
The owner explicitly accepts requested-only model identity, unknown quota, ambient
network authority, and unsupported hard USD/inner-turn caps. Configured dollars
are accounting reservations, not a provider-enforced spending ceiling. An unknown
paid charge stops further paid launches. Local catalog presence is not inference,
weights-identity, resource-fit or airgap proof, and never triggers a download/load.
Mixed process/Claude/Codex/OSS launches and multiple Claude profiles are supported
through the same exact review, with compatible common hosted worker ceilings.
See [interactive launch](interactive-launch.md) for preflight deduplication,
retained failed/unknown operations, and separate cost envelopes.
See [the Codex worker tier](codex-workers.md) for its exact limitations.

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

The dependency rail is a passive inventory. Git/Docker executable presence is
`installed`, not connected; Camol does not call Docker to inspect its daemon.
Cached authentication, key presence, and local catalogs are labeled by observation
type and age. The rail says `task unverified` and does not use a filled readiness
glyph because it does not consume exact fresh task-admission evidence.
Bare `/connections` only reads this cache. `/connections refresh
[all|claude|codex|local|openai]` explicitly records:

- provider and runtime names and versions;
- authentication/reachability status;
- an opaque local credential reference such as `cli:claude` or
  `env:OPENAI_API_KEY`;
- a keyed, non-reversible fingerprint when provider status exposes an identity; and
- capability labels and observation time.

It does not read, copy, parse, or store provider credential caches. On startup, the
TUI performs no provider or HTTP probe; `↻ explicit refresh` only appears while a
requested refresh is active. `/login` opens a keyboard picker containing Claude Code and Codex CLI in
V0. Every selection suspends the TUI and gives the terminal directly to
`claude auth login` or `codex login`, even when an earlier discovery record reported authentication,
so the provider can print/open its own URL and own the browser session. Camol then
runs only the selected provider's status probe, writes the result to the orchestrator transcript,
refreshes its observed-authentication label, and shows the active model in the top rail.
A cancelled or failed native flow/status refresh cannot reuse an older success as a new login
confirmation. A login never changes the model embedded in a frozen or running plan.

Refreshes do not invoke a model or paid capability preflight. CLI status/version
commands have individual deadline/output bounds. Local catalog requests use an
absolute two-second deadline, a 1 MiB body bound, no redirects/proxies/DNS, and
client-owned transport cleanup on timeout/cancellation. Cached legacy `ready`
statuses remain compatible but never become task authority. See
[connection observations](provider-connections.md) for exact limits.

Normal messages are sent to the selected planning-only provider and streamed into
the orchestrator pane. Camol retains real human/orchestrator dialogue for continuity,
but slash commands, status dumps, and model-identity notices are excluded from the
provider context so operational UI traffic cannot crowd out the planning exchange.

Task readiness is established later by the kernel and includes the exact plan,
workspace revision, evaluator bundle, authority, capacity reservation, sandbox,
adapter, provider capability where required, and expiry.

## V0 support matrix

| Connection | Planning dialogue | Fenced worker | V0 status |
|---|---:|---:|---|
| Manual/offline | deterministic `/grill` only | no | testable, no model evidence |
| Claude CLI | yes, planning-only call | yes, Fable profile | implementation present; live account proof pending |
| Codex CLI | yes, read-only ephemeral `codex exec` | yes, imported explicit weaker tier | fake-CLI proof; live account proof pending |
| Local OpenAI-compatible | yes, loopback `/chat/completions` | via imported Codex OSS and a compatible local Responses host | catalog/fake-CLI proof; live inference pending |
| OpenAI Platform key reference | no | no | presence discovery only |

Explicit owner-manifest model downloads and static repository-graph inspection
are implemented. Automatic downloads, model-host loading, direct OpenAI Platform
Responses execution, container/VM/GCP execution targets, an interactive graph pane,
and the spatial build visualizer remain follow-up work. Watcher observations do
not provide execution-target authority.

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
