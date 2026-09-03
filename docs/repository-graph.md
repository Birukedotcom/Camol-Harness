# Dependency rail and repository graph

Status: specified, not implemented.

This subsystem gives the human and orchestrator two immediate answers:

1. What capabilities are actually usable in each box right now?
2. If this file, package, service, or deployment changes, what could be affected?

The implementation is Python-native and terminal-native. It does not require a web
application, and the durable graph model is independent of the eventual TUI library.

## 1. Terminal shape

The main view reserves the upper-right for a compact dependency rail:

```text
┌ CAMOL / ORCHESTRATOR ───────────── DOCKER ■  GIT ■  GCP □  MODELS ■■↓ ┐
│ objective, current state, approvals, cost                                  │
├ boxes ──────────────────────────────┬ repository graph ─────────────────────┤
│ ■ 1 builder     EXECUTING           │ api ──requires──▶ database            │
│ ■ 2 verifier    EVALUATING          │  │                 ▲                  │
│ □ 3 watcher     DISCONNECTED        │  └──deploys──▶ cloud-run              │
│ … 44 more; 2 need attention          │                                        │
├ selected evidence / dependency detail┴──────────────────────────────────────┤
│ camol>                                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

The main viewport is followed by a persistent workspace switcher:

```text
── WORKSPACES ─────────────────────────────────────────────────────
[0 ORCH] [BOXES 12/47] [!2] [1 ■ API] ▸[2 ■ VERIFY] [3 □ DEPLOY] [MORE]
Alt+0 orchestrator · Alt+1..9 visible shortcut · [/] filtered fleet cycle
```

The switcher remains visible while the graph is focused. Connection and selection
are separate signals: `■` means the box is connected and task-ready, while `▸`
(plus inverse-video styling where available) marks the workspace currently being
viewed. A box does not become ready merely because the user selects it. `API`,
`VERIFY`, and `DEPLOY` are example current assignments, not fixed slot roles.

Direct selection uses `Alt+0` for the orchestrator and `Alt+1` through `Alt+9` for the
currently visible recent or pinned box shortcuts. The numbers are not permanent box
identities. Left/right or `[`/`]` cycles through the current filtered fleet;
`/box <stable-id>` opens any box; `/boxes` searches and filters by task, state, target,
model, or attention; and mouse-capable terminals may click a label. Focus movement is
local UI state and emits no command to the worker. Attention badges for unread output,
a pending question, or an approval may decorate a label without replacing its
readiness glyph.

When a box is selected, the center workspace changes to a peer inspector:

```text
BOX 2 / VERIFIER · READ-ONLY · EVALUATING
────────────────────────────────────────────────────────────────────
live terminal output or selected evidence stream
────────────────────────────────────────────────────────────────────
terminal  context  tools  diff  evals  events  evidence
```

Peering is read-only by default. The inspector can copy output, follow a stream, and
open evidence, but it cannot send keystrokes into the worker. Interactive control is
an explicit takeover transition with an event-ledger entry and any required approval.
If a box disconnects, its tab remains traversable and the inspector renders the last
durable screen/checkpoint with a `STALE / DISCONNECTED` banner and observation time.

The dependency rail is deliberately small. It shows capabilities the active plan
cares about, not every installed package. Selecting an item opens its readiness vector:

```text
docker
  cli_present       READY       Docker 28.x
  daemon_reachable  READY       unix socket probe
  registry_auth     AUTH_REQUIRED
  image_required    DOWNLOADABLE project/image@sha256:...
  observed_at       2026-09-03T...
