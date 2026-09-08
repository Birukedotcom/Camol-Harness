"""Replayable usage accounting; unknown provider usage is never treated as free.

Provider receipts describe observed consumption. Reservations are conservative
budget charges only, never advertised as measured tokens or actual dollars.
"""

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional

from .schema import canonical_digest, require_identifier, require_non_negative_int, require_schema_header, require_timestamp


class UsageError(ValueError):
    """Usage identity or measurements cannot be trusted."""


@dataclass(frozen=True)
class UsageRecord:
    SCHEMA = "camol.usage"
    SCHEMA_VERSION = 1

    invocation_id: str
    run_id: str
    task_id: str
    agent_id: str
    lease_id: str
    turn_number: int
    provider: str
    model: Optional[str]
    phase: str
    provenance: str
    outcome: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_creation_tokens: Optional[int]
    cost_usd_micros: Optional[int]
    reserved_tokens: int
    reserved_cost_usd_micros: int
    started_at: str
    finished_at: str

    def __post_init__(self) -> None:
        for name in ("invocation_id", "run_id", "task_id", "agent_id", "lease_id", "provider", "phase"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), "usage " + name))
        if self.model is not None:
            require_identifier(self.model, "usage model")
        require_non_negative_int(self.turn_number, "usage turn_number")
        if self.turn_number < 1:
            raise UsageError("usage turn_number must be positive")
        if self.provenance not in {"provider_observed", "worker_reported", "unknown"}:
            raise UsageError("usage provenance is invalid")
        if self.outcome not in {"success", "error", "unknown", "cancelled"}:
            raise UsageError("usage outcome is invalid")
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd_micros"):
            value = getattr(self, name)
            if value is not None:
                require_non_negative_int(value, "usage " + name)
        for name in ("reserved_tokens", "reserved_cost_usd_micros"):
            require_non_negative_int(getattr(self, name), "usage " + name)
        for name in ("started_at", "finished_at"):
            object.__setattr__(self, name, require_timestamp(getattr(self, name), "usage " + name))
        if datetime.fromisoformat(self.finished_at) < datetime.fromisoformat(self.started_at):
            raise UsageError("usage cannot finish before it starts")
        if self.input_tokens is not None:
            cache = (self.cache_read_tokens or 0) + (self.cache_creation_tokens or 0)
            if cache > self.input_tokens:
                raise UsageError("usage cache tokens exceed total input tokens")
        if self.provenance == "unknown" and any(getattr(self, name) is not None for name in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd_micros"
        )):
            raise UsageError("unknown usage cannot contain claimed measurements")

    @property
    def duration_ms(self) -> int:
        return round((datetime.fromisoformat(self.finished_at) - datetime.fromisoformat(self.started_at)).total_seconds() * 1000)

    @property
    def total_tokens(self) -> Optional[int]:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def token_charge(self) -> int:
        return self.reserved_tokens if self.total_tokens is None else self.total_tokens

    @property
    def cost_charge(self) -> int:
        return self.reserved_cost_usd_micros if self.cost_usd_micros is None else self.cost_usd_micros

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
                **{field.name: getattr(self, field.name) for field in fields(self)}}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UsageRecord":
        require_schema_header(value, cls.SCHEMA, cls.SCHEMA_VERSION, "usage record")
        names = {field.name for field in fields(cls)}
        if set(value) != names | {"schema", "schema_version"}:
            raise UsageError("usage record has missing or unknown fields")
        return cls(**{name: value[name] for name in names})


def _trusted_receipts(evidence: Iterable[Mapping[str, Any]]) -> Dict[str, UsageRecord]:
    records: Dict[str, UsageRecord] = {}
    for item in evidence:
        if item.get("kind") != "model_usage" or item.get("epistemic_status") != "OBSERVED" or item.get("producer") != "adapter":
            continue
        data = item.get("data", {})
        if data.get("schema") != UsageRecord.SCHEMA:
            continue
        record = UsageRecord.from_dict(data)
        for name in ("run_id", "task_id", "agent_id", "lease_id"):
            if item.get(name) != getattr(record, name):
                raise UsageError("usage receipt does not match its evidence " + name)
        previous = records.get(record.invocation_id)
        if previous is not None and previous != record:
            raise UsageError("conflicting usage receipts for invocation " + record.invocation_id)
        records[record.invocation_id] = record
    return records


