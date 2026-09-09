# Camol platform portability specification

Status: `SPECULATIVE`

Scope: native macOS, native Linux, native Windows, and WSL-hosted Linux.

Authority: this document refines `SPEC.md`; it does not override the product,
readiness, authority, evidence, or trust contracts defined there. If the two
documents conflict, `SPEC.md` wins until both are deliberately amended.

Evidence posture:

- the current macOS/POSIX implementation is `SOURCE_OBSERVED` and partially
  exercised by the existing test suite;
- native Windows behavior is `LOCAL_OBSERVED` from Python 3.13 and Git for
  Windows on 2026-09-04;
- Linux behavior is `SOURCE_INFERRED` until the required Linux CI and sandbox
  evidence in this document exist; and
- no platform may be labeled `BACKED` or `PROVEN` from this specification alone.

## 1. Purpose

Camol's deterministic kernel should behave consistently across supported host
platforms without pretending that unlike operating-system security primitives
are interchangeable. This specification defines:

- the vocabulary used to describe platform support;
- the invariants every implementation must preserve;
- the platform boundary that isolates operating-system-specific behavior;
- the intended macOS, Linux, Windows, and WSL implementations;
- schema, packaging, diagnostics, CI, and migration requirements; and
- the acceptance gates for progressively stronger support.

The specification intentionally separates basic operability from enforced
isolation. A terminal UI that opens and launches a process is useful, but it is
not evidence that filesystem, process, network, or credential boundaries are
enforced.

## 2. Goals

1. Install and launch Camol with a truthful support status on every advertised
   platform.
2. Keep the event ledger, runbook digests, approvals, leases, evidence, and
   deterministic replay platform-neutral.
3. Support durable detach, reattach, cancellation, crash recovery, and
   single-writer supervision using native host primitives.
4. Preserve source-checkout isolation and Git worktree semantics across path,
   line-ending, executable, and filesystem differences.
5. Make the effective security boundary visible in readiness and evidence.
6. Provide an enforcing Linux sandbox backend.
7. Provide a useful native-Windows `developer_trusted` mode before claiming a
   native-Windows enforcing sandbox.
8. Treat WSL as an explicit Linux execution target on a Windows host, not as
   evidence of native-Windows support.
9. Test supported behavior continuously on macOS, Ubuntu, and Windows.

## 3. Non-goals

- Making identical operating-system APIs appear identical in code.
- Treating a Windows Job Object as a filesystem or network sandbox.
- Treating a container, VM, WSL distribution, or running process as ready
  without the normal Camol readiness receipts.
- Automatically weakening a requested trust tier when its backend is missing.
- Supporting arbitrary shell command strings. Camol continues to execute typed
  argument vectors without a shell unless a separately modeled interpreter is
  explicitly part of the command.
- Moving live state directories between operating systems.
- Promising every terminal emulator, filesystem, network share, or Git filter
  driver in the first portability milestone.
- Using platform support as evidence that any provider adapter is authenticated,
  funded, or ready.

## 4. Support vocabulary

Platform claims are cumulative and scope-qualified.

| Level | Required meaning |
|---|---|
| `unsupported` | Camol refuses early with a specific reason and supported alternative. |
| `installable` | Packaging resolves and installs without an import-time crash. |
| `launchable` | `camol` opens, reports platform capability, and supports safe inspection. |
| `functional` | Sessions, planning, validation, Git inspection, and documented local operations pass platform CI. |
| `developer_trusted` | Workers may run with explicit unsandboxed authority and truthful labeling; lifecycle containment is still enforced. |
| `sandboxed` | The selected backend enforces the declared filesystem, process, environment, network, and credential policy. |
| `backed` | The scoped platform level has passed clean-start, failure, recovery, and adversarial acceptance suites with retained evidence. |

These levels do not replace the product maturity terms `SPECULATIVE`, `MAPPED`,
`BACKED`, and `PROVEN`. For example, `Windows / developer_trusted / BACKED` is a
valid scoped claim; `Windows supported` is too vague.

### 4.1 Target support matrix

