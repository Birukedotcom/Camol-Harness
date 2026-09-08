"""Content-free, read-only investigation views over the authoritative run ledger."""

from collections import Counter
from datetime import datetime

from .schema import canonical_digest
from .state import project
from .usage import usage_report


def profile_run(events):
    """Expose costs, retries, waits and lifecycle spans without inventing savings.

    UTC event spans are diagnostic envelopes, not CPU, provider latency or exclusive
    work time. They are deliberately separate from measured command/usage receipts.
    """
    ordered = list(events)
    state = project(ordered)
    usage = usage_report(ordered)
    spans, open_spans, issues = [], {}, []
    observed_types, waits = Counter(), Counter()
    phase_events = {"TASK_STARTED": "builder_envelope", "TASK_SUBMITTED": "verification_to_acceptance_envelope"}
    end_events = {"TASK_SUCCEEDED", "TASK_RETRY_SCHEDULED", "TASK_BLOCKED", "LEASE_REVOKED"}

    def finish(task_id, event, *, censored=False):
        previous = open_spans.pop(task_id, None)
        if previous is None:
            return
        started, phase = previous
        duration = (datetime.fromisoformat(event["occurred_at"]) - datetime.fromisoformat(started["occurred_at"])).total_seconds()
        if duration < 0:
            issues.append(dict(kind="clock_regression", task_id=task_id, start_seq=started["seq"], end_seq=event["seq"]))
        spans.append(dict(task_id=task_id, phase=phase, start_seq=started["seq"], end_seq=event["seq"],
                          started_at=started["occurred_at"], finished_at=event["occurred_at"],
                          duration_ms=round(duration * 1000) if duration >= 0 else None, right_censored=censored))

    preceding = None
    for event in ordered:
        kind, payload = event["type"], event["payload"]
        if preceding and datetime.fromisoformat(event["occurred_at"]) < datetime.fromisoformat(preceding["occurred_at"]):
            issues.append(dict(kind="clock_regression", start_seq=preceding["seq"], end_seq=event["seq"]))
        preceding = event
        observed_types[kind] += 1
        task_id = payload.get("task_id")
        if kind == "TASK_WAITING":
            reason = payload.get("reason", payload.get("waiting_reason", {}))
            waits[reason.get("code", "unknown")] += 1
        if task_id and kind in phase_events:
            finish(task_id, event)
            open_spans[task_id] = (event, phase_events[kind])
        elif task_id and kind in end_events:
            finish(task_id, event)
    if ordered:
        for task_id in list(open_spans):
            finish(task_id, ordered[-1], censored=True)
    succeeded = sum(task["status"] == "succeeded" for task in state["tasks"].values())
    completed_steps = sum(len(task["completed_step_ids"]) for task in state["tasks"].values())
    hotspots = []
    for task_id, task in state["tasks"].items():
        metrics = usage["by_task"].get(task_id, {})
        hotspots.append(dict(task_id=task_id, status=task["status"], attempts=task["attempts"],
                             recorded_turns=task["turn_count"], recorded_completed_steps=len(task["completed_step_ids"]),
                             accounted_tokens=sum(metrics.get(name, 0) for name in ("provider_observed_tokens", "worker_reported_tokens", "reserved_unknown_tokens")),
                             accounted_cost_usd_micros=sum(metrics.get(name, 0) for name in ("observed_cost_usd_micros", "reserved_unknown_cost_usd_micros")),
                             invocation_duration_ms=metrics.get("duration_ms", 0),
                             unknown_usage=bool(metrics.get("unknown_token_invocations", 0) or metrics.get("unknown_cost_invocations", 0))))
    hotspots.sort(key=lambda item: (-item["accounted_tokens"], -item["invocation_duration_ms"], item["task_id"]))
    elapsed = None
    if ordered:
        elapsed = round((datetime.fromisoformat(ordered[-1]["occurred_at"]) - datetime.fromisoformat(ordered[0]["occurred_at"])).total_seconds() * 1000)
        if elapsed < 0:
            elapsed = None
    terminal = state["status"] in {"completed", "blocked", "superseded"}
    return dict(schema="camol.diagnostic_profile", schema_version=1, run_id=state["run_id"], status=state["status"],
                source_digest=canonical_digest(ordered), as_of_seq=state["last_seq"], event_counts=dict(sorted(observed_types.items())),
                wait_event_counts=dict(sorted(waits.items())), task_hotspots=hotspots, lifecycle_spans=spans, clock_issues=issues,
                ledger_elapsed_ms=elapsed, ledger_elapsed_right_censored=not terminal, usage=usage,
                productivity=dict(succeeded_tasks=succeeded, recorded_completed_steps=completed_steps,
                                  accounted_tokens_per_succeeded_task=usage["totals"]["accounted_tokens"] / succeeded if succeeded else None,
                                  accounted_tokens_per_recorded_step=usage["totals"]["accounted_tokens"] / completed_steps if completed_steps else None),
                coverage=dict(cpu_time_available=False, peak_memory_available=False, exclusive_phase_latency_available=False,
                              span_clock="recorded_utc", spans_include_human_wait_and_integration=True,
                              planning_usage_in_separate_session_ledger=True,
                              savings_or_causal_claim=False))


def event_metadata(event):
    """A log-sink record without user prose, command arguments or source bodies."""
    payload = event["payload"]
    return dict(schema="camol.log_metadata", schema_version=1, run_id=event["run_id"], seq=event["seq"],
                event_type=event["type"], occurred_at=event["occurred_at"], actor_id=event["actor_id"],
                subject={key: payload[key] for key in ("task_id", "agent_id", "lease_id", "watcher_id", "case_id") if key in payload},
                event_digest=canonical_digest(event))
