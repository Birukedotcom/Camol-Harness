import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from camol.orchestrator import Orchestrator
from camol.runbook import load_runbook
from camol.schema import canonical_digest
from camol.state import project
from camol.store import SQLiteEventStore
from camol.watchers import WatchSpec, Watcher, WatcherError
from camol.watch_runtime import JournalSource, WatchRuntime, journal_fixture_digest, journal_header


ROOT = Path(__file__).resolve().parents[1]


class WatchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = SQLiteEventStore(self.root / "events.db")
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.orchestrator = Orchestrator(self.store, clock=lambda: self.now)
        initial = self.orchestrator.initialize(load_runbook(ROOT / "examples/three-agent-runbook.json"))
        self.run_id = initial["run_id"]
        self.orchestrator.approve_plan(self.run_id, "owner", initial["plan_digest"])
        spec = WatchSpec("calls", "calls-local", "journal:calls-local", ("started", "ended"),
                         ("job", "session"), "ended", (("job", "job-one"),), "voice-v1", "jsonl-v1",
                         canonical_digest("placeholder"), timeout_seconds=2, max_batch_events=2)
        self.spec = replace(spec, fixture_digest=journal_fixture_digest(spec))
        self.watcher = Watcher.create(self.orchestrator, self.run_id, self.spec, approved_by="owner")
        self.journal = self.root / "source.jsonl"
        self.write_journal([])
        self.source = JournalSource()
        self.runtime = WatchRuntime(self.orchestrator, self.run_id)
        self.schedule = dict(schema="camol.watch_schedule", schema_version=1, watcher_id="calls",
                             binding=self.source.binding(self.spec.source_id, self.journal), interval_seconds=5,
                             max_polls=3, expires_at=(self.now + timedelta(hours=1)).isoformat())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, event_id="first", *, event_class="started", job="job-one"):
        return dict(event_id=event_id, revision=1, event_class=event_class, correlation=dict(job=job, session="session-one"),
                    occurred_at=self.now.isoformat(), content_digest=canonical_digest(event_id))

    def write_journal(self, events):
        self.journal.write_text("".join(json.dumps(value) + "\n" for value in [journal_header(self.spec)] + events))
        self.journal.chmod(0o600)

    def append(self, event, *, newline=True):
        with self.journal.open("a") as handle:
            handle.write(json.dumps(event) + ("\n" if newline else ""))

    def configure(self):
        return self.runtime.configure(self.schedule, approved_by="owner", approval_digest=canonical_digest(self.schedule))

    async def test_actual_journal_survives_store_and_runtime_restart_to_correlated_terminal(self):
        self.configure()
        self.append(self.event())
        result = await self.runtime.tick()
        self.assertEqual(result["calls"]["status"], "waiting")
        self.assertEqual(result["calls"]["scheduled_polls"], 1)
        self.configure()
        self.assertEqual(self.watcher.inspect()["scheduled_polls"], 1, "approval retry must not reset the poll budget")
        self.assertEqual(len(result["calls"]["observations"]), 1)
        seq = self.orchestrator.state(self.run_id)["last_seq"]
        await self.runtime.tick()
        self.assertEqual(self.orchestrator.state(self.run_id)["last_seq"], seq)
        self.store.close()
        self.store = SQLiteEventStore(self.root / "events.db")
        self.orchestrator = Orchestrator(self.store, clock=lambda: self.now)
        self.runtime = WatchRuntime(self.orchestrator, self.run_id)
        self.append(self.event("unrelated", event_class="ended", job="other"))
        self.now += timedelta(seconds=5)
        self.assertEqual((await self.runtime.tick())["calls"]["status"], "waiting")
        self.append(self.event("terminal", event_class="ended"))
        self.now += timedelta(seconds=5)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scheduled_polls"], 3)
        self.assertIsNone(result["active_poll"])
        self.assertEqual(project(self.store.read(self.run_id)), self.orchestrator.state(self.run_id))

    async def test_empty_partial_and_budget_exhaustion_are_not_completion(self):
        self.configure()
        await self.runtime.tick()
        self.append(self.event("partial", event_class="ended"), newline=False)
        self.now += timedelta(seconds=5)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["observations"], {})
        self.assertEqual(result["status"], "waiting")
        self.now += timedelta(seconds=5)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["scheduler_state"], "poll_budget_exhausted")
        self.assertEqual(result["status"], "waiting")
        seq = self.orchestrator.state(self.run_id)["last_seq"]
        self.now += timedelta(days=1)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["scheduler_state"], "approval_expired")
        self.assertEqual(self.orchestrator.state(self.run_id)["last_seq"], seq)

    async def test_duplicate_json_keys_cannot_complete_watch_or_advance_cursor(self):
        self.configure()
        terminal = json.dumps(self.event("ambiguous", event_class="ended"))
        ambiguous = terminal.replace('"event_class": "ended"', '"event_class": "started", "event_class": "ended"')
        with self.journal.open("a") as handle:
            handle.write(ambiguous + "\n")
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["observations"], {})
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["last_attempt_outcome"], "unavailable")
        self.assertIn("OBSERVATION_INCOMPLETE", result["blocker"])

        nested = terminal.replace('"job": "job-one"', '"job": "unrelated", "job": "job-one"')
        self.journal.write_text(json.dumps(journal_header(self.spec)) + "\n" + nested + "\n")
        with self.assertRaises(WatcherError):
            await self.source.fetch(self.schedule["binding"], self.spec, self.spec.query, None)
        self.write_journal([])
        valid = await self.source.fetch(self.schedule["binding"], self.spec, self.spec.query, None)
        cursor = valid["next_cursor"]
        with self.assertRaises(WatcherError):
            await self.source.fetch(self.schedule["binding"], self.spec, self.spec.query, '{"offset":0,' + cursor[1:])
        header = json.dumps(journal_header(self.spec))
        self.journal.write_text('{"schema":"wrong",' + header[1:] + "\n")
        with self.assertRaises(WatcherError):
            await self.source.probe(self.schedule["binding"], self.spec, self.now.isoformat())

    async def test_replay_rejects_worker_impersonation_of_observer_on_every_poll_event(self):
        self.configure()
        self.append(self.event("terminal", event_class="ended"))
        self.assertEqual((await self.runtime.tick())["calls"]["status"], "completed")
        events = self.store.read(self.run_id)
        for kind in ("WATCHER_POLL_STARTED", "WATCHER_BATCH_RECORDED", "WATCHER_POLL_FINISHED"):
            with self.subTest(kind=kind):
                forged = deepcopy(events)
                next(item for item in forged if item["type"] == kind)["actor_id"] = "builder"
                with self.assertRaisesRegex(WatcherError, "trusted observer actor"):
                    project(forged)

    async def test_prior_byte_mutation_rotation_permissions_and_symlink_preserve_cursor(self):
        self.configure()
        self.append(self.event())
        await self.runtime.tick()
        prior = self.watcher.inspect()["cursor"]
        original = self.journal.read_bytes()
        self.journal.write_bytes(original.replace(b'"first"', b'"other"'))
        self.now += timedelta(seconds=5)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["cursor"], prior)
        self.assertIn("OBSERVATION_INCOMPLETE", result["blocker"])
        self.journal.write_bytes(original)
        self.journal.chmod(0o666)
        with self.assertRaises(WatcherError):
            await self.source.probe(self.schedule["binding"], self.spec, self.now.isoformat())
        self.journal.chmod(0o600)
        other = self.root / "moved.jsonl"
        self.journal.rename(other)
        self.journal.symlink_to(other)
        with self.assertRaises(WatcherError):
            await self.source.probe(self.schedule["binding"], self.spec, self.now.isoformat())

    async def test_bound_plugin_change_and_unknown_source_never_fetch(self):
        self.configure()
        class Trap:
            plugin_id = JournalSource.plugin_id
            plugin_digest = canonical_digest("wrong-code")
            async def probe(self, *args):
                raise AssertionError("must never call substituted plugin")
        self.runtime = WatchRuntime(self.orchestrator, self.run_id, sources=[Trap()])
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["last_attempt_outcome"], "unavailable")
        self.assertEqual(result["scheduled_polls"], 1)
        self.assertIsNone(result["cursor"])
        self.assertNotIn("wrong-code", str(self.store.read(self.run_id)))

    async def test_unrelated_run_events_during_fetch_do_not_discard_valid_observation(self):
        self.configure()
        self.append(self.event())
        base, orchestrator, run_id, spec = self.source, self.orchestrator, self.run_id, self.spec
        class Interleaved:
            plugin_id = base.plugin_id
            plugin_digest = base.plugin_digest
            validate = base.validate
            probe = base.probe
            async def fetch(self, *args):
                Watcher.create(orchestrator, run_id, replace(spec, watcher_id="unrelated"), approved_by="owner")
                return await base.fetch(*args)
        self.runtime = WatchRuntime(self.orchestrator, self.run_id, sources=[Interleaved()])
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["last_attempt_outcome"], "observed")
        self.assertEqual(len(result["observations"]), 1)
        self.assertIsNotNone(result["cursor"])
        self.assertEqual(result["scheduled_polls"], 1)

    async def test_exact_owner_approval_and_replay_reject_mutated_schedule(self):
        with self.assertRaises(WatcherError):
            self.runtime.configure(self.schedule, approved_by="owner", approval_digest=canonical_digest("different"))
        with self.assertRaises(WatcherError):
            self.runtime.configure(self.schedule, approved_by="worker", approval_digest=canonical_digest(self.schedule))
        self.configure()
        events = deepcopy(self.store.read(self.run_id))
        events[-1]["actor_id"] = "worker"
        with self.assertRaises(WatcherError):
            project(events)
        events = deepcopy(self.store.read(self.run_id))
        events[-1]["payload"]["schedule"]["max_polls"] += 1
        with self.assertRaises(WatcherError):
            project(events)
        with self.assertRaises(WatcherError):
            self.runtime.stop("calls", approved_by="worker", reason="hide watcher")
        result = self.runtime.stop("calls", approved_by="owner", reason="cancel observation")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual((await self.runtime.tick())["calls"]["scheduled_polls"], 0)

    async def test_schedule_expiry_during_fetch_cannot_accept_an_observation(self):
        self.schedule["expires_at"] = (self.now + timedelta(seconds=1)).isoformat()
        self.configure()
        self.append(self.event("terminal", event_class="ended"))
        base, fixture = self.source, self
        class Expired:
            plugin_id = base.plugin_id
            plugin_digest = base.plugin_digest
            validate = base.validate
            probe = base.probe
            async def fetch(self, *args):
                fixture.now += timedelta(seconds=1.5)
                return await base.fetch(*args)
        self.runtime = WatchRuntime(self.orchestrator, self.run_id, sources=[Expired()])
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["scheduler_state"], "approval_expired")
        self.assertEqual(result["observations"], {})
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["last_attempt_outcome"], "unavailable")

    async def test_unknown_prior_attempt_is_charged_and_not_reissued_before_deadline(self):
        self.configure()
        self.watcher._commit("WATCHER_POLL_STARTED", dict(attempt_id="crashed", schedule_digest=canonical_digest(self.schedule), observed_at=self.now.isoformat()))
        self.runtime = WatchRuntime(self.orchestrator, self.run_id)
        self.assertEqual((await self.runtime.tick())["calls"]["active_poll"]["attempt_id"], "crashed")
        self.now += timedelta(seconds=2)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["last_attempt_outcome"], "recovered_unknown")
        self.assertEqual(result["scheduled_polls"], 1)
        self.assertIsNone(result["active_poll"])
        self.now += timedelta(seconds=3)
        result = (await self.runtime.tick())["calls"]
        self.assertEqual(result["scheduled_polls"], 2)

    async def test_cancellation_waits_for_source_and_finalizes_attempt_without_progress(self):
        self.configure()
        entered, ended = asyncio.Event(), asyncio.Event()
        base = self.source
        class Delayed:
            plugin_id = base.plugin_id
            plugin_digest = base.plugin_digest
            validate = base.validate
            probe = base.probe
            async def fetch(self, *args):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    ended.set()
        self.runtime = WatchRuntime(self.orchestrator, self.run_id, sources=[Delayed()])
        pending = asyncio.create_task(self.runtime.tick())
        await entered.wait()
        with self.assertRaises(WatcherError):
            self.runtime.stop("calls", approved_by="owner", reason="active")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(ended.is_set())
        result = self.watcher.inspect()
        self.assertIsNone(result["active_poll"])
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["last_attempt_outcome"], "interrupted")
        self.assertEqual(result["scheduled_polls"], 1)


if __name__ == "__main__":
    unittest.main()