| Host/runtime | First target | Later target | Explicit limitation |
|---|---|---|---|
| macOS | preserve current functional and sandboxed behavior | backed recovery evidence | Seatbelt availability and policy remain capability-probed |
| Linux | functional plus `developer_trusted` | Bubblewrap-backed `sandboxed` | no silent fallback when user namespaces or Bubblewrap are unavailable |
| Windows | functional plus `developer_trusted` | separately designed native `sandboxed` target | Job Objects provide lifecycle containment, not resource authority |
| WSL 2 | Linux functional and sandboxed semantics inside the distribution | backed Windows-host/Linux-target workflow | state, paths, tools, and evidence identify both host and execution OS |

## 5. Cross-platform invariants

The following are normative. A platform adapter that cannot satisfy one must
return a typed non-runnable reason; it must not approximate the invariant and
continue.

### 5.1 Authority and truth

- Requested trust tier is immutable after plan approval.
- A missing sandbox backend yields `POLICY_DENIED` or an equivalent typed wait.
- `developer_trusted` is explicit in the runbook, grant, readiness receipt,
  invocation record, and user interface.
- Platform capability does not imply provider, network, credential, model, or
  deployment readiness.
- Every platform-specific fact used for admission is recorded with its probe
  definition digest and expiry.

### 5.2 Durable single-writer control

- Exactly one supervisor may own a state directory.
- Ownership is represented by a lock held for the supervisor lifetime, not by a
  stale PID file alone.
- A crashed process releases the operating-system lock.
- Reattach authenticates the live endpoint and verifies the expected run and
  plan digests before accepting it.
- Stale endpoints, lock records, and PID reuse never become proof of ownership.

### 5.3 Process lifecycle

- Every launched worker belongs to one platform-owned containment unit.
- Camol can identify the original process instance, not only its reusable PID.
- Timeout, cancellation, and force-stop address the whole descendant tree.
- Graceful termination is bounded; hard termination follows when the grace
  period expires.
- Camol never signals a process whose recorded instance identity no longer
  matches.
- Detaching the client does not unintentionally terminate the supervisor.

### 5.4 Filesystem and state security

- State is outside the source repository and Git common directory, lexically
  and after platform-native canonicalization.
- Control credentials and authoritative state are readable and writable only
  by the owning user and required system principals.
- Symlinks, junctions, mount points, reparse points, aliases, and case folding
  cannot escape an allowed root.
- Atomic replacement never silently becomes a non-atomic copy across volumes.
- Source and integration worktrees remain protected from worker writes.
- Platform permission evidence records the mechanism used: POSIX mode/UID or
  Windows owner SID/DACL. One must never be inferred from the other.

### 5.5 Command execution

- Commands are structured argument vectors.
- No command is passed through an implicit shell.
- Interpreter-backed scripts name their interpreter explicitly.
- Executable resolution is deterministic under a sanitized environment.
- Platform aliases, filename extensions, and path case are included in the
  resolved command identity.
- Environment sanitization retains only documented platform essentials and
  never leaves unresolved variable syntax as a path.

### 5.6 Evidence and replay

- Kernel event schemas remain platform-neutral wherever semantics are neutral.
- Platform-dependent receipts name the host OS, execution OS, architecture,
  backend, and backend version.
- Replay produces the same logical state from the same event sequence on every
  supported platform.
- Evidence never upgrades a platform maturity claim outside the exact platform,
  backend, filesystem, and trust-tier scope that produced it.

## 6. Platform boundary

Operating-system behavior must sit behind narrow services. Kernel, planning,
schema, readiness, event, and evidence code must not branch directly on
`os.name` or `sys.platform` except at composition time.

The required services are:

| Service | Responsibility |
|---|---|
| `StatePaths` | Select user state/runtime roots and validate same-volume atomicity requirements. |
| `PrivateFiles` | Create, validate, replace, and remove owner-private files/directories. |
| `LeaderLock` | Provide crash-released, non-blocking exclusive ownership. |
| `ControlTransport` | Start and connect to an authenticated local control endpoint. |
| `ProcessController` | Spawn, fingerprint, contain, gracefully stop, and hard-stop a process tree. |
| `ExecutableResolver` | Resolve platform executables and explicit script interpreters without a shell. |
| `PathIdentity` | Canonicalize path identity and detect link/reparse/mount escape. |
| `SandboxBackend` | Enforce the approved resource policy or refuse the requested trust tier. |

