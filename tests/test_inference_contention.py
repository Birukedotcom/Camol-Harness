"""Read-only heartbeat contention must not become inference retry authority."""

import sqlite3
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from camol import model_inference
from camol.model_inference import InferenceLedgerUnavailable
from camol.models import ModelError
from tests import test_model_inference as fixtures


class InferenceHostContentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModelInferenceTests()
        self.fixture.setUp()
        self.addCleanup(self._close_fixture)

    def _close_fixture(self):
        self.fixture.doCleanups()
        self.fixture.tearDown()

    def _hold_host(self, seconds):
        entered = threading.Event()
        failures = []

        def writer():
            try:
                with closing(sqlite3.connect(str(self.fixture.host.database))) as connection:
                    connection.execute("BEGIN EXCLUSIVE")
                    entered.set()
                    time.sleep(seconds)
                    connection.rollback()
            except BaseException as error:
                failures.append(error)
                entered.set()

        thread = threading.Thread(target=writer)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.assertTrue(entered.wait(5), "fixture writer failed to acquire its bounded lock")
        self.assertEqual(failures, [])
        return thread

    def test_short_host_commit_wait_reobserves_only_reads_before_approval(self):
        case = self.fixture
        plan = case.start()
        case.inference.prepare(plan)
        self._hold_host(.15)
        result = case.inference.approve(plan.digest(), "owner")
        self.assertEqual(result["status"], "approved")
        self.assertFalse(case.posts())
        self.assertEqual([event["type"] for event in case.inference.events(plan.digest())],
                         ["MODEL_INFERENCE_PREPARED", "MODEL_INFERENCE_APPROVED"])

    def test_tampered_prompt_denial_survives_transient_host_commit(self):
        case = self.fixture
        plan = case.start()
        case.approve_request(plan)
        case.prompt.write_text("not the approved prompt")
        self._hold_host(.15)
        with self.assertRaisesRegex(ModelError, "exact approved bytes"):
            case.infer(plan)
        self.assertEqual(case.inference.status(plan.digest())["status"], "approved")
        self.assertFalse(case.posts())

    def test_infer_deadline_includes_initial_read_wait_before_one_post(self):
        case = self.fixture
        plan = replace(case.start(), timeout_seconds=1)
        case.approve_request(plan)
        self._hold_host(.20)
        remaining = []
        original = model_inference._post

        def observe(port, payload, deadline, *args):
            remaining.append(deadline - time.monotonic())
            return original(port, payload, deadline, *args)

        with patch("camol.model_inference._post", side_effect=observe):
            result = case.infer(plan)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(remaining), 1)
        self.assertLess(remaining[0], .85, "host read wait must consume the original one-second deadline")
        self.assertEqual(len(case.posts()), 1)
        self.assertEqual(case.infer(plan)["status"], "completed")
        self.assertEqual(len(case.posts()), 1)

    def test_cancel_during_host_read_keeps_approval_and_never_writes_intent(self):
        case = self.fixture
        plan = replace(case.start(), timeout_seconds=1)
        case.approve_request(plan)
        self._hold_host(.40)
        cancelled = threading.Event()
        timer = threading.Timer(.08, cancelled.set)
        timer.start()
        self.addCleanup(timer.join, 1)
        began = time.monotonic()
        with self.assertRaisesRegex(ModelError, "cancelled"):
            case.infer(plan, cancel_event=cancelled)
        self.assertLess(time.monotonic() - began, .35)
        self.assertFalse(case.posts())
        self.assertEqual(case.inference.status(plan.digest())["status"], "approved")
        self.assertFalse(any(event["type"] == "MODEL_INFERENCE_INTENT"
                             for event in case.inference.events(plan.digest())))

    def test_persistent_host_busy_expires_without_an_intent_or_post(self):
        case = self.fixture
        plan = replace(case.start(), timeout_seconds=1)
        case.approve_request(plan)
        self._hold_host(1.35)
        began = time.monotonic()
        with self.assertRaisesRegex(ModelError, "deadline"):
            case.infer(plan)
        self.assertLess(time.monotonic() - began, 1.25)
        self.assertFalse(case.posts())
        self.assertEqual(case.inference.status(plan.digest())["status"], "approved")
        self.assertFalse(any(event["type"] == "MODEL_INFERENCE_INTENT"
                             for event in case.inference.events(plan.digest())))

    def test_corrupt_database_error_is_not_retried_or_mislabeled_as_contention(self):
        case = self.fixture
        plan = case.start()
        case.inference.prepare(plan)
        with patch.object(case.inference.host, "_plan", side_effect=sqlite3.DatabaseError(
                "database disk image is malformed PRIVATE_RAW_DETAIL")) as reader:
            with self.assertRaises(InferenceLedgerUnavailable) as caught:
                case.inference.approve(plan.digest(), "owner")
        self.assertEqual(reader.call_count, 1)
        self.assertNotIn("PRIVATE_RAW_DETAIL", str(caught.exception))
        self.assertFalse(case.posts())

    def test_expiry_during_passive_read_wait_cannot_approve_stale_authority(self):
        case = self.fixture
        plan = case.start()
        plan = replace(plan, expires_at=(datetime.now(timezone.utc) + timedelta(seconds=.20)).isoformat())
        case.inference.prepare(plan)
        self._hold_host(.30)
        with self.assertRaisesRegex(ModelError, "expired"):
            case.inference.approve(plan.digest(), "owner")
        self.assertEqual(case.inference.status(plan.digest())["status"], "prepared")
        self.assertFalse(case.posts())


if __name__ == "__main__":
    unittest.main()
