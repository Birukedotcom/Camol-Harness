import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camol.orchestrator import Orchestrator
from camol.runbook import load_runbook
from camol.schema import canonical_digest
from camol.state import project
from camol.store import ConcurrentAppendError, SQLiteEventStore
from camol.watchers import ObserverReceipt, Watcher, WatcherError, WatchSpec, apply_watcher_event


ROOT = Path(__file__).resolve().parents[1]


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "run.db"
        self.store = SQLiteEventStore(self.path)
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.orchestrator = Orchestrator(self.store, clock=lambda: self.now)
        initial = self.orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
        self.run_id = initial["run_id"]
        self.orchestrator.approve_plan(self.run_id, "owner", initial["plan_digest"])
        self.spec = WatchSpec("calls", "fixture-logs", "calls for campaign-one", ("started", "ended"),
                              ("job", "session"), "ended", (("job", "job-one"),),
                              "voice-v1", "parser-v1", canonical_digest("fixture-one"))
        self.receipt = ObserverReceipt(
            self.spec.source_id, canonical_digest(self.spec.query), self.spec.source_schema,
            self.spec.parser_version, self.spec.fixture_digest, self.spec.event_classes,
            self.spec.correlation_keys, self.now.isoformat(), (self.now + timedelta(minutes=5)).isoformat())
        self.watcher = Watcher.create(self.orchestrator, self.run_id, self.spec, approved_by="owner")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, *, event_id="event-one", revision=1, event_class="started", job="job-one", content="content"):
        return dict(event_id=event_id, revision=revision, event_class=event_class,
                    correlation=dict(job=job, session="session-one"), occurred_at=self.now.isoformat(),
                    content_digest=canonical_digest(content))

    async def batch(self, observations, cursor="cursor-one", *, watcher=None, receipt=None):
        async def fetch(query, previous):
            self.assertEqual(query, self.spec.query)
            return dict(observations=observations, next_cursor=cursor)
        return await (watcher or self.watcher).poll(fetch, receipt or self.receipt)

    async def test_cursor_restart_dedup_and_correlated_terminality(self):
        first = self.event()
        await self.batch([first])
        self.store.close()
        self.store = SQLiteEventStore(self.path)
        resumed = Orchestrator(self.store, clock=lambda: self.now)
        watcher = Watcher(resumed, self.run_id, "calls")
        self.assertEqual(watcher.inspect()["cursor"], "cursor-one")
        result = await self.batch([first, self.event(event_id="unrelated", event_class="ended", job="other-job")],
                                  "cursor-two", watcher=watcher)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(len(result["observations"]), 2)
        result = await self.batch([self.event(event_id="terminal", event_class="ended")], "cursor-three", watcher=watcher)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(project(self.store.read(self.run_id)), resumed.state(self.run_id))
        async def never(*args):
            self.fail("completed watcher queried its source")
        self.assertEqual((await watcher.poll(never, self.receipt))["status"], "completed")

    async def test_empty_expired_future_mismatched_observer_never_completes(self):
        self.assertEqual((await self.batch([]))["status"], "waiting")
        async def never(*args):
            self.fail("unready observer queried its source")
        for field, replacement in (("parser_version", "parser-v2"), ("fixture_digest", canonical_digest("other")),
                                   ("source_schema", "voice-v2"), ("query_digest", canonical_digest("other")),
                                   ("observed_at", (self.now + timedelta(seconds=1)).isoformat()),
                                   ("expires_at", self.now.isoformat())):
            payload = self.receipt.to_dict()
            payload[field] = replacement
            if field == "expires_at":
                payload["observed_at"] = (self.now - timedelta(seconds=1)).isoformat()
            result = await self.watcher.poll(never, ObserverReceipt.from_dict(payload))
            self.assertEqual(result["status"], "waiting")
            self.assertIn("OBSERVATION_INCOMPLETE", result["blocker"])
            self.assertEqual(result["cursor"], "cursor-one")

    async def test_source_errors_schema_drift_and_expiry_during_poll_preserve_cursor(self):
        await self.batch([self.event()])
        async def broken(*args):
            raise RuntimeError("sensitive provider body")
        result = await self.watcher.poll(broken, self.receipt)
        self.assertEqual(result["cursor"], "cursor-one")
        self.assertNotIn("sensitive", str(self.store.read(self.run_id)))
        result = await self.batch([self.event(event_class="new-unknown-class")], "bad-cursor")
        self.assertEqual(result["cursor"], "cursor-one")
        async def slow(*args):
            self.now += timedelta(minutes=6)
            return dict(observations=[self.event(event_class="ended")], next_cursor="expired-cursor")
        result = await self.watcher.poll(slow, self.receipt)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["cursor"], "cursor-one")

    async def test_timeout_and_cancellation_do_not_claim_absence(self):
        async def timeout(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError()
        async def fetch(*args):
            return dict(observations=[], next_cursor="unused")
        with patch("camol.watchers.asyncio.wait_for", timeout):
            result = await self.watcher.poll(fetch, self.receipt)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["blocker"], "poll_timeout")
        before = self.store.read(self.run_id)
        async def cancelled(*args):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.watcher.poll(cancelled, self.receipt)
        self.assertEqual(self.store.read(self.run_id), before)

    async def test_conflicting_revision_is_not_silently_reconciled_or_reopened(self):
        await self.batch([self.event()])
        result = await self.batch([self.event(content="different")], "must-not-advance")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["cursor"], "cursor-one")
        with self.assertRaises(WatcherError):
            self.watcher.reopen(approved_by="owner", reason="try again")
        with self.assertRaises(WatcherError):
            apply_watcher_event(self.orchestrator.state(self.run_id), dict(type="WATCHER_WAITING",
                                payload=dict(watcher_id="calls", reason="clear", observed_at=self.now.isoformat())))

    async def test_human_reopen_requires_a_new_revision_not_a_duplicate_terminal(self):
        terminal = self.event(event_class="ended")
        await self.batch([terminal])
        with self.assertRaises(WatcherError):
            self.watcher.reopen(approved_by="worker", reason="recheck")
        self.watcher.reopen(approved_by="owner", reason="review source correction")
        self.assertEqual((await self.batch([terminal]))["status"], "waiting")
        self.assertEqual((await self.batch([dict(terminal, revision=2)]))["status"], "completed")
        self.assertEqual(len(self.watcher.inspect()["observations"]), 2)

    async def test_future_source_event_cannot_complete_and_replay_cannot_forge_owner(self):
        future = dict(self.event(event_class="ended"), occurred_at=(self.now + timedelta(days=1)).isoformat())
        result = await self.batch([future], "future-cursor")
        self.assertEqual(result["status"], "waiting")
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["observations"], {})
        await self.batch([self.event(event_class="ended")], "actual-cursor")
        self.watcher.reopen(approved_by="owner", reason="reviewed continuation")
        for kind in ("WATCHER_CREATED", "WATCHER_REOPENED"):
            events = copy.deepcopy(self.store.read(self.run_id))
            next(event for event in events if event["type"] == kind)["actor_id"] = "worker"
            with self.assertRaises(WatcherError):
                project(events)

    async def test_concurrent_polls_cannot_overwrite_the_cursor(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return dict(observations=[self.event(event_id="stale")], next_cursor="stale")
        pending = asyncio.create_task(self.watcher.poll(delayed, self.receipt))
        await entered.wait()
        await self.batch([self.event()], "fresh")
        release.set()
        with self.assertRaises(ConcurrentAppendError):
            await pending
        self.assertEqual(self.watcher.inspect()["cursor"], "fresh")
        self.assertEqual(len(self.watcher.inspect()["observations"]), 1)

    async def test_redaction_never_corrupts_an_opaque_resume_cursor(self):
        from camol.probes import Redactor
        self.orchestrator.redactor = Redactor(env={"TEST_TOKEN": "private-cursor"})
        result = await self.batch([self.event()], "private-cursor")
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["observations"], {})
        self.assertIn("OBSERVATION_INCOMPLETE", result["blocker"])
        self.assertNotIn("private-cursor", str(self.store.read(self.run_id)))

    def test_exact_contract_unknown_fields_and_correlation_binding(self):
        self.assertEqual(WatchSpec.from_dict(self.spec.to_dict()), self.spec)
        for field, value in (("unknown", True), ("correlation_filter", {}), ("event_classes", [[], "ended"]),
                             ("schema_version", True)):
            payload = dict(self.spec.to_dict(), **{field: value})
            with self.assertRaises(ValueError):
                WatchSpec.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
