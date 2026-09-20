# Source-binding adversarial review

Review date: 2026-09-07. Scope: the next-checkpoint source-binding changes in
`source_binding.py` and their admission, runner, supervisor, workspace, approval,
and revision seams. This was a bounded independent review, not a public-workload
or model benchmark. Checks used disposable local Git repositories and local
supervisor processes; no provider calls or user workloads were launched.

## Findings and corrections

### Startup must prove which supervisor answered

Before correction, detached startup accepted any successful `ping` from the
state-directory socket. A fixture with a newly spawned child PID of `999999` and
an existing endpoint reporting PID `424242` and an unrelated run returned
`started: true` for PID `424242`. The expected source binding had been persisted,
but there was no proof that the answering supervisor had adopted it. An existing
unbound leader or a race with another start could therefore produce a dishonest
successful-start result. A prior client-side attachment check was insufficient
to close that race.

Startup now requires the responding PID to match the live child and then checks
the control-plane plan response against the exact run ID, frozen plan digest,
and required full source binding. The regression is
`test_spawn_never_accepts_a_different_existing_daemon_as_bound_startup`.
An independent real-process recheck started an unbound draft supervisor, then
attempted a source-bound second launch into that state directory: the second
launch was rejected instead of reporting the existing unbound process as a
successful bound launch.

### Source-bound approval must validate the whole approval subject

Before correction, approval replay checked the source-binding digest but not
the approved plan digest, transition state, or registered-worker identity. Two
synthetic replay cases demonstrated the gap: a correct source digest plus the
wrong plan digest produced `ready`, and an approval whose actor and owner were
the registered worker `builder` also produced `ready`.

This permissive approval behavior predated source binding in legacy unbound
replay. The correction applies strict validation to the new source-bound path
without rewriting old valid event streams or plan digests. Source-bound approval
now requires the exact draft plan, exact source-binding digest, a nonempty owner,
and neither a registered-worker actor nor a registered-worker owner. The public
approval method enforces the same worker exclusion. The regression is
`test_source_bound_approval_replay_rejects_wrong_plan_and_worker_owner`.

These are replay/control-plane integrity checks. They do not imply that an
arbitrary worker can write the private event database, and an owner-name string
is not remote identity authentication. The trusted local control plane and its
filesystem boundary remain part of the security model.

## Other inspected boundaries

- Source identity includes tracked bytes rather than trusting Git's clean-status
  result alone; pinned worktree creation rechecks source identity.
- The event binds run, plan, and source. The sidecar cannot silently substitute
  for a missing source event, and a missing sidecar can be reconstructed from the
  authoritative event.
- Admission and worker launch recheck the required source. Launch also rechecks
  after an awaited capacity grant, so that wait cannot silently reuse an earlier
  source observation.
- Immutable successor revisions preserve source lineage and use their retained
  integration baseline. This does not turn an old evaluator receipt into proof
  for a changed plan or artifact.
- The detached daemon uses isolated interpreter bootstrapping so a source
  checkout's `camol` package or `sitecustomize.py` is not authoritative daemon
  code. The detached-launch regression includes those import traps.

## Verification and limits

Independent post-fix check: `python3 -m unittest tests.test_source_binding -q`
passed all 8 tests on Python 3.9 (20.053 seconds); `git diff --check` was clean.

The focused source-binding suite covers ordinary bound execution and replay,
source mutation before admission, mutation during materialization, successor
inheritance, required-binding failures, false attachment, approval tampering,
and an actual detached restart with a missing sidecar and changed source.
This record describes a local checkpoint review; remote CI and the complete
repository suite are separate verification gates. It does not claim protection
against a malicious same-user owner rewriting all trusted state, nor an atomic
filesystem snapshot against arbitrary external writers.