```

Statuses have both text and glyphs so meaning never depends on color:

```text
■ CONNECTED_AND_READY     □ PRESENT_NOT_CONNECTED     ↓ DOWNLOADABLE
! ACTION_REQUIRED        × UNAVAILABLE                ↻ PROBING_OR_REFRESHING
```

The ready mark is a solid white square in the default dark theme. On a light terminal
it uses the terminal foreground color so it remains visible. The square is the quick
signal; the expanded dependency detail still states the full status in text. The same
mark appears beside connected worker boxes and ready repository/runtime nodes.

"Installed" and "connected" are not synonyms. Docker can have a CLI with no daemon;
gcloud can have a configured project with expired OAuth; a local-model runtime can be
ready while the requested weights are absent. The rail renders the weakest required
layer and lets the user drill into the evidence.

## 2. Three graph layers

One giant graph would be visually impressive and operationally useless. Camol stores
related layers with shared identities and renders only the layer needed for the
question.

### Environment dependency graph

Represents box readiness:

```text
box -> executable -> daemon or endpoint -> auth scope -> artifact/model -> capability
```

Examples include Python, Node, Docker, Git, gcloud, cmux, SSH, local model servers,
downloaded model weights, cloud projects, and required SDKs. Credential nodes contain
references, scope fingerprints, and readiness only—never secret values.

### Source dependency graph

Represents the repository:

```text
repository -> workspace -> package -> module -> symbol
package -> builds -> target -> produces -> image
test -> verifies -> package
service -> requires -> package or infrastructure resource
deployment -> activates -> service revision
```

The default graph stops at package/module granularity. Symbol-level crawling is an
on-demand focused operation because rendering every symbol in a monorepo would create
noise and unnecessary cost.

### Execution overlay

Connects the static graph to Camol's control plane:

```text
task -> changes -> node
invariant -> guards -> node or edge
evaluation -> verifies -> node
failure -> observed_at -> node
box -> leased_to -> task
deployment identity -> activates -> artifact
```

This makes the graph operational. Selecting a changed package can show the tasks,
tests, agents, invariants, and deployment gates downstream of it.

## 3. Durable graph model

Each crawl creates an immutable `GraphSnapshot`:

```text
snapshot_id, root identity, source revision, dirty digest
crawler version, scanner versions, configuration digest
started_at, completed_at, status, warnings
node count, edge count, content hash
```

Nodes contain:

```text
node_id, kind, canonical locator, display name, scope
attributes, evidence status, observed_at, freshness policy
```

Edges contain:

```text
edge_id, source_node_id, target_node_id, kind
attributes, evidence status, evidence reference, observed_at
```

Initial node kinds are:

```text
repo | workspace | package | module | symbol | build_target | test
container | service | deployment | infrastructure | box | task
tool | daemon | endpoint | credential_ref | runtime | model_artifact
```

Initial edge kinds are:

```text
contains | declares | imports | requires | builds | produces | tests
deploys | activates | reads | writes | generates | configured_by
available_on | leased_to | changes | verifies | guards | observed_as
```

Canonical IDs come from stable locators and graph kind, not presentation labels.
Every inferred relationship is labeled `INFERRED`; manifest, syntax-tree, runtime,
and provider-readback edges keep their distinct epistemic status from the main spec.

SQLite remains the durable store. Graph snapshots, nodes, edges, crawl events, and
evidence references are normal control-plane records and are reconstructible after a
restart.

## 4. Crawl pipeline

The safe static pipeline is:

```text
discover repository root
  -> load ignore and boundary policy
  -> inventory files and manifests
  -> run matching read-only scanners
  -> normalize nodes and evidence-backed edges
  -> detect cycles and condense them
  -> compute impact indexes
  -> hash and commit the immutable snapshot
```

Static crawling never executes repository code. It does not source shell profiles,
import project Python modules, run package-manager hooks, or read `.env` values.
Runtime probes are separate plan-authorized tool calls whose results overlay the
static graph.

The crawler respects Git ignores plus Camol-specific exclusions, refuses symlinks
that escape the approved root, applies file-count and byte ceilings, and records
truncation as `OBSERVATION_INCOMPLETE` rather than presenting a partial graph as
complete.

### Scanner protocol

Scanners implement one small interface:

```python
class RepositoryScanner(Protocol):
    name: str
    version: str

    def supports(self, inventory: "RepositoryInventory") -> bool: ...
    def scan(self, context: "ScanContext") -> "GraphFragment": ...
