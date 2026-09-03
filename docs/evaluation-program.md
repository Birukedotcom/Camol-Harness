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

Public suites are complemented by a Camol-owned workflow corpus. Its first long-run
scenario family comes from sanitized Buckeye work patterns: inspect a repository,
plan work across three boxes, implement and integrate a change, forward-deploy to an
isolated GCP target, observe a voice-agent boundary, diagnose a seeded failure,
recover from an orchestrator restart, and produce replayable evidence. This internal
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
same model + Camol orchestrator and three execution boxes
```

The two unused v1 slots remain dormant in the one-active-box control so the kernel's
three-slot contract does not change. Additional ablations may disable plan freezing,
cross-box context routing, evaluator feedback, or recovery one at a time. Unsafe
ablations run only in isolated benchmark environments.

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