Each service has platform-independent contract tests and platform-specific
integration tests. Platform implementations may share helpers, but they may not
leak weaker semantics through a common interface.

### 6.1 Composition

Startup selects one immutable platform bundle:

```text
PlatformBundle
  host_identity
  state_paths
  private_files
  leader_lock
  control_transport
  process_controller
  executable_resolver
  path_identity
  sandbox_backends[]
```

Selection produces a capability report before interactive state is mutated. A
missing required service prevents launch of the affected command while allowing
safe commands such as `camol version` and `camol platform doctor`.

## 7. POSIX implementation

The initial POSIX bundle covers macOS and Linux while keeping their sandbox
backends distinct.

### 7.1 Locking and control

- `fcntl.flock` remains the POSIX leader-lock implementation.
- Unix-domain stream sockets remain the POSIX control transport.
- Socket and token paths are owner-private and checked for link substitution.
- Long Unix-socket paths may use an owner-private hashed runtime directory while
  authoritative state remains under the state root.

### 7.2 Processes

- Workers and the supervisor use dedicated POSIX sessions/process groups.
- Identity combines PID with a stable process-start marker.
- Group termination uses bounded `SIGTERM` followed by `SIGKILL`.
- Missing `ps` or another required observation primitive produces a typed
  capability failure before launch.

### 7.3 Private state

- Private directories require owner UID and mode `0700`.
- Private files require owner UID, regular-file identity, and mode `0600`.
- `O_NOFOLLOW` is used when available; lack of the flag is recorded and covered
  by pre/post-open identity checks.

## 8. Linux implementation

Linux uses the POSIX lock, control, process, and permission services. Its main
new work is an enforcing sandbox and backed distribution coverage.

### 8.1 Developer-trusted mode

Linux `developer_trusted` may use the existing local process backend once the
full Linux functional suite passes. It still requires process-tree containment,
private state, clean-workspace validation, and truthful receipts.

### 8.2 Enforcing sandbox

Bubblewrap is the primary local Linux sandbox target for the first enforcing
backend.

The backend must:

- create a new mount namespace and process namespace;
- expose only declared read-only and read-write roots;
- provide a minimal `/proc`, `/dev`, and runtime view required by the command;
- deny network with a network namespace unless unrestricted network is
  explicitly granted;
- pass only the approved environment variables;
- prevent access to undeclared credential and home directories;
- preserve the process-controller's ability to terminate the entire sandbox;
  and
- report its executable identity, version, kernel capabilities, user-namespace
  availability, and effective policy digest.

If Bubblewrap or required kernel namespace support is absent, a `sandboxed`
task is non-runnable. Camol may offer `developer_trusted` only if that tier was
approved in the plan. Docker, Podman, nsjail, and remote VM backends may be added
later as separate execution-target/sandbox adapters; they are not automatic
substitutes.

## 9. Native Windows implementation

Native Windows support must not emulate POSIX permission and process semantics
with misleading mode bits or PIDs.

### 9.1 State paths

- Default durable state root: `%LOCALAPPDATA%\Camol\State`.
- Default ephemeral runtime root: `%LOCALAPPDATA%\Camol\Runtime` unless a
  validated owner-private temporary root is required.
- `CAMOL_STATE_HOME` remains the explicit override.
- Relative paths, unresolved `%NAME%` fragments, device paths, alternate data
  streams, reserved DOS names, and unsupported UNC/network roots are rejected
  for authoritative state.
- Long-path handling uses canonical Win32 path identity internally without
  leaking incompatible path forms to external tools.

### 9.2 Private files and path identity

Windows private-state proof uses the current user SID, file owner, and DACL.
POSIX-looking `0600` or `0700` bits are not evidence.

The implementation must:

- create state objects with an explicit protected DACL;
- allow the owner and required Windows system principals only;
- reject unexpected inherited or explicit access entries;
- open security-sensitive paths by handle and verify final path identity;
- detect symlinks, junctions, mount points, and other reparse points;
- define and test hard-link behavior for tokens, databases, and replacement
  targets; and
- keep atomic replacements on one volume.

Windows Developer Mode or administrator rights may be used by dedicated
security tests, but ordinary Camol operation must not require elevated rights.

