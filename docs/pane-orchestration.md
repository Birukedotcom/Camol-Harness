# Pane organization and agent addressing

This extends the product plan using the two user-supplied references. It does not
replace durable orchestration with a multiplexer and does not install either tool.

## References reviewed

- [tmax at 7aff1b0](https://github.com/theo-kirby/tmax/tree/7aff1b0a27a5744fa5a440e16b886aadd16a9b35): grouped session switching,
  remembered organization and a tiled window preview are useful navigation patterns.
  Its remote bridge separates local proxies from remote sessions and documents
  reconnect, history and command-routing limits. Reviewed README, sidebar and preview
  code. No license file was present in this snapshot; no source is copied or vendored.
- [smux at f8f591b](https://github.com/ShawnPana/smux/tree/f8f591b0b7966b210aa0b68f4f3bce54cf64e07b): an agent-neutral bridge exposes
  list/read/name/resolve/type/keys/message operations. Reviewed its implementation,
  not just its README: it has a read-before-action guard and message sender labels.
  The snapshot is MIT licensed. This design uses the interface ideas, not copied code.

## Camol identity and authority

A **task** is a frozen unit of work. A **box** is its admitted worker execution
context. A **pane** is a disposable client view into that context. One box can
have several panes (tools, diff, evidence); hiding a pane does not stop its task.
Display labels and pane indices are not authority and must never redirect work
when reused. Routing names the exact run, task, box, worker generation and lease.

The orchestrator allocates as many boxes as the reviewed DAG and resource envelope
permit. Layout must not imply a fixed three-agent topology or one box per project.
Planned, assigned, running, waiting, gate-blocked and finished work remain distinct.
Connected transport, account observation and task readiness are separate indicators.

## Commands and implementation status

| Interface | Purpose | Status |
| --- | --- | --- |
| `/overview [--attention] [--json] [--offset N] [--limit N]` | Box/task tables, dependency waits, human gates; no model call | Implemented |
| `camol overview --db PATH --run-id ID [--attention] [--json]` | Same deterministic projection outside the TUI | Implemented |
| `/boxes`, `/box ID VIEW` | Stable box selection and existing context/tools/diff/evals/evidence/transcript views | Implemented |
| `/box next`, `/box previous` | Traverse worker views from the terminal | Implemented |
| `/usage run`, `/debug inbox`, `/gate TASK` | Accounting, failed-evaluator inbox and gate detail | Implemented |
| `/switch [WORDS]`, `Alt+B` | Search current-run box/task/status/adapter metadata, grouped by orchestrator/attention/workers; arrows/Enter, preserved draft | Implemented |
| `/pin [BOX [on\|off]]`, `/group [BOX NAME\|BOX --clear]` | Persistent current-plan pins and display groups | Implemented; no authority change |
| `/layout focus\|split\|grid` | Composer plus paginated box monitoring; detail selection is scope-checked | Implemented; metadata tiles, not mirrored PTYs |
| `camol box list\|resolve\|read` | Scoped observation using explicit run/box IDs, including stopped runs | Implemented; no message delivery or execution grant |
| `camol box observe\|message\|inbox` | Lease-scoped owner CLI and embedding mailbox, idempotent sends and worker consumption receipts | Implemented; opt-in local native peer integration exists, native live acceptance and distributed remote workers remain pending |
| `adapter.peer_tools` / `Harness.peer_tools()` | Current-turn list/observe/own-inbox and read-before-send with replayable observations | Implemented for explicit Python embeddings and opt-in Codex profiles; native live acceptance pending |
| `PeerEndpoint` / `python -m camol.peer_mcp` | Explicit owner-issued local socket and bounded stdio peer tools | Implemented; schema3 Codex, restricted schema4 Claude, and schema5 Claude prompt-withholding profiles; native live acceptance pending |
| `/message BOX TEXT`, `/reply MESSAGE_ID TEXT`, `/inbox [BOX [OFFSET]]`, `/outbox [REQUEST_ID]` | Interactive sends/replies, inbox pane and immutable pending-request inspection | Implemented; explicit `/message retry ID` preserves its original scope |
| `/delegate [TASK] [--json] [--offset N] [--limit N]` | Inspect declared capability matches and recorded assignments | Implemented; not readiness or a scheduling decision |
| `/delegate --from RUNBOOK --reason TEXT [--effects POLICY.json]` | Review new/redistributed work through the existing stopped-owner revision workflow | Implemented; exact `/revise apply DIGEST` and separate `/run` remain required |
| `/delegate --propose --from SEED --reason TEXT --goal TEXT [--effects POLICY.json]` | Ask the selected planner for a bounded linked successor candidate | Implemented; one disclosed no-tools call, unchanged parent, exact human revision review and separate launch |

The overview is a ledger snapshot, not an active transport probe. Its digest and
event cursor identify what was observed. Before a ledger exists it explicitly
says `plan_only`; a damaged existing ledger cannot fall back to a healthy-looking
plan. Default pages show up to 50 rows per table, with explicit offsets for N boxes.
Text abbreviates long labels; JSON retains complete redacted identities. No raw
worker output is previewed by this metadata-only implementation.

The switcher uses the same exact ledger/plan-only source as the overview, rather
than probing accounts or treating a disconnected supervisor as a fresh dormant
pool. Its TUI renders 50 options per page; PageUp/PageDown traverse larger pools.
Multiple search words are literal, case-insensitive filters, and spaces remain
text. Escape preserves the current view and unsent composer draft. Returning from
a selection preserves the draft too; normal replies and denials become visible
in the orchestrator rather than disappearing behind the worker view.

Each selection binds the project session, run, frozen plan and sorted box IDs.
Reordering rows cannot redirect it, and an amended plan or removed box rejects
the old picker. A worker literally named `orchestrator` remains a distinct pane.
The scope is navigation identity, not an execution grant or lease read receipt;
worker progress can change while the picker is open. Close/reopen to refresh its
metadata snapshot. Line mode prints the first 50 matches and supports `/box ID`
for selection; it does not pretend to offer a graphical picker.

## Retained box inspection

For an already-running remote supervisor, `camol remote monitor --target PROFILE
--state-dir LOCAL_JOURNAL` provides a read-only terminal overview and box selector
through the pinned SSH bridge. It never reuses local project state as remote truth,
and it marks retained observations stale after a failed refresh. See
[remote monitor setup and limits](ssh-control.md#remote-terminal-monitor).

`camol box list --state-dir STATE_DIR --run-id RUN_ID` lists the exact run's
workers. `camol box resolve BOX_ID --state-dir STATE_DIR --run-id RUN_ID` resolves
one exact worker identity; prefixes, display labels and pane numbers are rejected.
`camol box read BOX_ID --state-dir STATE_DIR --run-id RUN_ID --tail --limit 200`
returns associated events, task contracts/states, recorded admission workspaces and
up to 16 artifact previews. Uppercase values are placeholders: use an existing
absolute state directory and the IDs shown by `/status` and `/boxes`.
`--db` selects an existing database inside that directory, default `camol.sqlite3`.
Forward consumers resume with `--after SEQUENCE`; `--no-previews` withholds artifact
bodies. Reports carry run, plan digest, cut cursor and snapshot digest. These are
observation records, not authenticated execution receipts.

Python clients use `BoxInspector(state_dir).list(run_id)`, `.resolve(run_id, box_id)`
and `.read(run_id, box_id)`, or `Harness.inspect_box(box_id)` while the harness is
open. No TUI, tmux, supervisor connection, model call or worker process is required.
A newly opened interactive client can display `/box` evidence from a stopped run;
the header says `RETAINED`. A same-client memory-only fallback says `STALE`, is
bound to session/run/plan/box, and never substitutes for a corrupt existing ledger.
Disconnected box lists retain recorded state instead of calling all boxes dormant.
Workspace identity comes from exact run-bound admission events, not an unscoped
directory scan. Neither receipts nor retained output prove a currently live
connection, current filesystem contents or current task readiness.

The offline reader bounds replay to 20,000 events, 2 MiB per row and 64 MiB total
by default; oversized runs fail explicitly rather than returning partial healthy
state. The Python API permits bounded increases. Pages contain at most 1,000
events. Previews verify content hash/length and producer run/task/box identity,
withhold raw artifacts, reapply credential redaction, escape terminal controls and
truncate at 16,000 characters. Each object read is capped at 1 MiB, the preview
batch at 8 MiB. Full retained evidence remains available through exports.

Cold SQLite reads do not initialize state, artifacts or sidecars. Live WAL reads
may maintain SQLite shared-memory coordination but never append events or repair
the database. Owner-controlled state is required; selected symlinks, hardlinked or
special database/artifact files and unsafe sidecars are rejected. This is an
owner-side diagnostic API, not a sandbox against an attacker controlling the owner
UID. It supplies no messaging, input injection, lease grant or automatic resume.

## Tiled monitoring

`/layout split` keeps the orchestrator transcript/composer beside one row of box
tiles. `/layout grid` allows more rows; `/layout focus` restores the single-view
client. These are ephemeral client layout choices, not plan or session-schema
changes. Reopening defaults to focus. A box detail selected while tiled keeps the
orchestrator transcript beside its read-only detail; Escape returns to the tiles
and focuses the composer without deleting an unsent draft.

Alt+Left/Alt+Right pages the monitor. Tab then Enter, or a click, opens a tile.
Ordinary prompt spaces and letters remain input. The existing bottom box bar,
Alt+B picker, pins and groups remain available. Tiles follow the same attention,
pin and group order as the picker. Pages hold at most six boxes, adapting to the
monitor's actual width and terminal height. N boxes do not require N widgets or
N independent database reads; a single ledger snapshot populates the page.

Tiles show exact box/task labels, lifecycle, gate/wait, recorded agent tokens and
turns, and the event cursor. The heading distinguishes `plan_only` from a retained
`ledger_snapshot`; neither is a live readiness or connectivity proof. This is
metadata monitoring, not terminal-output replication, 3D rendering, or a remote
worker bridge. The pure `camol.pane_layout.monitor_snapshot` and `page` projection
helpers are usable without Textual. Line clients get an explicit tiling limitation
and continue using `/overview` and `/box`.

Refreshes serialize the project observation against commands. A failed or deferred
read disables and clears the tiles with an unavailable label, rather than keeping
a healthy-looking stale interaction. Resize cannot revive invalidated tiles.
Each input captures its exact key and scope before the selection event is queued;
page refresh cannot reinterpret that event as a different worker. The existing
scope-checked switch command rereads current state and refuses changed plans.
No tile selection sends terminal input, spawns a process or grants execution.

## Remaining UI implementation

Keep the orchestrator composer available and retain the bottom box navigator.
Current-run custom groups and pins are implemented. Project/target grouping and
folding remain future work. Preserve selection by immutable identity during refresh,
never by row number.
Focus/split/paginated-grid monitoring is implemented as described above. Richer
task-qualified dependency evidence and project/target folding remain open;
destructive controls stay separate from navigation and monitoring.

### Persistent display organization

`/pin BOX` and `/pin BOX on` idempotently pin an exact box; `/pin BOX off` removes
the pin. Pin order is insertion order. `/group BOX API build` assigns the literal
display label `API build`; quoted multiword names work too. `/group BOX --clear`
removes the group. With no arguments, `/pin` and `/group` list current preferences.
These commands never reinterpret a pane number or partial name as an exact box ID.

Pins lead the bottom shortcut list and are marked `★`. The searchable picker keeps
the orchestrator first, then attention items, then ordinary pinned boxes, then
remaining workers ordered by group and exact ID. Group labels and pin marks never
replace lifecycle/readiness indicators. Existing selections retain exact identity
when preferences reorder rows. Search matches literal group words along with
box/task/status/adapter metadata; Rich markup in a label is displayed as text.

Preferences live in an owner-private `pane-organization.json` beside the project
session. Its schema is separate from both the session and the kernel ledger:
older session schemas are not migrated to add this feature. Updates use the same
cross-process project transaction as other client commands and atomic replacement.
Records are capped at 64 KiB, 1,000 pins and 1,000 group assignments; group names
are 1..64 printable characters. Linked, special, duplicate-key, malformed and
oversized files are rejected. A damaged record leaves the basic fleet usable with
an explicit warning, while preference commands and picker opening report denial;
the record is not silently repaired or overwritten.

Scope binds project session, workspace, state directory, run, frozen product/kernel
plans and the sorted exact box IDs. A new plan/run does not inherit preferences
for reused box names. Reading another scope returns an empty view without writing;
the next explicit preference edit replaces the active preference record. Pins are
not an archive of old run layouts. Neither organization nor selection changes task
contracts, grants, budgets, leases, evaluations, plan approval or worker execution.

## Agent-to-agent bridge

The [turn-scoped peer API](peer-tools.md) supports explicit Python embedding
adapters and opt-in Codex/Claude worker profiles through a local MCP relay.
Its recorded observations bind the caller's current turn. Live native acceptance,
provider-specific startup enforcement and remote workers remain open integration gates.

Observation and mutation are separate commands. A read receipt should bind the
exact subject/generation and observed cursor, not merely a temporary file indicating
that somebody read a pane earlier. Labels resolve uniquely or fail; disconnected
targets refuse delivery rather than replaying stale keystrokes after reconnect.
Messages carry sender, recipient, task, correlation/request ID, expiry and reply
address. Delivery and consumption acknowledgments are distinct from task success.
An agent message can supply evidence or request a plan change; it cannot approve
itself, expand grants, evade budgets or substitute for an evaluator receipt.

The [lease-scoped mailbox](box-mailbox.md) implements the owner CLI, embedding
service, versioned worker sends and packet consumption protocol. Its documentation
distinguishes prepared-packet delivery from consumption and current-generation
preconditions from proof of actual reading. TUI messaging uses a private immutable
outbox for uncertain outcomes. Opt-in worker tools can request fresh observations;
automatic model-directed peer coordination still needs live acceptance.

An optional tmux adapter may expose explicitly registered views later. It must
bind its socket/server/session/pane identity, use bounded reads, and require
explicit scoped authority for typing or special keys. Camol must not scan unrelated
terminal sessions, infer trust from a pane label, or send arbitrary input through
the default bridge. Terminal pixels and process names are observations, not proof
that a requested command executed or passed.

## Acceptance gates

Test 1, 3 and 50+ boxes; dependency and human-gate attention; missing/corrupt stores;
stable identity during reorder/removal; label collisions; keyboard-only navigation;
narrow terminals; redaction and terminal-control injection; reconnect without
duplicate messages; stale generation/cursor denial; restart-safe inboxes; client
closure without worker termination; and exact plan approval for new delegated work.
Overview and switcher tests cover metadata/projection and keyboard navigation.
Tiled metadata monitoring, explicit Python peer tools and opt-in Codex local MCP
transport are implemented as described above. Native model-directed tool use,
live Claude tool use, remote peer transport and external tmux attachment are not
claimed verified. The durable mailbox core has its own local execution/CLI tests.

## Delegation review

`/delegate` lists tasks, recorded assignments, unmet dependencies and counts of
workers whose declared capabilities cover the task. `/delegate EXACT_TASK_ID`
shows each box's match or missing capabilities, recorded lifecycle and current
task. Both accept `--json`, `--offset N` and `--limit N` (1..200); pagination is
over tasks in the summary and over boxes in task detail. Display labels are not
alternative addresses. This projection is also available as
`camol.delegation.delegation_snapshot(state, ...)` for trusted Python callers.

A matching busy box remains a capability match, not a free slot. Requested
resources, dependency completion, provider access, money, freshness, workspace,
authority and human gates still require normal admission. The report explicitly
labels `declared_capabilities_only`, reports no readiness proof, and neither
reserves a box nor changes the scheduler. It uses the same exact selected-run
ledger/plan-only source as `/overview`; corrupt state cannot become a dormant
healthy-looking pool. It does not inspect other terminals or contact a model.

New tasks, changed commands, evaluator changes or redistribution outside the
frozen plan use `/delegate --from RUNBOOK --reason 'WHY' [--effects POLICY.json]`.
This routes to the existing stopped-owner `/revise` review, rather than creating
a second approval mechanism. The full successor, policy/task delta, inherited
usage, effects and invalidated evidence are reviewed together. The current plan
remains active during review. `/revise apply REVIEW_DIGEST` seals the old run and
approves the exact linked successor; `/run` separately starts it subject to fresh
admission. `/delegate apply` is not an approval command. This path retains the
existing source-bound V5+ revision requirements, quiescence checks, recovery and
conservative reverify-all policy; it does not migrate legacy unbound sessions.

This is human-reviewed runbook delegation, not automatic task generation,
force-assignment to a selected pane, live migration or an agent-facing authority
tool. Natural-language delegation proposals and native peer tool registration
remain separate work. A message can carry a discovery to the orchestrator but
cannot amend the plan or grant execution by itself.

The overview/controller/TUI group passes 60 tests on Python 3.9 (15.337 seconds)
and Python 3.12 (14.006 seconds), including the real composer route. The new slice
has seven projection/CLI tests and one terminal test. No external terminal config,
account, model, or remote host was changed to run these checks.
