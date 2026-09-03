# Product V0 adversarial review ledger

This ledger records the independent review performed against Product V0 before
release. It is part of the claim boundary: a finding is not closed by prose; it
needs a code change and an executable regression gate.

An attempted Claude/Fable review was stopped after the provider reported
`$2.10444975` despite a requested `$1` command ceiling and returned no review
report. No further hosted review spend was authorized. A separate read-only Codex
review completed and produced the findings below.

| Priority | Finding | V0 disposition | Regression evidence |
|---|---|---|---|
| P1 | The human evaluator was attached to every task and could deadlock a dependency graph. | Intermediate tasks run patch-integrity checks. A generated task depending on every declared task runs the human evaluator against the integrated graph. | `tests.test_planning.PlanningTests.test_grill_is_ordered_and_compiles_to_schema_v4`; `tests.test_product_e2e.ProductFlowTests.test_boot_to_grill_approval_detached_run_box_completion_and_reattach` |
| P1 | Approved exclusions were not included in worker rules. | Every exclusion becomes a digest-bound hard `human-exclusion-*` rule. | `tests.test_planning.PlanningTests.test_grill_is_ordered_and_compiles_to_schema_v4` |
| P1 | Free-form resource prose was accepted and silently ignored. | The resource answer accepts only five bounded `name=N` fields; extra prose, duplicates, and out-of-range values are denied. | `tests.test_planning.PlanningTests.test_resource_prose_and_forward_dependencies_are_rejected` |
| P1 | Worker effort was fixed rather than taken from the approved plan. | Each executable plan embeds a strict effective profile snapshot containing the approved effort and ceilings. Runtime admission and execution consume that snapshot. | `tests.test_app.InteractiveControllerTests.test_effective_worker_policy_matches_approved_effort_cost_and_timeout` |
| P1 | Secret-shaped grill text could enter durable session and plan records. | Interactive text is checked before persistence, and the TUI echo/history uses redacted text. | `tests.test_app.InteractiveControllerTests.test_secret_shaped_grill_input_is_rejected_before_plan_persistence`; `tests.test_tui.TuiTests.test_terminal_app_has_transcript_multiline_prompt_and_fleet` |
| P1 | A per-turn token ceiling could exceed the total run ceiling. | The compiled per-turn ceiling is capped by the approved total. | `tests.test_planning.PlanningTests.test_numeric_suffixes_and_total_turn_cap_are_preserved` |
| P1 | A very large exact plan could become impossible to review or reload. | Exact rendered plans are bounded before persistence; individual message and whole-session limits agree; oldest messages are trimmed atomically. | `tests.test_session.SessionStoreTests.test_save_trims_old_messages_to_its_reload_byte_limit` |
| P1 | A local-model URL could escape loopback or inherit proxy environment, leaking prompts. | Only credential-free numeric loopback HTTP(S) endpoints are accepted, and both discovery and chat use a proxy-disabled opener. | `tests.test_connections.ConnectionRegistryTests.test_non_loopback_local_endpoint_is_denied_without_network_access`; `tests.test_conversation.ConversationTests.test_local_conversation_rejects_non_loopback_before_network_access` |
| P1 | A stale Unix socket could be mistaken for a running supervisor. | `/run` proves the remote plan/status before reattach; a failed probe falls through to leader-locked stale-socket recovery. | `tests.test_app.InteractiveControllerTests.test_stale_socket_is_not_mistaken_for_a_live_supervisor` |
| P2 | List parsing removed meaningful numeric suffixes. | Parsing removes only a leading bullet or enumeration marker. | `tests.test_planning.PlanningTests.test_numeric_suffixes_and_total_turn_cap_are_preserved` |
| P2 | The product accepted forward dependencies that the runbook rejected later. | Grill and proposal validation require dependencies to be declared earlier. | `tests.test_planning.PlanningTests.test_resource_prose_and_forward_dependencies_are_rejected` |
| P2 | Save could create a session larger than load would accept. | Save serializes first, trims messages to the same 2 MiB load ceiling, and rejects an oversized plan/settings body. | `tests.test_session.SessionStoreTests.test_save_trims_old_messages_to_its_reload_byte_limit` |
| P2 | Box views included every task a capability-compatible worker could have run. | Box history is derived from actual assignment and box/worker event identity. | `tests.test_supervisor.SupervisorTests.test_box_tail_filters_unassigned_tasks_and_keeps_latest_events` |
| P2 | `/box` returned the oldest 100 events. | V2 box requests have a strict boolean `tail` option and Product V0 asks for the latest bounded window. | `tests.test_supervisor.SupervisorTests.test_box_tail_filters_unassigned_tasks_and_keeps_latest_events` |
| P2 | One broken connection probe hid all other connection results. | Each probe fails independently into a typed, non-secret error record. | `tests.test_connections.ConnectionRegistryTests.test_one_broken_runtime_does_not_hide_other_connections` |

