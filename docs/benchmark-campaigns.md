# Pinned benchmark campaigns

`camol.campaign` adds an embeddable campaign runner around the existing
`BenchmarkTrial` matched-pair contract. It does not download public benchmark data
or claim a leaderboard result. `BenchmarkSuite` is the trusted host-plugin boundary
for isolated materialization, gold/no-op validation, agent execution, independent
grading, evidence retention and teardown.

The frozen manifest names the explicit task cohort and dataset digest, source
commits, evaluator/public-test/environment digests, model/version/effort, sampling,
context, hardware, tool/network/secrets policy, per-arm harness commits, paired
seeds, three arms, repetition count, per-trial ceilings and promotion thresholds.
Every task runs direct Claude, one Camol box, and adaptive Camol under the same
total trial budget; adaptive boxes do not multiply the allowance. Arm order rotates
by repetition. Selection does not change after observing a candidate's result.

```python
from camol.campaign import BenchmarkCampaign, CampaignStore
from camol.schema import canonical_digest

store = CampaignStore(state_path)
try:
    # manifest is reviewed before this explicit owner approval.
    campaign = BenchmarkCampaign.create(
        store, manifest, approved_by=authenticated_owner,
        manifest_digest=canonical_digest(manifest),
    )
    result = await campaign.run(suite_adapter)
    report = campaign.report()
finally:
    store.close()
```

The plugin must prove the pinned gold candidate passes and a no-op negative control
fails before any model trial. Broken readiness is quarantined, not counted as an
agent failure. Plugins must enforce the supplied ceilings; the campaign also checks
returned usage and time and retains unresolved reservations for interrupted or
invalid trials. A user-name string is not authentication: these APIs belong to the
trusted owner control plane, not a worker tool.

Durable trial identities include the campaign digest, task, repetition and arm.
Accepted results also bind the candidate and artifact-manifest digests, so cached
scores cannot silently apply to a different patch. A resumed completed trial is not
executed again. Unknown charges and partial cleanup require explicit reconciliation;
the harness does not hide them by repeating a trial until it passes.

Reports preserve per-task paired vectors and families, task-level correctness and
safety regressions, evidence completeness, recovery and unresolved usage. They
expose readiness for human promotion review under the frozen thresholds, not an
automatic promotion. No statistical-significance claim or blended score is made.
The [offline SWE-bench Verified adapter](swebench-adapter.md) now supplies pinned
ingestion and an official grader interface. Its protocol tests are not public
scores; real Docker cohorts, trusted three-arm budget-enforcing executors,
powered multi-seed studies and scheduled campaign hosting still require explicit
environment setup and validation.

```text
camol campaign validate --manifest /absolute/campaign.json
camol campaign status --db /absolute/campaign.sqlite3 --campaign-id campaign-id
camol campaign report --db /absolute/campaign.sqlite3 --campaign-id campaign-id
```

The included campaign tests execute tiny real Python gold/no-op/trial subprocesses.
They test scheduling and accounting, not model coding ability. No paid model request
or protected reference answer is used in that fixture.
