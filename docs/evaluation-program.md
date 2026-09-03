# Evaluation and benchmark program

Status: specified, not implemented.

Camol uses coding benchmarks as repeatable outside pressure on the harness, not as a
replacement for the active plan's invariants or the target repository's own tests.
The program must answer two different questions:

1. Did this candidate safely improve the current build?
2. Is Camol becoming a better general software-engineering harness over time?

## 1. Evaluation ladder

Running an entire public benchmark after every agent turn would be slow, expensive,
and prone to benchmark overfitting. Camol therefore applies benchmark pressure at
four cadences:

| Cadence | Required evaluation |
|---|---|
| Every agent turn | Plan invariants, changed-package tests, type/static checks, and failure-specific regression cases. |
| Every candidate hill climb | The turn gate plus a small, pinned coding-suite canary selected before the run. Compare candidate and baseline under matched conditions. |
| Nightly or scheduled campaign | A stratified, rotating external benchmark slice plus Camol-native restart, delegation, integration, and deployment scenarios. |
| Release or milestone | Full pinned release cohort, multiple trials where stochasticity matters, long-horizon builds, failure injection, and human review of the resulting evidence. |

The canary is part of each meaningful candidate iteration, but it is not silently
resampled until the candidate passes. Its task IDs, selection rule, and budget are
frozen before execution. A failed canary returns the candidate to refinement without
discarding its evidence.

## 2. Initial suite registry

The registry accepts adapters rather than embedding one benchmark into the kernel.
The initial targets are:

- [SWE-bench Verified](https://www.swebench.com/SWE-bench/) for human-reviewed,
  repository-level issue resolution. Multilingual tasks can later test whether the
  harness generalizes beyond Python.
- [Terminal-Bench](https://www.tbench.ai/) for end-to-end work in real terminal
  environments, including builds, configuration, services, and system tooling.
- [Terminal-Bench Challenges](https://www.tbench.ai/news/terminal-bench-challenges)
  for token-intensive, long-horizon build campaigns. This is a milestone lane, not
  a per-turn check.
- [SWE-Lancer](https://openai.com/index/swe-lancer/) for economically meaningful
  feature work and engineering decisions. Camol should use only a reproducible,
  locally runnable public split whose current terms permit the run.
- [RE-Bench](https://metr.org/AI_R_D_Evaluation_Report.pdf) or MLE-bench later when
  Camol begins claiming competence on ML research and model-building workflows.

Small function-generation suites may be useful as adapter smoke tests, but they are
not primary Camol metrics because they do not exercise planning, terminal work,
delegation, recovery, or integration.

Public suites are complemented by a Camol-owned, technology-diverse workflow corpus.
One initial long-run scenario family comes from sanitized Buckeye work patterns:
inspect a repository,
let the plan choose an appropriate N-box topology within a frozen resource envelope,
implement and integrate a change, forward-deploy to an isolated GCP target, observe a
voice-agent boundary, diagnose a seeded failure, recover from an orchestrator restart,
and produce replayable evidence. Additional profiles cover local repair,
multi-language builds, multi-service integration, and local-model work. This internal
corpus measures product fit that a public leaderboard cannot.

## 3. Benchmark adapter contract

Each suite implements the same adapter lifecycle:

```text
resolve -> materialize -> validate_gold -> allocate -> run -> grade -> normalize
  -> retain evidence -> teardown
```

`validate_gold` proves that the pinned task and grader work in the selected
environment before spending model budget. The adapter must emit an execution manifest:

```text
suite, suite version, harness commit, dataset split and digest
task IDs and predeclared selection rule
base repository revision and container/image digests
grader and public-test digests
agent, model, effort, sampling, and context configuration
tool policy, network policy, secrets policy, and authority grants
token, cost, wall-time, attempt, and concurrency budgets
host architecture, runtime versions, and relevant hardware allocation
trial/seed identity, baseline run ID, and Camol source revision
```

Evaluation cache keys include the candidate artifact digest as well as task, suite,
environment, and evaluator digests. A reused run ID must never return a verdict for a
different patch.

Every evaluator is visible to authorized humans. Worker boxes receive only the task
inputs and test surfaces allowed by the frozen suite policy; reference patches,
protected grader answers, and other oracle material are not placed in worker context.
This is access separation, not invisible evaluation: the ledger shows that protected
material exists, who can inspect it, and when it was used.

## 4. Comparable experiments

A harness change is compared with its predecessor using paired tasks and matched
conditions. Model, effort, tools, starting repository, network, time, and token budget
remain fixed unless the experiment explicitly names one of them as the independent
variable. Provider drift and model aliases are recorded rather than treated as the
same model.

Each release report includes:

- resolved-task rate and partial-progress distribution;
- invariant and security violations, always reported separately from task score;
- token, provider cost, wall time, and compute use;
- human questions, approvals, interventions, and takeovers;
- duplicate work, integration conflicts, retries, idle time, and stalled states;
- recovery success after worker, orchestrator, tool, and remote-effect failures;
- evidence completeness and reproducibility on a clean rerun; and
- performance by task family, duration, language, and required capability.

Camol reports the vector, not one blended score. A lower-cost run cannot compensate
for a correctness or safety regression. Statistical claims use predeclared multiple
trials or confidence intervals where the model or environment is stochastic.

## 5. Harness ablations

Benchmarking only the complete Camol stack would make improvements impossible to
attribute. Every milestone campaign includes matched controls:

```text
same model + direct vendor CLI
same model + Camol with one active execution box
same model + Camol with a plan-selected N-box topology
```

Unused workers remain dormant; a registered fleet never forces work into them. The
adaptive arm records active count, role assignment, task fan-out, candidate groups,
and why each additional box was justified. Fixed-topology ablations may separately
test partitioned, replicated, mixed-role, sequential, and different fixed-N
assignments. Additional ablations may disable plan freezing, cross-box context
routing, evaluator feedback, or recovery one at a time. Unsafe ablations run only in
isolated benchmark environments.

### Claude CLI versus Camol

The first product-facing comparison replays representative tasks from the owner's
actual work under three arms:

```text
A  direct Claude CLI
B  one active Camol box using the Claude CLI adapter
C  Camol orchestrator using its plan-selected adaptive box topology
```

Arm A measures the existing workflow. The difference between A and B reveals Camol's
kernel, logging, context, and gating overhead or benefit. The difference between B
and C reveals whether the orchestrator selected useful concurrency, task assignment,
parallel candidates, independent verification, or integration for that task.

Each paired task freezes the same user brief, acceptance contract, starting commit,
dependency lock state, seeded external sandbox, tool authority, network policy,
model/version, effort setting, and total token, spend, and wall-time ceilings. The
total budget is matched across arms rather than multiplied by the number of boxes.
Camol may produce a richer internal plan because planning is part of the harness being
tested; it may not receive extra facts about the desired implementation.

Historical tasks are reconstructed from the commit and external state that existed
before the original solution. Later commits, final diffs, reference answers, and
post-hoc tests are kept out of worker context. Each arm starts from a clean clone and
isolated cloud namespace. Stochastic arms receive multiple paired trials, with arm
order rotated when a human participates.

The same frozen evaluator grades all artifacts after execution. Where judgment is
required, reviewers see anonymized artifacts before learning which arm produced
them. Reports preserve task-level outcomes rather than publishing only an aggregate:

```text
accepted behavior, invariant violations, regression count
elapsed time, tokens, provider cost, compute, and tool calls
human questions, approvals, corrections, and takeover time
retries, duplicated work, merge conflicts, idle/stalled time
restart recovery, deployment reconciliation, and evidence completeness
```

A task may be won by the direct CLI, one-box Camol, or adaptive Camol. The adaptive
arm may correctly choose any finite active count inside its approved envelope. Camol
does not promote a harness change merely because its preferred arm wins the average;
the change must meet the predeclared threshold without a forbidden task-level
regression.

The planned command surface is:

```text
/bench capture <run-or-worklog>
/bench compare --arms claude-direct,camol-one,camol-adaptive
/bench inspect <campaign-id> [task-id]
/bench diff <baseline-campaign> <candidate-campaign>
```

## 6. Integrity and hill-climb policy

- Development canaries, rotating campaign tasks, and release cohorts are distinct.
- Suite code, task set, images, and graders are version- and digest-pinned.
- The benchmark task is never rewritten after seeing a candidate result.
- Grader failures, flaky infrastructure, and agent failures are separate outcomes.
- Gold/reference validation and a no-op negative control precede a campaign.
- Contaminated, broken, ambiguous, or non-reproducible tasks are reported and
  quarantined; they are never quietly counted as model or harness failures.
- Newly discovered Camol failures become durable regression scenarios, but public
  benchmark answers are not copied into agent prompts or product fixtures.
- Promotion requires the active plan's invariant gates, the candidate canary, and the
  declared improvement threshold. Release promotion additionally requires its frozen
  external and long-horizon cohorts.

The benchmark program itself is versioned. Changing task membership, grading,
budgets, or aggregation creates a new program revision and prevents misleading
before/after comparisons.