```

The initial scanner set is:

- generic Git and filesystem structure;
- Python packaging and imports using `pyproject.toml`, lockfiles, and `ast`;
- JavaScript/TypeScript workspace and package declarations using package manifests,
  lockfiles, and `tsconfig` references;
- Dockerfile and Compose build/runtime relationships;
- Terraform module/resource/provider references;
- common CI workflow dependencies; and
- Camol runbook tasks, boxes, commands, evidence, and deployment identities.

Additional ecosystems add scanners without changing the graph or UI protocol. A
manifest edge is stronger than a filename heuristic, and a runtime observation is
kept separate from both.

Remote boxes perform the same crawl locally and return signed/hash-bound graph
fragments. The orchestrator merges fragments only after root, revision, scanner, and
packet identities match.

## 5. Graph queries

The interactive and non-interactive commands share one query layer:

```text
/deps
/deps inspect docker
/repo crawl [path]
/repo graph [overview|packages|imports|deploy|execution]
/repo impact <node>
/repo why <source> <target>
/repo cycles
/repo diff [snapshot-or-revision]
/repo export --format json|dot|graphml
```

`impact` walks downstream edges selected by an explicit policy. A source import, a
test relationship, and a production deployment relationship are not interchangeable,
so the result names every edge kind and evidence source.

`why` returns the shortest evidence-backed paths and explains ambiguity when several
paths have equal authority. It never invents causality from directory proximity.

`diff` compares immutable snapshots and reports added, removed, and changed nodes or
edges. Removed evidence remains in the historical snapshot.

## 6. Rendering and scale

The core commands always support plain text and JSON. The interactive TUI can use
Textual as an optional distribution dependency, while the graph algorithms and data
model remain ordinary Python.

The renderer uses a deterministic layered layout:

1. collapse each strongly connected component into a visible cycle group;
2. topologically layer the resulting DAG;
3. order peers by stable canonical ID;
4. cluster by repository, workspace, package, or service; and
5. retain positions between incremental snapshots when possible.

The default view shows the selected node, its first-order neighborhood, and collapsed
upstream/downstream groups. Expansion is deliberate. This prevents a large monorepo
from turning into an unreadable hairball.

The UI supports keyboard selection, edge/path explanation, ASCII fallbacks, graph
diffs, and links from every visible element to its evidence. DOT and GraphML export
allow richer external rendering without making Graphviz or a browser a runtime
requirement.

## 7. Post-v1 spatial build view

The future visualizer is about builds, not decorative repository browsing. It may
project the same graph into an almost-3D scene in which depth separates source,
build, runtime/deployment, and evaluation layers. Boxes occupy visible work areas;
task and artifact movement follows evidence-backed edges; failed or blocked build
paths remain selectable; and a time scrubber can reconstruct how the build changed.

This view is explicitly outside v1. Its camera, layout, and animation are local
presentation state. Selection never mutates a task, apparent proximity never creates
a dependency, and every object must link to the underlying snapshot or ledger event.
Camol does not need to choose a 3D rendering technology until the terminal graph and
real build telemetry prove what spatial questions are worth answering.

## 8. First implementation slice

1. Add graph snapshot, node, edge, and crawl-event records to SQLite.
2. Implement safe inventory plus Git, Python, package-manifest, Docker, and Camol
   runbook scanners.
3. Add `/deps`, `/repo crawl`, `/repo impact`, `/repo why`, `/repo cycles`, and JSON
   output before attempting the interactive renderer.
4. Add deterministic cycle condensation, layered layout, and compact text rendering.
5. Add the Textual dependency rail and graph pane as an optional TUI extra.
6. Overlay real task leases, changes, tests, invariants, and deployment identities.
7. Validate against the Camol repository, then a bounded Enrollment Hub snapshot,
   including one deliberately missing dependency and one real dependency cycle.

The acceptance test is not "a graph appeared." It is: Camol can truthfully show why
a capability is or is not ready, answer which verified work is downstream of a
change, and trace every rendered relationship back to evidence.