def _subject(item):
    return tuple(item.get(name) for name in ("run_id", "task_id", "agent_id", "lease_id"))


def _legacy_receipts(evidence, modern):
    """Correlate old bills using identity, never coincidentally equal amounts.

    The pre-v1 adapter emitted a trusted command with invocation_id immediately
    before its model_usage receipt, which lacked that ID. Evidence-only callers
    preserve ledger insertion order. Bills with neither command nor invocation
    retain separate evidence identities and explicitly incomplete coverage.
    """
    commands, receipts, links, seen_evidence = {}, {}, {}, {}
    for index, item in enumerate(evidence):
        data = item.get("data", {})
        subject = _subject(item)
        if item.get("producer") == "adapter" and item.get("epistemic_status") == "EXECUTED" and item.get("kind") == "command":
            commands[subject] = data
        if (item.get("kind") != "model_usage" or item.get("epistemic_status") != "OBSERVED"
                or item.get("producer") != "adapter" or data.get("schema") == UsageRecord.SCHEMA):
            continue
        command = commands.get(subject, {})
        invocation = data.get("invocation_id") or command.get("invocation_id")
        if data.get("invocation_id") and data["invocation_id"] != command.get("invocation_id"):
            command = {}  # unrelated latest command cannot supply this bill's timing
        if invocation is not None:
            require_identifier(invocation, "legacy usage invocation")
        evidence_id = item.get("evidence_id")
        if evidence_id is not None:
            require_identifier(evidence_id, "legacy evidence ID")
            if evidence_id in seen_evidence:
                previous, link = seen_evidence[evidence_id]
                if previous != item:
                    raise UsageError("conflicting legacy usage evidence identity")
                links[id(item)] = link
                continue
        values = {}
        for name in ("input_tokens", "output_tokens", "cost_usd_micros"):
            amount = data.get(name)
            values[name] = None if amount is None else require_non_negative_int(amount, "legacy " + name)
        model = data.get("resolved_model") or data.get("model")
        if model is not None:
            require_identifier(model, "legacy observed model")
        if invocation in modern:
            record = modern[invocation]
            if subject != tuple(getattr(record, name) for name in ("run_id", "task_id", "agent_id", "lease_id")):
                raise UsageError("legacy usage identity names another modern receipt subject")
            if any(amount is not None and amount != getattr(record, name) for name, amount in values.items()):
                raise UsageError("legacy and modern usage disagree for one invocation")
            if model is not None and model != record.model:
                raise UsageError("legacy and modern usage disagree about resolved model")
            links[id(item)] = ("modern", invocation)
            if evidence_id is not None:
                seen_evidence[evidence_id] = (item, links[id(item)])
            continue
        if invocation is None and all(subject) and any(subject == tuple(getattr(record, name) for name in ("run_id", "task_id", "agent_id", "lease_id")) for record in modern.values()):
            raise UsageError("legacy usage cannot be disambiguated from modern receipts without invocation/command identity")
        identity = ("invocation", invocation) if invocation else (("evidence", evidence_id) if evidence_id else ("unbound", index))
        elapsed = None
        start, finish = command.get("started_at"), command.get("finished_at")
        if start is not None and finish is not None:
            delta = (datetime.fromisoformat(require_timestamp(finish, "legacy finish")) - datetime.fromisoformat(require_timestamp(start, "legacy start"))).total_seconds()
            if delta < 0:
                raise UsageError("legacy command clock regressed")
            elapsed = round(delta * 1000)
        value = dict(subject=subject, invocation_id=invocation, model=model, duration_ms=elapsed, **values)
        previous = receipts.get(identity)
        if previous is not None:
            if previous["subject"] != subject:
                raise UsageError("legacy invocation belongs to another subject")
            for name, amount in value.items():
                if amount is None:
                    continue
                if previous[name] is not None and previous[name] != amount:
                    raise UsageError("conflicting legacy usage receipts for one identity")
                previous[name] = amount
        else:
            receipts[identity] = value
        links[id(item)] = ("legacy", identity)
        if evidence_id is not None:
            seen_evidence[evidence_id] = (item, links[id(item)])
    return receipts, links