### 9.3 Leader lock

The Windows leader lock uses an OS file-handle lock with crash-release semantics
such as `LockFileEx`. The handle remains open for the supervisor lifetime.

The lock record may contain diagnostics, but the record is not ownership proof.
Acquisition must distinguish contention from access denial and malformed state.

### 9.4 Control transport

The target native transport is a user-scoped Windows named pipe with an explicit
owner-only DACL. The control protocol framing, authentication token, request IDs,
size limits, and digest verification remain unchanged.

An authenticated `127.0.0.1` TCP transport may exist as an experimental
`developer_trusted` fallback only when:

- it binds loopback exclusively on an ephemeral port;
- the endpoint record is owner-private;
- every request authenticates the existing high-entropy token;
- endpoint generation prevents stale-port confusion; and
- readiness labels the transport as an experimental fallback.

Loopback TCP is not the default and cannot independently satisfy the native
Windows `sandboxed` milestone.

### 9.5 Process lifecycle

Every worker process tree is assigned to a Windows Job Object before it may run
uncontained. The job uses kill-on-close and denies silent breakaway unless an
explicit backend contract requires and records it.

Process identity combines PID with the process creation timestamp obtained from
a process handle. Graceful stop may use a documented application control message
or console control event; force-stop terminates the job. `taskkill` output and a
reusable PID alone are not authoritative lifecycle evidence.

The supervisor's own detach behavior must be tested independently from worker
jobs so closing the terminal does not kill an active run.

### 9.6 Executables and scripts

- Native executable resolution honors Windows path case and validated `PATHEXT`
  behavior.
- `.exe` and `.com` programs may be launched directly.
- `.cmd` and `.bat` files require an explicit `cmd.exe` interpreter contract;
  they are never silently passed to a shell.
- PowerShell scripts require an explicit `pwsh` or `powershell.exe` argument
  vector with non-interactive/no-profile policy frozen in the adapter.
- Python scripts require an explicit Python interpreter unless installed as a
  verified console entry point.
- Resolved executable path, extension, file identity, interpreter, and command
  digest are recorded.

### 9.7 Initial Windows trust boundary

The first native Windows release target is `developer_trusted`. Job Objects,
ACLs, control authentication, and worktree isolation are mandatory, but they do
not make the worker sandboxed.

A later native enforcing backend must independently solve filesystem, process,
environment, network, and credential isolation. Candidate technologies include
restricted tokens/AppContainer, Windows Sandbox, a Hyper-V utility VM, or a
dedicated container target. No candidate is selected by this specification,
and no Windows `sandboxed` claim is permitted until an adversarial backend spec
and evidence suite are approved.

## 10. WSL contract

WSL is modeled as a Linux execution target hosted by Windows.

- Camol records `host_os=windows`, `execution_os=linux`, distribution identity,
  WSL version, kernel version, and mount type.
- Linux virtual environments are never shared with native Windows Python.
- Authoritative state defaults to the Linux filesystem, not `/mnt/c`.
- A Windows checkout mounted under `/mnt/c` may be inspected only after path,
  permission, Git line-ending, performance, and atomicity probes pass.
- Cross-boundary process termination must prove that the Linux process tree has
  stopped; terminating only `wsl.exe` is insufficient evidence.
- A WSL sandbox claim still requires the Linux sandbox backend. WSL by itself is
  an execution boundary, not the declared per-task sandbox policy.

## 11. Git and workspace portability

Git worktrees remain the workspace-isolation mechanism, but their observation
and mutation contracts must be platform-aware.

### 11.1 Safe configuration

Camol continues to neutralize executable Git configuration such as hooks,
fsmonitor, pagers, SSH commands, and interactive credential prompts. It must
also preserve or deliberately reproduce the checkout's safe line-ending
semantics.

Before declaring a repository clean, Camol must safely observe and validate the
effective scalar values that affect worktree normalization, including
`core.autocrlf`, `core.eol`, and `core.safecrlf`. Operational Git commands then
run with global/system configuration disabled plus explicit validated scalar
values and the existing no-execution overrides. A system-created Windows
checkout must not become dirty merely because the safety environment forgot
the system line-ending policy.

