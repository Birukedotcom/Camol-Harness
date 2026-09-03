# V0 verification and claim boundary

Camol V0 has two deliberately separate claims.

| Scope | Maturity | Evidence |
|---|---|---|
| Deterministic local-process control-plane kernel | `PROVISIONAL` until the final proof artifact is attached to the release; its individual safety properties are test-backed | Full unit/adversarial suite, deterministic N-box demo, detachable-daemon integration test, refinement test, and `scripts/run_v0_proof.py` archive |
| Claude CLI requesting the speculative Fable profile | `SPECULATIVE` | Contract, fake provider, provider-readiness and spend-preflight tests only; no owner-authorized live result is claimed |
| Remote workers, cloud deployment, voice operations, public benchmark campaigns | specified | No implementation or operational claim in V0 |

The deterministic dogfood edits Camol's own `camol/hillclimb.py` in an isolated box.
Its deliberately weak first candidate fails a protected oracle in a distinct verifier
worktree; the counterexample returns to the second turn, whose refined candidate is
applied to a new integration generation and evaluated again. The run then exports
every event/artifact and verifies that archive. It never changes the source checkout.

```bash
python3 scripts/run_v0_proof.py --output /absolute/path/outside/the/repository
```

`proof.json` names the exact harness/source commit, plan digest, accepted integration
revision, event counts, archive-manifest digest, and all claim limitations. The
adjacent `archive/` can be replayed with `python3 -m camol verify-export`.

## Required controlled failures

| Failure | Expected behavior | Executable evidence |
|---|---|---|
| Stale readiness between lease and launch | zero agent calls; typed `READINESS_STALE` wait | `tests.test_admission_scheduler.AdmissionSchedulerTests.test_expiry_between_lease_and_exec_rejects_without_starting` |
| Wrong/changed source revision | admission or pre-launch gate rejects it | `tests.test_admission_scheduler.AdmissionSchedulerTests.test_workspace_change_in_admission_to_exec_gap_makes_zero_agent_calls` and workspace base-revision tests |
| Frozen verifier failure | candidate becomes a counterexample, returns to refinement, and only a later green candidate integrates | `tests.test_evaluation.EvaluationLoopTests.test_failed_candidate_returns_counterexample_then_refines_and_integrates` |
| Daemon interruption after result write | packet-bound result is consumed; provider/agent is not called twice | process and Claude adapter recovery tests in `tests/test_runner.py` and `tests/test_claude_adapter.py` |
| Provider unavailable or unproved | no hosted lease | `tests.test_hosted_admission.HostedAdmissionTests.test_hosted_candidate_is_red_then_green_only_after_bound_preflight` |
| Attempted source-checkout write | sandbox denies it and source bytes remain unchanged | `tests.test_sandbox.SandboxTests.test_worker_cannot_write_source_or_integration_worktree` |
| Live orphan after supervisor restart | normal resume is denied; explicit force-stop revalidates PID/PGID/start identity | `tests.test_supervisor.SupervisorTests.test_live_orphan_blocks_resume_until_explicit_force_stop` |
| Builder changes protected evaluator | evaluator does not execute; candidate is rejected | `tests.test_evaluation.EvaluationLoopTests.test_builder_cannot_change_frozen_evaluator_asset` |
| Changed evaluator survives into a retry | readmission is denied before a second builder turn | `tests.test_evaluation.EvaluationLoopTests.test_changed_evaluator_stays_waiting_without_a_second_builder_turn` |
| Forked or duplicate integration receipt | deterministic replay rejects the event | `tests.test_evaluation.EvaluationLoopTests.test_projection_rejects_a_forked_or_duplicate_integration_receipt` |
| Worker commits to hide its diff | capture remains relative to the admitted base and replays the commit | `tests.test_workspace.WorkspaceManagerTests.test_worker_commit_cannot_hide_changes_from_candidate_salvage` |

## Comparative evidence

`camol bench-compare` accepts one strict `claude_direct` trial and one `camol_one` or
`camol_adaptive` trial. Task, source, evaluator, model/version, effort, tool policy,
and budget must match exactly. The report preserves quality, safety, tokens, cost,
time, retries, interventions, tool calls, recovery, and evidence completeness as a
vector. One pair is always labeled descriptive and `statistical_claim: false`.

No direct-Claude/Fable numbers ship in V0 because no matched, owner-authorized live
pair has been run. Adding fabricated or unmatched numbers would be weaker than
leaving this evidence cell explicitly empty.
