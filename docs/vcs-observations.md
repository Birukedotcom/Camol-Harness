# Remote VCS observations

Camol can now attach GitHub branch, PR, check, status and review observations to an
**exact accepted integration** of a captured candidate. This extends the local
[candidate relationship graph](vcs-lineage.md), without pushing, merging, posting
reviews, approving gates, provisioning machines or changing any account login.

## Explicit setup

Use `camol vcs inspect` or `/vcs` to identify the candidate and accepted integration.
Observation requires the existing absolute state directory, exact run ID and source
workspace. The CLI takes the same execution leader lock as other owner operations;
stop/drain a live supervisor before using this synchronous CLI workflow.

Prepare a target JSON file, replacing the example repository, branch and PR number:

```json
{
  "schema": "camol.github_vcs_target",
  "schema_version": 1,
  "repository": "your-org/your-repo",
  "branch": "feature/build",
  "pull_request": 12
}
```

Set `pull_request` to null for branch/check/status observations without a PR. The
target cannot contain credentials, arbitrary hosts, URLs, query parameters or
shell options. V1 supports bounded ASCII GitHub repository/branch names and only
`api.github.com` over verified HTTPS. Enterprise/GitLab/other provider schemas are
not silently accepted as equivalent.

`camol vcs observe` requires `--state-dir`, `--run-id`, `--workspace`, `--candidate`,
`--integration`, `--target`, `--by` and explicit `--allow-network`. Run
`camol vcs observe --help` for the exact options. It accepts `--timeout 1..60`
(default 30 seconds) and optional stable `--request-id`. The existing approved
human owner must authorize the request; workers cannot author its ledger events.

Public reads need no credential. For private reads, optionally use `--token-env`
to name one environment variable you explicitly supplied. Its value is never
stored in the target or ledger. Camol does not implicitly read GitHub CLI account
stores, ambient tokens, Claude/Codex accounts, or initiate login. Token strings
must be bounded printable non-whitespace ASCII. Provider permissions remain the
owner's responsibility; a 404 can reflect lack of access, not proven absence.

## What executes and what is retained

The native collector makes four GETs without a PR, seven with one:
branch-before, optional PR-before, checks and combined statuses for the exact
integration commit, optional reviews and PR-after, then branch-after. No retries,
redirects, pagination requests, proxy discovery or repository programs are used.
The API version is pinned to `2026-03-10`.

An isolated stdlib child runs under the harness's own Python interpreter with
`-I -S`, a minimal environment, private stdin and bounded capture. The existing
owned-process runner bounds DNS/header/body stalls, elapsed time, cancellation and
output; cleanup kills its owned process group. Each successful response body is
limited to 2 MiB, total child output to 16 MiB, stdin to 4 KiB, and each collection
to its first 100 items. Bounds refuse the operation, not silently truncate it.
The requested timeout excludes the small bounded process-cleanup allowance.

Before networking, `VCS_OBSERVATION_STARTED` retains the exact candidate,
integration, local repository identity, revision, target, owner, request ID and
limits. `VCS_OBSERVATION_FINISHED` retains a separate observed/unavailable/cancelled
receipt with local monotonic duration, timestamps and any wall-clock regression.
`planned_http_requests` is not a measured request count or provider bill.

Successful responses are reduced to identity/status fields. PR bodies, check
output text, comments, review prose, usernames, error bodies and authentication
headers are not retained. Check names and status contexts remain useful metadata,
with capture-time credential redaction, including exact supplied credential
echoes. Replay validates the retained fields without consulting current credential
environment values; changing an unrelated secret cannot rewrite historical truth.
Receipt digests bind retained metadata, not raw HTTP bodies or a signed GitHub
attestation. Storage and collector execution use the existing trusted-owner model.

An unavailable read retains its intent and a typed failure without invented
response content. Abrupt termination or failed final persistence can leave a
pending intent. An exact retry with the same request ID returns that existing
record, including pending state, without another network request. A new ID is an
explicit new observation. No recovery helper silently reissues an unknown read.
Cancellation remains cancellation even if recording its finish fails.

## Interpreting the result

`camol vcs observation --state-dir STATE --run-id RUN --request-id ID` reads the
complete retained request/result without networking. `/vcs` shows recent request
status; the V2 JSON graph contains per-candidate observation history and the latest
readback. A newer pending or failed read clears the convenience readback fields;
it never makes an older matching response look freshly confirmed.

Before/after ref and PR fields must agree to claim matching endpoints. The branch
SHA must equal the exact accepted integration revision. A PR must also name that
head, repository and branch; a forked or moved head cannot borrow its checks.
These are successive observations, not an atomic remote snapshot, an exclusion
of move-away-and-back races, or proof that Camol performed the push.

Check runs and statuses bind the requested commit. Counts/pagination mark incomplete
collections explicitly. They describe the endpoint's selected collection, not all
required checks or branch-protection rules. Reviews retain their associated commit
and submitted time, including old or pending records, without deriving an approval
decision. GitHub distinguishes check-run results from pull-request reviews; neither
is automatically a Camol evaluator verdict. See the official [check runs API](https://docs.github.com/en/rest/checks/runs?apiVersion=2026-03-10)
and [reviews API](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2026-03-10).

CLI exit 0 means the requested scope was read successfully, its heads matched and
the observed collections were complete. It does **not** mean the checks passed,
the PR was approved/mergeable, or deployment is authorized. Exit 2 includes pending,
transport errors, missing responses, moved/mismatched heads and partial collections.
The retained record supplies details. No result changes historical acceptance,
tokens, task state or existing evaluator gates.

## Embedding, compatibility and limits

`Harness.observe_vcs(...)` exposes the same owner workflow, refusing synchronous
observation during its active runner. `Orchestrator.observe_vcs(run_id, ...)` exposes
the ledger service. An explicit `fetcher` callback is labeled
`owner_embedding_callback`; its owner, not Camol's native process wrapper, is
responsible for enforcing the supplied timeout/cancellation policy. Its returned
fields are still validated and redacted before persistence. The native collector
uses `deadline_enforcement=owned_process`; callbacks use `embedding_owner`.

Legacy V1 graph digests remain unchanged before the first observation. V2 adds
external-observation fields without changing old relationship events. Requests and
receipts survive normal ledger export/replay. There is a 1,000-request per-run
ceiling, including pending entries; automatic archival/rotation is not implemented.
The existing exact-run reader's byte/event ceilings can be reached earlier.

Actual push-operation receipts, PR mutations, ruleset/approval-policy evaluation,
live background polling, cross-run VCS objects, dirty-status capture, non-GitHub
providers and distributed worker execution remain separate requirements. Remote
readback is evidence for human investigation, never a substitute for those gates.
