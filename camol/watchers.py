"""Cursor-based observation with explicit instrumentation readiness and terminality.

Sources are injected callables. A timed-out or empty poll is waiting, never proof
of absence or completion. The cursor and accepted events commit in one transaction.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from .schema import canonical_digest, require_digest, require_identifier, require_string, require_timestamp
from .store import ConcurrentAppendError


class WatcherError(ValueError):
    pass


WATCHER_EVENTS = frozenset({"WATCHER_CREATED", "WATCHER_BATCH_RECORDED", "WATCHER_WAITING", "WATCHER_CONFLICT", "WATCHER_REOPENED",
                            "WATCHER_SCHEDULED", "WATCHER_POLL_STARTED", "WATCHER_POLL_FINISHED", "WATCHER_STOPPED"})


def _names(value: Any, name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > 128:
        raise WatcherError(name + " must be a non-empty unique array")
    names = tuple(require_identifier(item, name) for item in value)
    if len(set(names)) != len(names):
        raise WatcherError(name + " must be a non-empty unique array")
    return names


@dataclass(frozen=True)
class WatchSpec:
    watcher_id: str
    source_id: str
    query: str
    event_classes: Tuple[str, ...]
    correlation_keys: Tuple[str, ...]
    terminal_event_class: str
    correlation_filter: Tuple[Tuple[str, str], ...]
    source_schema: str
    parser_version: str
    fixture_digest: str
    timeout_seconds: int = 30
    max_batch_events: int = 1000

    def __post_init__(self):
        for name in ("watcher_id", "source_id", "terminal_event_class", "source_schema", "parser_version"):
            require_identifier(getattr(self, name), name)
        require_digest(self.fixture_digest, "fixture_digest")
        require_string(self.query, "watch query")
        if len(self.query) > 16384:
            raise WatcherError("watch query is too large")
        object.__setattr__(self, "event_classes", _names(self.event_classes, "event_classes"))
        object.__setattr__(self, "correlation_keys", _names(self.correlation_keys, "correlation_keys"))
        raw_filter = self.correlation_filter
        if isinstance(raw_filter, dict):
            raw_filter = tuple(raw_filter.items())
        if (not isinstance(raw_filter, (list, tuple)) or not raw_filter
                or any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in raw_filter)):
            raise WatcherError("correlation_filter must bind at least one exact identity")
        pairs = tuple((require_identifier(key, "correlation key"), require_identifier(value, "correlation value"))
                      for key, value in raw_filter)
        if len(dict(pairs)) != len(pairs) or not set(dict(pairs)) <= set(self.correlation_keys):
            raise WatcherError("correlation_filter must bind unique declared keys")
        object.__setattr__(self, "correlation_filter", tuple(sorted(pairs)))
        if self.terminal_event_class not in self.event_classes:
            raise WatcherError("terminal event must be one of the expected event classes")
        for name, maximum in (("timeout_seconds", 3600), ("max_batch_events", 10000)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum:
                raise WatcherError(name + " is outside its supported bound")

    def to_dict(self):
        return dict(schema="camol.watch_spec", schema_version=1, watcher_id=self.watcher_id,
                    source_id=self.source_id, query=self.query, event_classes=list(self.event_classes),
                    correlation_keys=list(self.correlation_keys), terminal_event_class=self.terminal_event_class,
                    correlation_filter=dict(self.correlation_filter), source_schema=self.source_schema,
                    parser_version=self.parser_version, fixture_digest=self.fixture_digest,
                    timeout_seconds=self.timeout_seconds, max_batch_events=self.max_batch_events)

    @classmethod
    def from_dict(cls, value):
        fields = {"schema", "schema_version", "watcher_id", "source_id", "query", "event_classes",
                  "correlation_keys", "terminal_event_class", "timeout_seconds", "max_batch_events",
                  "correlation_filter", "source_schema", "parser_version", "fixture_digest"}
        if (not isinstance(value, dict) or set(value) != fields or value.get("schema") != "camol.watch_spec"
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
            raise WatcherError("invalid watch specification")
        return cls(**{name: value[name] for name in fields - {"schema", "schema_version"}})


@dataclass(frozen=True)
class ObserverReceipt:
    source_id: str
    query_digest: str
    source_schema: str
    parser_version: str
    fixture_digest: str
    event_classes: Tuple[str, ...]
    correlation_keys: Tuple[str, ...]
    observed_at: str
    expires_at: str

    def __post_init__(self):
        for name in ("source_id", "source_schema", "parser_version"):
            require_identifier(getattr(self, name), name)
        for name in ("query_digest", "fixture_digest"):
            require_digest(getattr(self, name), name)
        object.__setattr__(self, "event_classes", _names(self.event_classes, "event_classes"))
        object.__setattr__(self, "correlation_keys", _names(self.correlation_keys, "correlation_keys"))
        for name in ("observed_at", "expires_at"):
            object.__setattr__(self, name, require_timestamp(getattr(self, name), name))
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.observed_at):
            raise WatcherError("observer receipt has an empty validity window")

    def ready(self, spec: WatchSpec, now: str) -> bool:
        instant = datetime.fromisoformat(require_timestamp(now, "observer now"))
        return (self.source_id == spec.source_id and self.query_digest == canonical_digest(spec.query)
                and self.source_schema == spec.source_schema and self.parser_version == spec.parser_version
                and self.fixture_digest == spec.fixture_digest
                and set(spec.event_classes) <= set(self.event_classes)
                and set(spec.correlation_keys) <= set(self.correlation_keys)
                and datetime.fromisoformat(self.observed_at) <= instant < datetime.fromisoformat(self.expires_at))

    def to_dict(self):
        return dict(schema="camol.observer_receipt", schema_version=1, source_id=self.source_id,
                    query_digest=self.query_digest, source_schema=self.source_schema,
                    parser_version=self.parser_version, fixture_digest=self.fixture_digest,
                    event_classes=list(self.event_classes), correlation_keys=list(self.correlation_keys),
                    observed_at=self.observed_at, expires_at=self.expires_at)

    @classmethod
    def from_dict(cls, value):
        fields = {"schema", "schema_version", "source_id", "query_digest", "source_schema", "parser_version",
                  "fixture_digest", "event_classes", "correlation_keys", "observed_at", "expires_at"}
        if (not isinstance(value, dict) or set(value) != fields or value.get("schema") != "camol.observer_receipt"
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
            raise WatcherError("invalid observer receipt")
        return cls(**{name: value[name] for name in fields - {"schema", "schema_version"}})


def normalize_observation(value: dict, spec: WatchSpec) -> dict:
    fields = {"event_id", "revision", "event_class", "correlation", "occurred_at", "content_digest"}
    if not isinstance(value, dict) or set(value) != fields:
        raise WatcherError("observations contain only identity, time, correlation and a content digest")
    require_identifier(value["event_id"], "source event id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise WatcherError("source event revision must be positive")
    if value["event_class"] not in spec.event_classes:
        raise WatcherError("unknown source event class; observer schema must be reprobed")
    correlation = value["correlation"]
    if not isinstance(correlation, dict) or set(correlation) != set(spec.correlation_keys):
        raise WatcherError("observation lacks the declared correlation identity")
    for item in correlation.values():
        require_identifier(item, "correlation value")
    require_timestamp(value["occurred_at"], "source event time")
    require_digest(value["content_digest"], "observed content digest")
    return deepcopy(value)


WATCHER_EXECUTOR = "camol-observer"


def apply_watcher_event(state: dict, event: dict) -> None:
    p = event["payload"]
    kind = event["type"]
    if not isinstance(p, dict):
        raise WatcherError("watcher payload must be an object")
    watcher_id = require_identifier(p.get("watcher_id"), "watcher_id")
    if kind == "WATCHER_CREATED":
        if set(p) != {"watcher_id", "spec", "spec_digest", "plan_digest", "approved_by"}:
            raise WatcherError("watch creation has missing or unknown fields")
        spec = WatchSpec.from_dict(p["spec"])
        if (spec.watcher_id != watcher_id or p["spec_digest"] != canonical_digest(spec.to_dict())
                or p["plan_digest"] != state["plan_digest"] or p["approved_by"] != state["approved_by"]
                or state["approved_by"] is None or watcher_id in state["watchers"]
                or event.get("actor_id") != p["approved_by"]):
            raise WatcherError("watch creation requires unique identity and approval of its exact plan/spec")
        state["watchers"][watcher_id] = dict(deepcopy(p), status="waiting", cursor=None,
                                            observations={}, last_poll=None, blocker=None)
        return
    watcher = state["watchers"].get(watcher_id)
    if watcher is None:
        raise WatcherError("watcher is unknown")
    if kind not in {"WATCHER_REOPENED", "WATCHER_SCHEDULED", "WATCHER_STOPPED"} and event.get("actor_id") != WATCHER_EXECUTOR:
        raise WatcherError("observation runtime events require the trusted observer actor")
    if kind in {"WATCHER_SCHEDULED", "WATCHER_POLL_STARTED", "WATCHER_POLL_FINISHED", "WATCHER_STOPPED"}:
        from .watch_runtime import apply_schedule_event
        apply_schedule_event(state, watcher, event)
        return
    if kind == "WATCHER_REOPENED":
        if set(p) != {"watcher_id", "approved_by", "reason", "cursor", "observed_at"}:
            raise WatcherError("watch reopen has missing or unknown fields")
        if p["approved_by"] != state["approved_by"] or watcher["status"] != "completed" or event.get("actor_id") != p["approved_by"]:
            raise WatcherError("only the approved owner can reopen a completed watcher")
        require_string(p["reason"], "watch reopen reason")
        require_timestamp(p["observed_at"], "reopen time")
        _cursor(p["cursor"])
        watcher.update(status="waiting", cursor=p["cursor"], blocker=None, last_poll=p["observed_at"])
        return
    if watcher["status"] in {"completed", "conflict", "stopped"}:
        raise WatcherError("watcher is terminal; conflicting source revisions require a new reviewed watcher")
    if kind in {"WATCHER_WAITING", "WATCHER_CONFLICT"}:
        if set(p) != {"watcher_id", "reason", "observed_at"}:
            raise WatcherError("watch wait has missing or unknown fields")
        require_timestamp(p["observed_at"], "poll time")
        require_string(p["reason"], "watch wait reason")
        watcher.update(status="conflict" if kind == "WATCHER_CONFLICT" else "waiting",
                       blocker=p["reason"], last_poll=p["observed_at"])
        return
    if kind != "WATCHER_BATCH_RECORDED" or set(p) != {"watcher_id", "expected_cursor", "next_cursor", "receipt", "observed_at", "observations"}:
        raise WatcherError("watch batch has missing or unknown fields")
    if p["expected_cursor"] != watcher["cursor"]:
        raise WatcherError("watch cursor advanced; discard stale poll result")
    _cursor(p["next_cursor"])
    spec = WatchSpec.from_dict(watcher["spec"])
    receipt = ObserverReceipt.from_dict(p["receipt"])
    if not receipt.ready(spec, p["observed_at"]):
        raise WatcherError("OBSERVATION_INCOMPLETE: observer is stale or incompatible")
    if watcher.get("schedule"):
        from datetime import timedelta
        active = watcher.get("active_poll")
        instant = datetime.fromisoformat(p["observed_at"])
        if (not active or active["schedule_digest"] != watcher["schedule_digest"]
                or not (datetime.fromisoformat(active["observed_at"]) <= instant
                        < min(datetime.fromisoformat(watcher["schedule"]["expires_at"]),
                              datetime.fromisoformat(active["observed_at"]) + timedelta(seconds=spec.timeout_seconds)))):
            raise WatcherError("OBSERVATION_INCOMPLETE: scheduled poll authority expired or is absent")
    if not isinstance(p["observations"], list) or len(p["observations"]) > spec.max_batch_events:
        raise WatcherError("observation batch exceeds its bound")
    accepted = dict(watcher["observations"])
    new_keys = set()
    for raw in p["observations"]:
        item = normalize_observation(raw, spec)
        if datetime.fromisoformat(item["occurred_at"]) > datetime.fromisoformat(p["observed_at"]):
            raise WatcherError("OBSERVATION_INCOMPLETE: future-dated source event is not current evidence")
        key = "{}:{}".format(item["event_id"], item["revision"])
        prior = accepted.get(key)
        if prior is not None and prior != item:
            raise WatcherError("EVIDENCE_CONFLICT: same source event revision has different content")
        if prior is None:
            accepted[key] = item
            new_keys.add(key)
    latest = {}
    for key, item in accepted.items():
        previous = latest.get(item["event_id"])
        if previous is None or item["revision"] > accepted[previous]["revision"]:
            latest[item["event_id"]] = key
    terminal = any(key in new_keys and accepted[key]["event_class"] == spec.terminal_event_class
                   and all(accepted[key]["correlation"][name] == value for name, value in spec.correlation_filter)
                   for key in latest.values())
    watcher["observations"] = accepted
    watcher.update(cursor=p["next_cursor"], last_poll=p["observed_at"], blocker=None,
                   status="completed" if terminal else "waiting", receipt=receipt.to_dict())


def _cursor(value):
    if value is not None and (not isinstance(value, str) or not value or len(value) > 4096):
        raise WatcherError("cursor must be null or bounded nonempty opaque text")


class Watcher:
    def __init__(self, orchestrator: Any, run_id: str, watcher_id: str):
        self.orchestrator, self.run_id, self.watcher_id = orchestrator, run_id, watcher_id

    def _commit(self, event_type: str, payload: dict, state: Optional[dict] = None) -> dict:
        current = state if state is not None else self.orchestrator.state(self.run_id)
        if state is not None:
            fresh = self.orchestrator.state(self.run_id)
            if (fresh["watchers"].get(self.watcher_id) != current["watchers"].get(self.watcher_id)
                    or fresh["plan_digest"] != current["plan_digest"] or fresh["approved_by"] != current["approved_by"]):
                raise ConcurrentAppendError("watch identity, cursor or authorization changed during the poll")
            # Independent workers/watches may emit while this source is awaited.
            # Rebase only an unchanged watch, then retain the store's final CAS.
            current = fresh
        raw = dict(payload, watcher_id=self.watcher_id)
        clean = self.orchestrator.redactor.value(raw)
        if clean != raw:
            # Do not silently alter opaque cursors or query/identity contracts.
            # Source plugins must store credentials separately and supply safe
            # cursor handles, not bearer tokens, to the public event ledger.
            raise WatcherError("observer metadata contains protected values; use non-secret identifiers and cursor handles")
        actor = clean["approved_by"] if event_type in {"WATCHER_CREATED", "WATCHER_REOPENED", "WATCHER_SCHEDULED", "WATCHER_STOPPED"} else WATCHER_EXECUTOR
        apply_watcher_event(deepcopy(current), {"type": event_type, "payload": clean, "actor_id": actor})
        self.orchestrator._emit(self.run_id, event_type, clean, actor_id=actor, expected_seq=current["last_seq"])
        return self.inspect()

    @classmethod
    def create(cls, orchestrator: Any, run_id: str, spec: WatchSpec, *, approved_by: str) -> "Watcher":
        state = orchestrator.state(run_id)
        watcher = cls(orchestrator, run_id, spec.watcher_id)
        watcher._commit("WATCHER_CREATED", dict(spec=spec.to_dict(), spec_digest=canonical_digest(spec.to_dict()),
                                                plan_digest=state["plan_digest"], approved_by=approved_by), state)
        return watcher

    def inspect(self) -> dict:
        state = self.orchestrator.state(self.run_id)
        if self.watcher_id not in state["watchers"]:
            raise WatcherError("unknown watcher")
        return state["watchers"][self.watcher_id]

    def reopen(self, *, approved_by: str, reason: str, cursor: Optional[str] = None) -> dict:
        return self._commit("WATCHER_REOPENED", dict(approved_by=approved_by, reason=reason, cursor=cursor,
                                                    observed_at=self.orchestrator._now()))

    async def poll(self, fetch: Callable[..., Any], receipt: ObserverReceipt) -> dict:
        current = self.orchestrator.state(self.run_id)
        watcher = current["watchers"].get(self.watcher_id)
        if watcher is None:
            raise WatcherError("unknown watcher")
        if watcher["status"] in {"completed", "conflict", "stopped"}:
            return watcher
        spec = WatchSpec.from_dict(watcher["spec"])
        if not receipt.ready(spec, self.orchestrator._now()):
            return self._commit("WATCHER_WAITING", dict(reason="OBSERVATION_INCOMPLETE: observer readiness expired or mismatched",
                                                         observed_at=self.orchestrator._now()))
        try:
            # The source contract is an async callable. It owns supported API
            # authentication, read-only querying and decoding, never shell text.
            result = await asyncio.wait_for(fetch(spec.query, watcher["cursor"]), timeout=spec.timeout_seconds)
        except asyncio.TimeoutError:
            return self._commit("WATCHER_WAITING", dict(reason="poll_timeout", observed_at=self.orchestrator._now()), current)
        except Exception:
            # An unavailable source is not absence. Do not persist raw provider
            # exceptions, which may include query credentials or response bodies.
            return self._commit("WATCHER_WAITING", dict(reason="OBSERVATION_INCOMPLETE: source query failed",
                                                        observed_at=self.orchestrator._now()), current)
        if not isinstance(result, dict) or set(result) != {"observations", "next_cursor"}:
            return self._commit("WATCHER_WAITING", dict(reason="OBSERVATION_INCOMPLETE: malformed source response",
                                                        observed_at=self.orchestrator._now()), current)
        try:
            return self._commit("WATCHER_BATCH_RECORDED", dict(expected_cursor=watcher["cursor"],
                                next_cursor=result["next_cursor"], observations=result["observations"],
                                receipt=receipt.to_dict(), observed_at=self.orchestrator._now()), current)
        except ValueError as error:
            if str(error).startswith("EVIDENCE_CONFLICT"):
                return self._commit("WATCHER_CONFLICT", dict(reason=str(error), observed_at=self.orchestrator._now()), current)
            return self._commit("WATCHER_WAITING", dict(reason="OBSERVATION_INCOMPLETE: invalid source metadata or readiness",
                                                        observed_at=self.orchestrator._now()), current)
