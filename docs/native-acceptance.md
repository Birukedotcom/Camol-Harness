# Native-provider acceptance: what remains to prove

The deterministic suite, process fixtures and account-capability smoke are different
gates. Passing one does not establish the others. This checklist follows M5–M7 in
[the v0 build plan](v0-build-plan.md), without reducing the broader implementation
goal to a provider smoke test.

## Current evidence boundary

- `tests/live/test_claude_fable.py` is explicitly opt-in and excluded from ordinary
  discovery. It invokes the spend-capped capability probe and checks model resolution
  and cost. It does not execute a complete project, refine a failed candidate,
  demonstrate daemon recovery, or obtain final human acceptance.
- `tests/test_product_e2e.py`, native adapter/peer tests and runtime-resilience tests
  exercise those product paths with controlled fixtures. They are regression
  evidence, not proof that a current external account/provider supports the workflow.
- `examples/claude-fable-runbook.json` requests one isolated bounded edit. It is a
  one-turn smoke, not the complete M7 scenario. Its actual verifier accepts a normal
  newline, rejects literal backslash-n, and rejects missing/incorrect/extra content;
  `tests/test_shipped_native_example.py` executes that oracle without a model call.
- Model-profile names and historical compatibility notes are not current entitlement
  evidence. Requested and actually resolved model identities must both be retained.

No native account call, login, model download, cloud provisioning or billable trial
is authorized by running the ordinary test suite or reading this checklist.

## Evidence required for the first backed native slice

| Requirement | Acceptance evidence |
| --- | --- |
| Exact approved scope | Frozen runbook, evaluator bundle, selected profile and source identity; recorded owner approval of the exact digest |
| Current provider capability | Explicitly authorized bounded preflight with actual binary/profile binding, requested/resolved model, expiry, cost and known/unknown outcome |
| Isolated real worker turn | Fresh admission, reservation and fence; native invocation identity; sandbox/workspace receipts; actual tool/output and candidate artifacts |
| Failed verification feeds refinement | Independent frozen verifier rejects a candidate; retained counterexample appears in the next bounded worker packet; corrected candidate is independently verified |
| Integration is separately verified | Candidate identity, integration generation, frozen evaluator results and exact integrated source revision |
| Interruption is recoverable | Controlled interruption at a declared boundary; preserved result/checkpoint and original invocation identity; no unapproved effect retry; reconciled unknown usage |
| Owner review is real | Visible candidate/evaluator evidence and explicit human approval of the consequential gate/final acceptance; no script silently supplying the owner's identity for an unseen candidate |
| Cost and limits are honest | Known charges plus outstanding/unknown reservations; per-turn/task/run ceilings; no inferred zero cost, no fallback model disguised as the requested one |
| Results survive the terminal | Exported ledger/artifacts replay to the same run state and preserve the above evidence, independent of terminal scrollback |
| Comparison is properly scoped | A separately authorized matched direct-CLI/Camol trial records quality, retries, tokens, cost, time, recovery and human intervention; one trial is not a statistical win |

For a given trial, record the product commit, installed package/runtime identity,
provider CLI identity, selected account/profile scope and exact test scenario. The
owner must choose the provider/model and approve explicit spending limits before
the live trial. Do not reuse a broad historical example budget as fresh consent.
Use a disposable source repository and a separate retained state directory; do not
test interruption against a production deployment or a user's active worktree.

The existing `doctor`, `provider-preflight`, plan approval, `start`/`serve`, `ctl`,
box/evidence inspection and `export`/`verify-export` paths supply the mechanisms.
Use their current `--help` and the reviewed runbook to determine exact arguments.
This document deliberately does not supply a paste-and-run billable command or
pretend that a capability receipt satisfies the full workflow gate.

## Still separate from native acceptance

Distributed target-side admission/launch, source and artifact transfer, trusted
remote result/accounting reduction, actual cloud/voice proof, fleet-wide salvage,
operational restore/key lifecycle, public benchmarks and release portability have
their own open acceptance gates. A successful local native trial will not close
those requirements automatically, and no broad `BACKED` or finished-V0 claim follows
from the deterministic test count alone.