External clean/smudge/process filters remain untrusted executable behavior. A
repository that requires an unapproved filter is non-runnable with a typed
reason; Camol must not execute the filter during a read-only probe.

### 11.2 Path and filename rules

- Repository-relative paths use `/` in schemas and Git-facing records.
- Host-native paths are converted only at the execution boundary.
- Containment comparisons use platform path identity, not string prefixes.
- Windows comparisons account for case folding, drive identity, UNC roots,
  reserved names, trailing spaces/dots, and reparse points.
- Worktree branch names remain platform-neutral Git refs.
- Unsupported filenames produce a preflight failure before any worktree is
  created.

### 11.3 Filesystem capability probe

Admission records filesystem type and required capabilities: atomic replace,
advisory lock behavior, executable semantics, symlink/reparse behavior,
case sensitivity, and maximum practical path length. Network filesystems and
cross-OS mounts require explicit backed evidence rather than inheritance from a
local-disk claim.

## 12. Terminal behavior

Textual is expected to supply most cross-platform rendering and input behavior,
but terminal integration remains platform-tested.

- Core UI tests use Textual's application test harness where possible.
- POSIX end-to-end terminal tests use PTY/`termios` fixtures.
- Windows end-to-end terminal tests use ConPTY-compatible fixtures.
- Ctrl+C, safe detach, resize, multiline input, Unicode fallback, and reattach
  are tested on each platform.
- Test-only POSIX imports are guarded or isolated so they do not prevent Windows
  test discovery.
- Failure to support one terminal feature selects a documented fallback mode;
  it does not weaken kernel authority.

## 13. Schema and evidence changes

Existing logical event schemas remain unchanged unless a platform concept is
actually required for replay. The following records gain versioned platform
fields or platform-specific nested records.

### 13.1 Platform capability receipt

```json
{
  "schema": "camol.platform_capability",
  "schema_version": 1,
  "host_os": "windows|linux|macos",
  "execution_os": "windows|linux|macos",
  "architecture": "normalized architecture",
  "platform_version": "redacted bounded version",
  "services": {
    "leader_lock": "backend-id",
    "control_transport": "backend-id",
    "process_controller": "backend-id",
    "private_files": "backend-id",
    "path_identity": "backend-id"
  },
  "sandbox_backends": ["backend-id"],
  "observed_at": "timestamp",
  "expires_at": "timestamp",
  "definition_digest": "sha256:..."
}
```

Values are bounded, redacted, and normalized before persistence.

### 13.2 Control endpoint record

The current socket-path assumption becomes a tagged endpoint:

```text
transport: unix_socket | windows_named_pipe | tcp_loopback
address: transport-specific bounded identifier
generation: random endpoint generation
owner_identity_digest: non-reversible owner binding
created_at: timestamp
```

The bearer token remains in its separately protected token file and never
appears in the endpoint record or normal evidence.

### 13.3 Process invocation record

Invocation records replace POSIX-only `pgid` assumptions with:

```text
controller: posix_process_group | windows_job
process_id: integer
process_instance: backend-defined creation fingerprint
containment_id: non-secret backend identifier
```

Schema migration preserves old POSIX records and never fabricates Windows
identity evidence for them.

## 14. Packaging and startup

- The console entry point performs a lightweight platform-capability check
  before importing unsupported platform modules.
- Unsupported commands fail with a concise reason, the missing capability, and
  a supported alternative such as WSL; raw `ModuleNotFoundError` is forbidden.
- Platform-specific dependencies use environment markers or named extras and
  are pinned within compatible ranges.
- Source distributions and wheels declare accurate Python and platform support.
- Installation success is tested separately from launch success.
- README installation commands are provided for PowerShell, POSIX shells, and
  WSL without mixing virtual environments.
- `camol version` and `camol platform doctor --json` remain available even when
  execution capabilities are incomplete.

## 15. Testing and CI

CI is required before any new platform claim. The initial matrix is:

```text
ubuntu-latest  x minimum supported Python, latest supported Python
macos-latest   x minimum supported Python, latest supported Python
windows-latest x minimum supported Python, latest supported Python
```

Platform CI is divided into four suites:

1. `core`: schema, replay, planning, readiness, redaction, and pure policy.
2. `platform-contract`: common service contracts against the selected bundle.
3. `integration`: Git worktrees, supervisor detach/reattach, process trees,
   terminal behavior, storage, and recovery.