def provider_cost_used(state: Mapping[str, Any], task_id: Optional[str] = None) -> int:
    """Observed or conservatively reserved provider microdollars, deduplicated."""
    evidence = list(state.get("evidence", {}).values())
    records = _trusted_receipts(evidence)
    legacy, _ = _legacy_receipts(evidence, records)
    total = sum(record.cost_charge for record in records.values() if task_id is None or record.task_id == task_id)
    if task_id is None:
        total += state.get("revision", {}).get("inherited_usage", {}).get("provider_cost_usd_micros", 0)
    # Preserve old observed provider receipts, while never charging a worker's
    # UNVERIFIED claim as if it were a provider bill.
    total += sum(item["cost_usd_micros"] or 0 for item in legacy.values() if task_id is None or item["subject"][1] == task_id)
    return total


def accounted_tokens(state: Mapping[str, Any]) -> int:
    """Add failed/unconsumed invocations to the existing successful-turn total.

    AGENT_TURN_RECORDED replay puts its usage_invocation_id into
    accounted_usage_invocations. That prevents receipts and successful turns
    counting the same request twice.
    """
    consumed = set(state.get("accounted_usage_invocations", ()))
    records = _trusted_receipts(state.get("evidence", {}).values())
    inherited = state.get("revision", {}).get("inherited_usage", {}).get("accounted_tokens", 0)
    return inherited + state.get("total_tokens", 0) + sum(record.token_charge for key, record in records.items() if key not in consumed)


def _empty_metrics() -> Dict[str, Any]:
    return dict(invocations=0, failed_invocations=0, provider_observed_tokens=0,
                worker_reported_tokens=0, observed_cost_usd_micros=0,
                unknown_token_invocations=0, unknown_cost_invocations=0,
                reserved_unknown_tokens=0, reserved_unknown_cost_usd_micros=0,
                duration_ms=0, cache_read_tokens=0, cache_creation_tokens=0,
                legacy_provider_receipts=0, unknown_duration_invocations=0, unbound_legacy_invocations=0)


def _legacy_tokens(record):
    return None if record["input_tokens"] is None or record["output_tokens"] is None else record["input_tokens"] + record["output_tokens"]


def _measure_legacy(metrics, record):
    metrics["invocations"] += 1
    metrics["legacy_provider_receipts"] += 1
    metrics["unbound_legacy_invocations"] += record["invocation_id"] is None
    amount = _legacy_tokens(record)
    if amount is None:
        metrics["unknown_token_invocations"] += 1
    else:
        metrics["provider_observed_tokens"] += amount
    if record["cost_usd_micros"] is None:
        metrics["unknown_cost_invocations"] += 1
    else:
        metrics["observed_cost_usd_micros"] += record["cost_usd_micros"]
    if record["duration_ms"] is None:
        metrics["unknown_duration_invocations"] += 1
    else:
        metrics["duration_ms"] += record["duration_ms"]


def _measure(metrics: Dict[str, Any], record: UsageRecord) -> None:
    metrics["invocations"] += 1
    metrics["failed_invocations"] += record.outcome != "success"
    metrics["duration_ms"] += record.duration_ms
    if record.total_tokens is None:
        metrics["unknown_token_invocations"] += 1
        metrics["reserved_unknown_tokens"] += record.reserved_tokens
    else:
        key = "provider_observed_tokens" if record.provenance == "provider_observed" else "worker_reported_tokens"
        metrics[key] += record.total_tokens
    if record.cost_usd_micros is None:
        metrics["unknown_cost_invocations"] += 1
        metrics["reserved_unknown_cost_usd_micros"] += record.reserved_cost_usd_micros
    else:
        metrics["observed_cost_usd_micros"] += record.cost_usd_micros
    metrics["cache_read_tokens"] += record.cache_read_tokens or 0
    metrics["cache_creation_tokens"] += record.cache_creation_tokens or 0


