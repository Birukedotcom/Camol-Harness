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
| `/layout focus\|split\|grid`, `/pin BOX`, `/group BOX NAME` | Client-only tiling, pinned monitoring and organization | Planned |
| `camol box list\|resolve\|read` | Machine-readable scoped bridge using explicit run/box IDs | Planned; overview JSON is available now |
| `camol box message` | Durable, idempotent, scoped agent/human inbox with acknowledgments | Planned |
| `/delegate` | Review proposed task allocation; approved plan amendment when scope changes | Planned; kernel leasing remains authoritative |

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

## Next UI implementation

Keep the orchestrator composer available and retain the bottom box navigator.
Extend the current-run switcher with project/target grouping, optional user groups
and pins. Preserve selection by immutable identity during refresh, never by row number.
The native terminal overview should offer focus, split and paginated grid layouts.
Each tile identifies its box/task, current lifecycle, last event cursor, gate/wait,
and bounded usage. Selecting a tile opens the detailed read-only view. Escape
returns to the composer without changing execution; destructive controls remain
separate explicit actions. Keyboard routing must not steal ordinary prompt spaces
or letters. The same projection/API must work without Textual or tmux.

## Agent-to-agent bridge

Observation and mutation are separate commands. A read receipt should bind the
exact subject/generation and observed cursor, not merely a temporary file indicating
that somebody read a pane earlier. Labels resolve uniquely or fail; disconnected
targets refuse delivery rather than replaying stale keystrokes after reconnect.
Messages carry sender, recipient, task, correlation/request ID, expiry and reply
address. Delivery and consumption acknowledgments are distinct from task success.
An agent message can supply evidence or request a plan change; it cannot approve
itself, expand grants, evade budgets or substitute for an evaluator receipt.

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
Overview and switcher tests cover metadata/projection and keyboard navigation. Tiled panes,
durable bridge messaging and external tmux attachment are not claimed implemented.

The overview/controller/TUI group passes 60 tests on Python 3.9 (15.337 seconds)
and Python 3.12 (14.006 seconds), including the real composer route. The new slice
has seven projection/CLI tests and one terminal test. No external terminal config,
account, model, or remote host was changed to run these checks.