4. `security`: adversarial path escape, link substitution, permission/ACL,
   endpoint impersonation, PID reuse, sandbox escape, and secret persistence.

Critical platform-contract or security tests may not be silently skipped. A
skip requires a named missing capability and prevents the corresponding support
level from being published.

### 15.1 Required Windows fixtures

- a checkout created with `core.autocrlf=true` remains clean under Camol probes;
- spaces and non-ASCII characters in user/repository paths;
- case-colliding and reserved-name rejection;
- junction, symlink, mount-point, and hard-link attacks;
- private DACL creation and unauthorized-user denial;
- named-pipe impersonation/stale endpoint attempts;
- child and grandchild termination through a Job Object;
- supervisor survival after terminal close;
- forced termination followed by replay and reattach;
- `.exe`, explicit Python, explicit PowerShell, and rejected implicit batch
  execution; and
- SQLite WAL, atomic replacement, antivirus-style transient sharing violations,
  and retry bounds.

### 15.2 Required Linux fixtures

- POSIX owner/mode and symlink substitution attacks;
- Unix-socket impersonation and stale socket recovery;
- process-group descendant termination and PID-reuse refusal;
- Bubblewrap read/write/network denial with positive and negative controls;
- missing Bubblewrap/user-namespace capability yields a typed refusal;
- clean Git worktrees under representative line-ending policies; and
- supervisor detach, crash, restart, replay, and reattach.

### 15.3 Cross-platform replay fixture

One canonical platform-neutral event log is replayed on all three native CI
systems. Logical state, digests, terminal status, and completion verdict must
match byte-for-byte after normalization of explicitly non-authoritative display
paths.

## 16. Acceptance gates

### Gate P0 — truthful current behavior

- Windows installation no longer ends in an unhandled POSIX import error.
- Platform support is documented and emitted by `platform doctor`.
- Unsupported trust tiers fail closed with actionable diagnostics.
- No current macOS behavior regresses.

### Gate P1 — extracted POSIX platform bundle

- Existing POSIX behavior uses the platform service interfaces.
- macOS tests pass without weakening security assertions.
- Linux core and developer-trusted suites pass.
- Event replay and persisted V0 state remain compatible.

### Gate P2 — Linux enforcing backend

- Bubblewrap policy mapping passes positive and adversarial tests.
- Missing capability never falls back silently.
- Detach/recovery, cancellation, and evidence survive controlled failures.
- Linux `sandboxed` may advance from `SPECULATIVE` only with retained CI and
  independent review evidence.

### Gate P3 — native Windows functional mode

- Windows uses private ACL-backed state, a crash-released leader lock, a local
  authenticated control endpoint, and Job Object containment.
- Planning, doctor, worktree, developer-trusted execution, cancellation,
  detach/reattach, replay, and TUI/line fallback pass Windows CI.
- Clean repositories remain clean across supported Git line-ending settings.
- No test or runtime command writes into the source checkout except an approved
  worker operating in its isolated worktree.
- The UI and receipts state `developer_trusted`; no sandbox claim is made.

### Gate P4 — native Windows enforcing isolation

- A separate backend design names and enforces filesystem, process,
  environment, network, and credential boundaries.
- Adversarial tests demonstrate deny behavior, escape resistance, cleanup, and
  truthful failure modes.
- Independent security review covers the backend and Windows path/ACL model.
- Only then may native Windows advertise `sandboxed`.

### Gate P5 — backed distribution

- Signed artifacts, reproducible builds, upgrade/state migration tests, and
  rollback documentation exist.
- Each published platform/support level has clean install, real workflow,
  controlled failure, recovery, and retention evidence.
- Documentation and release metadata contain no broader claim than the evidence.

## 17. Migration and compatibility

- Existing POSIX state remains readable through schema-versioned migration.
- Migration writes a new record atomically and retains the old record until the
  new one is validated.
- Active supervisors are never migrated in place.
- Unix socket paths do not become Windows endpoints automatically.
- Platform-specific process records cannot be resumed on a different OS; they
  become typed interrupted/orphan-review records.
- Cross-platform project identity uses repository and revision identity, not
  raw absolute-path equality.