Two additional failures were found while running those fixes through the complete
detached lifecycle:

- final-evaluator evidence exceeded asyncio's default 64 KiB line reader. The
  authenticated local client now accepts responses up to an explicit 8 MiB
  protocol ceiling and always closes its socket;
- a very short verification command could disappear before `ps` observed its
  birth marker, causing a process-group race. The sandbox now gives an exited
  process one bounded observation window and safely handles a concurrent reap.

A second independent Codex pass reviewed the remediation commit and found seven
more issues. All seven were reproduced or traced to a concrete contract boundary
and closed before release:

| Priority | Follow-up finding | V0 disposition | Regression evidence |
|---|---|---|---|
| P1 | Startup probed a stale socket once and aborted before the replacement daemon became ready. | The parent retries authenticated ping until the new child answers, exits, or reaches the startup deadline. | `tests.test_supervisor.SupervisorTests.test_spawn_retries_past_stale_control_files` |
| P1 | The proposal resource object expanded without a proposal schema version change. | Current proposals are V2; original V1 proposal shape and graph semantics remain readable and are never silently rewritten. | `tests.test_planning.PlanningTests.test_legacy_v1_proposal_remains_readable_with_original_shape`; `tests.test_app.InteractiveControllerTests.test_saved_v1_proposal_remains_visible_after_upgrade` |
| P1 | The final repository evaluator still ran from one box during integration replay. | Schema V4 freezes verification `cwd`; the evaluator bundle preserves it, and Product V0's final gate uses `workspace_root` in candidate and integrated contexts. | `tests.test_runbook.RunbookV4Tests.test_v4_verification_can_select_box_or_workspace_root`; `tests.test_product_e2e.ProductFlowTests.test_boot_to_grill_approval_detached_run_box_completion_and_reattach` |
| P1 | Prefixing an imperative exclusion with “must not” could invert its meaning. | Rules now preserve the user's clause under the neutral prefix `Excluded from worker scope:`. | `tests.test_planning.PlanningTests.test_grill_is_ordered_and_compiles_to_schema_v4` |
| P2 | The persistence guard rejected ordinary authentication/security prose. | Input rejection now uses a high-confidence credential detector; aggressive output redaction remains defense in depth. | `tests.test_planning.PlanningTests.test_secret_detection_allows_ordinary_security_engineering_language` |
| P2 | A box cursor could forget assignments recorded before the cursor and omit later task-only events. | Association is derived from full history before the response cursor/window is applied. | `tests.test_supervisor.SupervisorTests.test_box_cursor_uses_assignment_history_before_the_cursor` |
| P2 | A V1 process adapter's unknown `profile_snapshot` stopped being ignored. | The original tolerant V1 normalization drops it; strict versions continue to reject it for process adapters. | `tests.test_runbook.RunbookV1CompatibilityTests.test_v1_still_ignores_unknown_fields_and_carries_no_v2_fields` |

The final exact-commit review found three more edge cases. The reviewer could not
bind Unix/TCP sockets or inspect processes in its sandbox, so its local runtime suite
reported expected environment denials; the same tests passed in the release host.
Its static findings were independently reproduced and closed:

