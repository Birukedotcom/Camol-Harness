"""Durable, explicitly authorized observation scheduling and trusted source plugins.

The standard journal source reads normalized metadata, not arbitrary log prose. A
source plugin is host code, never a module path or command supplied by a worker.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Protocol
from uuid import uuid4

from .schema import canonical_digest, canonical_json_bytes, require_digest, require_identifier, require_string, require_timestamp
from .store import ConcurrentAppendError
from .watchers import ObserverReceipt, Watcher, WatcherError, WatchSpec, normalize_observation


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WatcherError(name + " is outside its supported bound")
    return value


def _strict_json(raw):
    """Never resolve ambiguous external metadata into a consequential verdict."""
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise WatcherError("journal JSON contains duplicate object keys")
            value[key] = item
        return value

    def invalid_constant(value):
        raise WatcherError("journal JSON contains a non-finite constant")

    try:
        return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise WatcherError("journal JSON is malformed or ambiguous") from error


def normalize_schedule(value):
    fields = {"schema", "schema_version", "watcher_id", "binding", "interval_seconds", "max_polls", "expires_at"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.watch_schedule"
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise WatcherError("invalid watch schedule")
    require_identifier(value["watcher_id"], "watcher_id")
    _integer(value["interval_seconds"], "interval_seconds", 1, 86400)
    _integer(value["max_polls"], "max_polls", 1, 10000000)
    require_timestamp(value["expires_at"], "expires_at")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {"plugin_id", "plugin_digest", "source_id", "config"}:
        raise WatcherError("invalid observer binding")
    for field in ("plugin_id", "source_id"):
        require_identifier(binding[field], field)
    require_digest(binding["plugin_digest"], "plugin_digest")
    if not isinstance(binding["config"], dict) or len(canonical_json_bytes(binding["config"])) > 16384:
        raise WatcherError("observer configuration must be a bounded object without credentials")
    return deepcopy(value)


def apply_schedule_event(state, watcher, event):
    p, kind = event["payload"], event["type"]
    if kind in {"WATCHER_SCHEDULED", "WATCHER_STOPPED"}:
        if p.get("approved_by") != state["approved_by"] or event.get("actor_id") != p.get("approved_by"):
            raise WatcherError("observation changes require the approved owner")
        if watcher.get("active_poll"):
            raise WatcherError("stop or finish the active poll before changing its authorization")
    if kind == "WATCHER_SCHEDULED":
        if set(p) != {"watcher_id", "schedule", "schedule_digest", "approved_by"}:
            raise WatcherError("watch schedule has unknown or missing fields")
        schedule = normalize_schedule(p["schedule"])
        if (watcher["status"] != "waiting" or schedule["watcher_id"] != p["watcher_id"]
                or schedule["binding"]["source_id"] != watcher["spec"]["source_id"]
                or canonical_digest(schedule) != p["schedule_digest"]
                or watcher.get("schedule_digest") == p["schedule_digest"]):
            raise WatcherError("watch schedule does not bind this waiting observer")
        watcher.update(schedule=schedule, schedule_digest=p["schedule_digest"], scheduled_polls=0,
                       active_poll=None, last_attempt_at=None)
    elif kind == "WATCHER_STOPPED":
        if set(p) != {"watcher_id", "approved_by", "reason"} or watcher["status"] not in {"waiting", "conflict"}:
            raise WatcherError("only unfinished observation can be explicitly stopped")
        require_string(p["reason"], "stop reason")
        watcher.update(status="stopped", blocker="owner_stopped: " + p["reason"])
    elif kind == "WATCHER_POLL_STARTED":
        if set(p) != {"watcher_id", "attempt_id", "schedule_digest", "observed_at"}:
            raise WatcherError("invalid observation attempt")
        require_identifier(p["attempt_id"], "attempt_id")
        now = datetime.fromisoformat(require_timestamp(p["observed_at"], "poll time"))
        schedule = watcher.get("schedule")
        if (not schedule or watcher["status"] != "waiting" or watcher.get("active_poll")
                or p["schedule_digest"] != watcher["schedule_digest"]
                or watcher["scheduled_polls"] >= schedule["max_polls"]
                or now >= datetime.fromisoformat(schedule["expires_at"])):
            raise WatcherError("observation lacks current bounded scheduling authority")
        previous = watcher.get("last_attempt_at")
        if previous and now < datetime.fromisoformat(previous) + timedelta(seconds=schedule["interval_seconds"]):
            raise WatcherError("observation interval has not elapsed")
        watcher.update(active_poll=deepcopy(p), last_attempt_at=p["observed_at"],
                       scheduled_polls=watcher["scheduled_polls"] + 1)
    elif kind == "WATCHER_POLL_FINISHED":
        if set(p) != {"watcher_id", "attempt_id", "outcome", "observed_at"}:
            raise WatcherError("invalid observation attempt completion")
        pending = watcher.get("active_poll")
        now = datetime.fromisoformat(require_timestamp(p["observed_at"], "poll end time"))
        if (not pending or p["attempt_id"] != pending["attempt_id"]
                or now < datetime.fromisoformat(pending["observed_at"])
                or p["outcome"] not in {"observed", "unavailable", "interrupted", "recovered_unknown"}):
            raise WatcherError("observation result does not bind its active attempt")
        recovery_deadline = min(datetime.fromisoformat(watcher["schedule"]["expires_at"]),
                                datetime.fromisoformat(pending["observed_at"]) + timedelta(seconds=watcher["spec"]["timeout_seconds"]))
        if p["outcome"] == "recovered_unknown" and now < recovery_deadline:
            raise WatcherError("a prior observer may still be active")
        watcher.update(active_poll=None, last_attempt_outcome=p["outcome"])
    else:
        raise WatcherError("unknown scheduling event")


class ObservationSource(Protocol):
    plugin_id: str
    plugin_digest: str

    def validate(self, binding: dict, spec: WatchSpec) -> None: ...
    async def probe(self, binding: dict, spec: WatchSpec, now: str) -> ObserverReceipt: ...
    async def fetch(self, binding: dict, spec: WatchSpec, query: str, cursor: str) -> dict: ...


class WatchRuntime:
    def __init__(self, orchestrator, run_id, *, sources=None):
        self.orchestrator, self.run_id = orchestrator, run_id
        supplied = [JournalSource()] if sources is None else list(sources)
        self.sources = {source.plugin_id: source for source in supplied}
        if len(self.sources) != len(supplied):
            raise WatcherError("duplicate observer plugin identity")
        self._active = set()

    def configure(self, schedule, *, approved_by, approval_digest):
        schedule = normalize_schedule(schedule)
        if canonical_digest(schedule) != approval_digest:
            raise WatcherError("approve the exact observation schedule digest")
        watcher = Watcher(self.orchestrator, self.run_id, schedule["watcher_id"])
        if watcher.inspect().get("schedule_digest") == approval_digest:
            if approved_by != self.orchestrator.state(self.run_id)["approved_by"]:
                raise WatcherError("observation changes require the approved owner")
            # Retried approval is idempotent, never a way to reset poll spend.
            return watcher.inspect()
        spec = WatchSpec.from_dict(watcher.inspect()["spec"])
        binding = schedule["binding"]
        self._source(binding).validate(binding, spec)
        if datetime.fromisoformat(schedule["expires_at"]) <= datetime.fromisoformat(self.orchestrator._now()):
            raise WatcherError("observation schedule is already expired")
        return watcher._commit("WATCHER_SCHEDULED", dict(schedule=schedule, schedule_digest=approval_digest, approved_by=approved_by))

    def stop(self, watcher_id, *, approved_by, reason):
        return Watcher(self.orchestrator, self.run_id, watcher_id)._commit("WATCHER_STOPPED", dict(approved_by=approved_by, reason=reason))

    def _source(self, binding):
        source = self.sources.get(binding["plugin_id"])
        if source is None or source.plugin_digest != binding["plugin_digest"]:
            raise WatcherError("OBSERVATION_INCOMPLETE: approved observer plugin is unavailable or changed")
        return source

    def inspect(self):
        watches = deepcopy(self.orchestrator.state(self.run_id)["watchers"])
        now = datetime.fromisoformat(self.orchestrator._now())
        for watcher in watches.values():
            schedule = watcher.get("schedule")
            if not schedule:
                watcher["scheduler_state"] = "manual"
            elif watcher["status"] != "waiting":
                watcher["scheduler_state"] = watcher["status"]
            elif watcher.get("active_poll"):
                watcher["scheduler_state"] = "polling_or_unknown"
            elif now >= datetime.fromisoformat(schedule["expires_at"]):
                watcher["scheduler_state"] = "approval_expired"
            elif watcher["scheduled_polls"] >= schedule["max_polls"]:
                watcher["scheduler_state"] = "poll_budget_exhausted"
            else:
                watcher["scheduler_state"] = "scheduled"
        return watches

    async def tick(self):
        """Run each due source once, sequentially; never overlap the same cursor."""
        for watcher_id in sorted(self.orchestrator.state(self.run_id)["watchers"]):
            if watcher_id in self._active:
                continue
            self._active.add(watcher_id)
            try:
                await self._poll_due(watcher_id)
            except ConcurrentAppendError:
                # Another durable event won; do not retry this source in a tight
                # loop or accept an observation under the wrong cursor.
                continue
            finally:
                self._active.discard(watcher_id)
        return self.inspect()

    async def run_until_settled(self, *, should_stop=None):
        """Embedding host owns this awaitable and must await its cancellation."""
        while should_stop is None or not should_stop():
            current = await self.tick()
            if not any(item["scheduler_state"] in {"scheduled", "polling_or_unknown"} for item in current.values()):
                return current
            await asyncio.sleep(1.0)
        return self.inspect()

    async def _poll_due(self, watcher_id):
        watcher = Watcher(self.orchestrator, self.run_id, watcher_id)
        current = watcher.inspect()
        schedule = current.get("schedule")
        if not schedule:
            return
        now = datetime.fromisoformat(self.orchestrator._now())
        pending = current.get("active_poll")
        if pending:
            deadline = min(datetime.fromisoformat(schedule["expires_at"]),
                           datetime.fromisoformat(pending["observed_at"]) + timedelta(seconds=current["spec"]["timeout_seconds"]))
            if now < deadline:
                return
            watcher._commit("WATCHER_POLL_FINISHED", dict(attempt_id=pending["attempt_id"], outcome="recovered_unknown", observed_at=now.isoformat()))
            current = watcher.inspect()
        if (current["status"] != "waiting" or current["scheduled_polls"] >= schedule["max_polls"]
                or now >= datetime.fromisoformat(schedule["expires_at"])):
            return
        if current.get("last_attempt_at") and now < datetime.fromisoformat(current["last_attempt_at"]) + timedelta(seconds=schedule["interval_seconds"]):
            return
        attempt_id = "poll-" + uuid4().hex
        watcher._commit("WATCHER_POLL_STARTED", dict(attempt_id=attempt_id, schedule_digest=current["schedule_digest"], observed_at=now.isoformat()))
        outcome = "unavailable"
        try:
            binding = schedule["binding"]
            source = self._source(binding)
            spec = WatchSpec.from_dict(current["spec"])
            source.validate(binding, spec)
            async def observe():
                receipt = await source.probe(binding, spec, self.orchestrator._now())

                async def fetch(query, cursor):
                    return await source.fetch(binding, spec, query, cursor)

                return await watcher.poll(fetch, receipt)

            # One total deadline covers probing AND the query. The durable
            # recovery lease must not expire while a second timeout is running.
            remaining = (datetime.fromisoformat(schedule["expires_at"]) - datetime.fromisoformat(self.orchestrator._now())).total_seconds()
            result = await asyncio.wait_for(observe(), timeout=max(0, min(spec.timeout_seconds, remaining)))
            outcome = "observed" if result.get("blocker") is None else "unavailable"
        except asyncio.CancelledError:
            outcome = "interrupted"
            raise
        except ConcurrentAppendError:
            raise
        except Exception:
            watcher._commit("WATCHER_WAITING", dict(reason="OBSERVATION_INCOMPLETE: observer unavailable, changed or unready", observed_at=self.orchestrator._now()))
        finally:
            # No raw exception, response body or credential enters the ledger.
            watcher._commit("WATCHER_POLL_FINISHED", dict(attempt_id=attempt_id, outcome=outcome, observed_at=self.orchestrator._now()))


def journal_header(spec):
    return dict(schema=spec.source_schema, parser_version=spec.parser_version, source_id=spec.source_id,
                event_classes=list(spec.event_classes), correlation_keys=list(spec.correlation_keys))


def journal_fixture_digest(spec):
    return canonical_digest(dict(parser="camol.normalized_jsonl.v1", header=journal_header(spec),
                                 cases=["every_declared_class", "reject_missing_identity", "reject_unknown_field", "reject_invalid_digest",
                                        "reject_duplicate_json_keys", "reject_nonfinite_json"]))


class JournalSource:
    """Bounded local normalized-event journal; no code, SQL or shell execution.

    This proves parser shape and access, not the upstream producer's coverage or
    honesty. No body content is ingested; records carry a source content digest.
    """

    plugin_id = "normalized-jsonl-v1"

    @property
    def plugin_digest(self):
        return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def binding(self, source_id, path, *, max_bytes=4 * 1024 * 1024):
        return dict(plugin_id=self.plugin_id, plugin_digest=self.plugin_digest, source_id=source_id,
                    config=dict(path=str(Path(path).resolve()), expected_uid=os.getuid(), max_bytes=max_bytes))

    def validate(self, binding, spec):
        config = binding["config"]
        if (set(config) != {"path", "expected_uid", "max_bytes"} or binding["source_id"] != spec.source_id
                or spec.query != "journal:" + spec.source_id or spec.parser_version != "jsonl-v1"
                or spec.fixture_digest != journal_fixture_digest(spec)):
            raise WatcherError("normalized journal binding or parser fixture does not match the watch")
        path = Path(require_string(config["path"], "journal path"))
        if not path.is_absolute() or path != path.resolve():
            raise WatcherError("journal path must be absolute and canonical, without symlinks")
        if type(config["expected_uid"]) is not int or config["expected_uid"] != os.getuid():
            raise WatcherError("journal must be owned by the current host user")
        _integer(config["max_bytes"], "journal max_bytes", 128, 16 * 1024 * 1024)

    def _read(self, binding, spec):
        self.validate(binding, spec)
        config = binding["config"]
        fd = os.open(config["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != config["expected_uid"]
                    or meta.st_mode & 0o022 or meta.st_size > config["max_bytes"]):
                raise WatcherError("journal is not a bounded, owner-controlled regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(config["max_bytes"] + 1)
            if len(raw) > config["max_bytes"]:
                raise WatcherError("journal exceeds its approved read ceiling")
            separator = raw.find(b"\n")
            if separator < 0 or separator > 16384 or _strict_json(raw[:separator]) != journal_header(spec):
                raise WatcherError("journal instrumentation header is absent or changed")
            return raw, meta, separator + 1
        finally:
            os.close(fd)

    async def probe(self, binding, spec, now):
        self._read(binding, spec)
        for ambiguous in ('{"event_class":"started","event_class":"ended"}',
                          '{"correlation":{"job":"other","job":"target"}}', '{"offset":NaN}'):
            try:
                _strict_json(ambiguous)
            except WatcherError:
                pass
            else:
                raise WatcherError("observer ambiguous JSON fixture was accepted")
        sample = dict(event_id="fixture-event", revision=1, correlation={key: "fixture" for key in spec.correlation_keys},
                      occurred_at=now, content_digest=canonical_digest("fixture"))
        for kind in spec.event_classes:
            normalize_observation(dict(sample, event_class=kind), spec)
        malformed = dict(sample, event_class=spec.event_classes[0], injected="unknown")
        for bad in (malformed, dict(sample, event_class=spec.event_classes[0], correlation={}),
                    dict(sample, event_class=spec.event_classes[0], content_digest="invalid")):
            try:
                normalize_observation(bad, spec)
            except ValueError:
                pass
            else:
                raise WatcherError("observer negative fixture was accepted")
        expires = datetime.fromisoformat(now) + timedelta(seconds=spec.timeout_seconds)
        return ObserverReceipt(spec.source_id, canonical_digest(spec.query), spec.source_schema, spec.parser_version,
                               spec.fixture_digest, spec.event_classes, spec.correlation_keys, now, expires.isoformat())

    async def fetch(self, binding, spec, query, cursor):
        if query != spec.query:
            raise WatcherError("journal query changed")
        raw, meta, start = self._read(binding, spec)
        if cursor is not None:
            prior = _strict_json(cursor)
            if not isinstance(prior, dict) or set(prior) != {"device", "inode", "offset", "prefix_digest"}:
                raise WatcherError("invalid journal cursor")
            if any(type(prior[key]) is not int or prior[key] < 0 for key in ("device", "inode")):
                raise WatcherError("invalid journal filesystem identity")
            offset = _integer(prior["offset"], "journal offset", start, len(raw))
            if (prior["device"] != meta.st_dev or prior["inode"] != meta.st_ino
                    or prior["prefix_digest"] != "sha256:" + hashlib.sha256(raw[:offset]).hexdigest()):
                raise WatcherError("journal rotated or previously observed bytes changed")
            start = offset
        observations, offset = [], start
        for line in raw[start:].splitlines(keepends=True):
            if not line.endswith(b"\n") or len(observations) >= spec.max_batch_events:
                break
            if len(line) > 65536:
                raise WatcherError("journal record exceeds its supported bound")
            observations.append(normalize_observation(_strict_json(line), spec))
            offset += len(line)
        next_cursor = json.dumps(dict(device=meta.st_dev, inode=meta.st_ino, offset=offset,
                                      prefix_digest="sha256:" + hashlib.sha256(raw[:offset]).hexdigest()), sort_keys=True, separators=(",", ":"))
        return dict(observations=observations, next_cursor=next_cursor)