- Moving a workspace between native Windows and WSL requires a new readiness
  receipt and filesystem capability probe.

## 18. Diagnostics

`camol platform doctor` is read-only and reports:

- normalized host and execution platform;
- state-root selection and privacy mechanism;
- lock, control, process, path, and sandbox backend availability;
- Git executable and line-ending policy;
- filesystem capability summary;
- terminal mode;
- supported trust tiers; and
- typed blockers with human actions.

It never enables Windows features, changes ACLs outside a disposable probe
directory, installs Bubblewrap/WSL, edits Git configuration, launches an agent,
or sends a provider request.

## 19. Current gap inventory

The inventory below describes the source observed on 2026-09-04 and is not a
permanent architectural requirement.

| Gap | Current location/evidence | Required destination |
|---|---|---|
| unconditional POSIX lock import | `camol/supervisor.py` imports `fcntl` | platform `LeaderLock` composition |
| Unix-socket-only control | `asyncio.start_unix_server/open_unix_connection` | tagged `ControlTransport` |
| POSIX-only process identity/termination | `ps`, `getpgid`, `killpg`, POSIX signals | platform `ProcessController` |
| POSIX modes treated as privacy proof | `chmod(0600/0700)`, `fchmod`, UID checks | `PrivateFiles` with POSIX mode or Windows ACL evidence |
| macOS-only enforcing sandbox | `MacOSSandboxBackend` is the only enforcing backend | Bubblewrap Linux backend; future Windows backend |
| POSIX-only terminal fixtures | tests import `fcntl`, PTY, and `termios` | Textual core tests plus PTY/ConPTY integration fixtures |
| Unix-oriented state default | non-macOS uses `~/.local/state/camol` | native Windows LocalAppData policy |
| Windows Git normalization mismatch | focused Windows tests report clean fixtures as dirty | safe observed scalar normalization policy |
| unresolved Windows environment/path behavior | Windows diagnostic run created a literal `%SystemDrive%` path | reject unresolved variables and add regression fixture |
| inaccurate install signal | editable install succeeds on Windows; launch crashes | capability-aware startup and packaging metadata |
| no CI matrix | no `.github/workflows` directory | required three-OS matrix |

Focused native-Windows evidence at the time of this specification:

- Python exposed no `fcntl`, `AF_UNIX`, `os.getuid`, `os.getpgid`, or
  `os.killpg` support required by the current supervisor;
- a focused storage/session/workspace run completed 18 tests with 4 passes,
  2 failures, and 12 errors;
- permission assertions observed Windows mode semantics rather than owner-only
  security;
- symlink fixtures failed without the required Windows privilege;
- multiple Git worktree fixtures were classified dirty under the sanitized Git
  environment; and
- the broad Windows test run left a child test process requiring cleanup.

These observations establish the need for the platform boundary. They do not
define the final implementation or qualify any Windows support claim.

## 20. Deferred decisions

The following remain deliberate design decisions, not permission to weaken the
contracts above:

1. The Python/Win32 library used for named pipes, ACLs, Job Objects, and file
   identity (`pywin32`, a narrower dependency, or maintained internal bindings).
2. The minimum supported Windows release and filesystem set.
3. Whether experimental loopback TCP is shipped or remains test-only.
4. The native Windows enforcing-sandbox technology and deployment model.
5. Additional Linux sandbox backends for hosts without Bubblewrap/user
   namespaces.
6. Whether Python 3.9 remains the minimum after platform dependencies and CI
   runners are selected.

Each decision requires a short decision record containing alternatives,
security consequences, migration impact, and the acceptance tests it changes.

## 21. Definition of done

Platform portability is not done when `camol` merely starts. A scoped platform
level is done when:

1. its advertised services satisfy the common contracts;
2. its support and trust labels are visible and accurate;
3. required CI and adversarial suites pass without critical skips;
4. detach, crash, cancellation, restart, and reattach preserve authoritative
   state;
5. source isolation and process-tree cleanup are demonstrated;
6. security evidence names the actual platform mechanisms used;
7. packaging, installation, upgrade, and rollback are documented and tested;
8. retained evidence supports the exact maturity claim; and
9. no broader platform claim appears in the CLI, README, package metadata, or
   release notes.