- high-confidence input detection now rejects secret-named assignments and long
  bare auth-scheme values while continuing to allow ordinary security-engineering
  prose and the `tokens=N` resource field;
- proposal V1 accepts both historical three-field and transitional six-field
  resource shapes, preserving all explicitly recorded six-field limits; and
- `cwd` is restricted to V4 verification commands. Step commands remain box-local
  until a future worker-execution schema explicitly implements another location.

Regression coverage is in
`tests.test_planning.PlanningTests.test_secret_detection_allows_ordinary_security_engineering_language`,
`tests.test_planning.PlanningTests.test_legacy_v1_proposal_remains_readable_with_original_shape`,
`tests.test_app.InteractiveControllerTests.test_saved_v1_proposal_remains_visible_after_upgrade`,
`tests.test_tui.TuiTests.test_terminal_app_has_transcript_multiline_prompt_and_fleet`,
and `tests.test_runbook.RunbookV4Tests.test_v4_verification_can_select_box_or_workspace_root`.

A final review of that remediation found two P2 usability regressions in the
high-confidence input guard. Broad output-redaction names had been reused for input
admission, and generic auth-scheme words were classified by length alone. Input
admission now uses normalized credential-bearing field forms plus a non-placeholder
value. Bearer values are always opaque credentials, while generic Basic/Token
candidates require mixed token structure. Regression cases cover `AUTH_ENABLED=true`, `GIT_AUTHOR_NAME=test`,
`max_tokens=12000`, snake/kebab auth prose, and the credential examples that must
remain denied. The aggressive defense-in-depth output redactor remains separate.

The remediation follow-up found and closed three P1 omissions: camelCase/session
credential names, numeric credential values, and low-entropy or hyphenated Bearer
values. Those forms are now denied before any raw grill or plan persistence.

A hands-on installed-client pass then found two release blockers and one visual
defect:

- the boot timer returned Textual's awaitable screen-dismiss handle and closed the
  application after the splash; the timer now invokes the non-awaitable dismiss
  action and a regression test holds the main screen open;
- the sanitized provider subprocess environment omitted `USER` and `LOGNAME`, which
  made an authenticated Claude CLI report logged out inside Camol and would keep the
  hosted readiness gate red. Connection discovery, planning calls, spend preflight,
  the detached supervisor, and the worker sandbox now preserve those non-secret
  identity fields while continuing to filter unrelated environment values; and
- Textual's default two-column black scrollbar track appeared as a black stripe at
  the transcript's right edge. V0 uses a one-column scrollbar with a track matching
  the transcript surface.

Startup now discovers connections in a disposable background thread, shows `↻`
while probing, refreshes automatically after provider login, and does not delay
client detach. Regression coverage is in `tests.test_tui`,
`tests.test_interactive_cli`, `tests.test_connections`, `tests.test_conversation`,
`tests.test_providers`, `tests.test_probes`, and `tests.test_admission_scheduler`.

The follow-up login pass replaces the argument-only interaction with a keyboard
picker limited to Claude Code and Codex CLI. Provider-native login still owns the
URL, browser session, and credential cache. Camol accepts the result only after a
fresh status probe; failure leaves the model unchanged, success is persisted and
rendered in the orchestrator, and an existing frozen plan cannot be rewritten by a
login confirmation.

A second hands-on interaction pass removed the connected-account shortcut because
it hid the provider URL and prevented deliberate reauthentication. `/login` now
always enters the native provider flow; a nonzero exit cannot promote a previously
green record. Enter sends, Shift+Enter inserts a line, `/` opens the command palette,
and `/skills`, `/history`, and `/clear` make the terminal discoverable without adding
their rendered output to model context. Reattachment retains durable state while
presenting a fresh, explicit boundary instead of replaying the old terminal surface.

The release gate remains the complete test suite, wheel installation in clean
environments with and without the TUI extra, strict runbook validation, Python
compilation, focused static checks, and `git diff --check`. Live Fable performance,
provider billing enforcement, remote targets, and public-production security are
not inferred from these local results.