def usage_report(events: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build a deterministic content-free usage/latency report from a run ledger.

    Event bodies, prompts, commands, credentials and tool outputs are deliberately
    excluded. Summed invocation duration is not elapsed run wall time: parallel
    work may make the sum larger. Old ledgers retain explicit incomplete coverage.
    """
    ordered = list(events)
    run_ids = {event["run_id"] for event in ordered}
    if len(run_ids) > 1:
        raise UsageError("usage reports cover exactly one run")
    evidence = [event["payload"] for event in ordered if event.get("type") == "EVIDENCE_RECORDED"]
    records = _trusted_receipts(evidence)
    legacy, legacy_links = _legacy_receipts(evidence, records)
    totals = _empty_metrics()
    groups: Dict[str, Dict[str, Any]] = {"by_task": {}, "by_box": {}, "by_model": {}, "by_phase": {}}
    for record in records.values():
        _measure(totals, record)
        for group, key in (("by_task", record.task_id), ("by_box", record.agent_id), ("by_model", record.model or "unknown"), ("by_phase", record.phase)):
            _measure(groups[group].setdefault(key, _empty_metrics()), record)
    for record in legacy.values():
        _measure_legacy(totals, record)
        for group, key in (("by_task", record["subject"][1]), ("by_box", record["subject"][2]),
                           ("by_model", record["model"]), ("by_phase", "legacy-provider")):
            _measure_legacy(groups[group].setdefault(key or "unknown", _empty_metrics()), record)
    consumed = set()
    consumed_legacy = set()
    pending_legacy = {}
    turns = []
    for event in ordered:
        if event.get("type") == "EVIDENCE_RECORDED":
            item = event["payload"]
            if id(item) in legacy_links:
                pending_legacy[_subject(item)] = legacy_links[id(item)]
        if event.get("type") == "AGENT_TURN_RECORDED":
            turn = event["payload"]
            identifier = turn.get("usage_invocation_id")
            if identifier in records:
                if identifier in consumed:
                    raise UsageError("one provider invocation cannot fund two agent turns")
                receipt = records[identifier]
                if any(turn.get(name) != getattr(receipt, name) for name in ("task_id", "agent_id", "lease_id")):
                    raise UsageError("agent turn refers to another usage subject")
                consumed.add(identifier)
            else:
                subject = (event["run_id"], turn.get("task_id"), turn.get("agent_id"), turn.get("lease_id"))
                linked = pending_legacy.pop(subject, None)
                if linked:
                    kind, key = linked
                    record = records[key] if kind == "modern" else legacy[key]
                    amount = record.total_tokens if kind == "modern" else _legacy_tokens(record)
                    used = consumed if kind == "modern" else consumed_legacy
                    invocation = record.invocation_id if kind == "modern" else record["invocation_id"]
                    if identifier is not None and invocation != identifier:
                        raise UsageError("agent turn names another legacy usage invocation")
                    if key in used and amount is not None:
                        raise UsageError("one provider invocation cannot fund two legacy agent turns")
                    if amount is not None:
                        reported = sum(require_non_negative_int(turn[name], "reported " + name) for name in ("input_tokens", "output_tokens"))
                        if reported != amount:
                            raise UsageError("legacy turn differs from its observed provider usage")
                        used.add(key)
                        continue
                turns.append(turn)
    for turn in turns:
        # Legacy turns and arbitrary process adapters supply estimates, not a
        # provider receipt. Reporting keeps that distinction explicit.
        amount = sum(require_non_negative_int(turn[name], "reported " + name) for name in ("input_tokens", "output_tokens"))
        for metrics in (totals, groups["by_task"].setdefault(turn["task_id"], _empty_metrics()), groups["by_box"].setdefault(turn["agent_id"], _empty_metrics()), groups["by_model"].setdefault("worker-reported", _empty_metrics()), groups["by_phase"].setdefault("worker", _empty_metrics())):
            metrics["worker_reported_tokens"] += amount
    tools: Dict[str, Dict[str, Any]] = {}
    seen_tools = set()
    seen_commands = set()
    command_duration = 0
    evaluator_duration = 0
    commands = 0
    for item in evidence:
        if item.get("epistemic_status") != "EXECUTED":
            continue
        data = item.get("data", {})
        invocation = data.get("invocation_id")
        if item.get("kind") == "command" and invocation and invocation not in seen_commands:
            seen_commands.add(invocation)
            commands += 1
            start, finish = data.get("started_at"), data.get("finished_at")
            if start and finish:
                elapsed = max(0, round((datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds() * 1000))
                command_duration += elapsed
                if item.get("producer") == "verifier":
                    evaluator_duration += elapsed
        if item.get("kind") == "tool_call":
            identity = (invocation, data.get("tool_use_id"))
            if data.get("phase") != "request" or identity in seen_tools:
                continue
            seen_tools.add(identity)
            name = data.get("tool") or "unknown"
            tools.setdefault(name, {"calls": 0, "duration_ms": None})["calls"] += 1
    debug_receipts = {}
    debugger_duration = 0
    debugger_commands = 0
    for event in ordered:
        if event.get("type") != "DEBUG_EXECUTION_FINISHED" or event.get("actor_id") != "debug-executor":
            continue
        payload = event["payload"]
        identity = (payload["case_id"], payload["execution_id"])
        receipt = payload["receipt"]
        if identity in debug_receipts:
            if debug_receipts[identity] != receipt:
                raise UsageError("conflicting debugger execution receipts")
            continue
        debug_receipts[identity] = receipt
        for result in receipt["results"]:
            debugger_duration += require_non_negative_int(result["duration_ms"], "debugger duration")
            debugger_commands += 1
    lineage = [event["payload"]["proposal"]["inherited_usage"] for event in ordered if event.get("type") == "REVISION_LINKED"]
    if len(lineage) > 1:
        raise UsageError("run has more than one revision parent")
    inherited = lineage[0] if lineage else {"accounted_tokens": 0, "provider_cost_usd_micros": 0}
    totals["inherited_accounted_tokens"] = inherited["accounted_tokens"]
    totals["inherited_accounted_cost_usd_micros"] = inherited["provider_cost_usd_micros"]
    totals["inherited_unknown_usage"] = inherited.get("unknown_usage", False)
    totals["accounted_tokens"] = totals["provider_observed_tokens"] + totals["worker_reported_tokens"] + totals["reserved_unknown_tokens"] + inherited["accounted_tokens"]
    totals["accounted_cost_usd_micros"] = totals["observed_cost_usd_micros"] + totals["reserved_unknown_cost_usd_micros"] + inherited["provider_cost_usd_micros"]
    return {
        "schema": "camol.usage_report", "schema_version": 1,
        "run_id": next(iter(run_ids), None), "event_count": len(ordered),
        "source_digest": canonical_digest(ordered), "totals": totals, **groups,
        "tools": tools, "commands": commands + debugger_commands, "command_duration_ms": command_duration + debugger_duration,
        "evaluator_duration_ms": evaluator_duration,
        "debugger_duration_ms": debugger_duration, "debugger_commands": debugger_commands,
        "capacity_call_reservations": sum(event.get("type") == "CAPACITY_CALL_RESERVED" for event in ordered),
        "retry_events": sum(event.get("type") == "TASK_RETRY_SCHEDULED" for event in ordered),
        "coverage": {"provider_receipts": len(records) + len(legacy), "versioned_provider_receipts": len(records),
                     "legacy_provider_receipts": len(legacy), "worker_reported_turns": len(turns),
                     "unknown_usage": bool(totals["unknown_token_invocations"] or totals["unknown_cost_invocations"] or totals["inherited_unknown_usage"]),
                     "legacy_identity_incomplete": bool(totals["unbound_legacy_invocations"]),
                     "duration_coverage_complete": not bool(totals["unknown_duration_invocations"] or turns),
                     "duration_is_sum_not_wall_time": True,
                     "tool_latency_available": False},
    }
