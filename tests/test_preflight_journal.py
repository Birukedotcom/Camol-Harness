import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from camol.preflight_journal import PreflightJournal, PreflightJournalError
from camol.preflight_process import bounded_preflight_run
from camol.providers import ProviderError, create_claude_capability, read_capability
from tests.test_providers import Completed, Help
from tests import test_providers as provider_fixtures


class PreflightRecoveryTests(unittest.TestCase):
    """All requests here are local fake runtimes or injected fixtures, not models."""

    setUp = provider_fixtures.ProviderContractTests.setUp
    tearDown = provider_fixtures.ProviderContractTests.tearDown

    def call(self, runner, operation_id="first", **kwargs):
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            return create_claude_capability(self.profile, target_id="local:test", state_dir=self.state,
                                           cwd=self.workspace, accept_spend=True, now=kwargs.pop("now", self.now),
                                           operation_id=operation_id, runner=runner, **kwargs)

    def inventory(self):
        with PreflightJournal(self.state, read_only=True) as journal:
            return journal.inventory()

    def test_success_replay_spends_once_and_cannot_renew_or_rebind(self):
        calls = []
        def runner(argv, **kwargs):
            if "--help" in argv:
                return Help()
            calls.append(argv)
            self.assertNotEqual(Path(kwargs["cwd"]), self.workspace)
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
            return Completed()
        first = self.call(runner)
        self.assertEqual(self.call(runner).digest(), first.digest())
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ProviderError, "stale"):
            self.call(runner, now=self.now + timedelta(seconds=61))
        with self.assertRaisesRegex(ProviderError, "different request"):
            self.call(runner, spend_ceiling_cents=1)
        self.assertEqual(len(calls), 1)
        row = self.inventory()[0]
        self.assertEqual(row["outcome"]["usage"]["cost_usd_micros"], 2000)
        self.assertEqual(row["outcome"]["status"], "succeeded")
        self.assertIsNotNone(read_capability(self.state, self.profile))

    def test_timeout_unknown_blocks_same_and_new_ids_without_raw_output(self):
        calls = []
        def runner(argv, **kwargs):
            if "--help" in argv:
                return Help()
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 120, output=b"secret-provider-output")
        with self.assertRaises(ProviderError):
            self.call(runner)
        for operation in ("first", "new-attempt"):
            with self.assertRaisesRegex(ProviderError, "unresolved preflight usage"):
                self.call(runner, operation)
        self.assertEqual(len(calls), 1)
        rows = self.inventory()
        self.assertEqual(rows[0]["outcome"]["reason"], "timeout")
        self.assertIsNone(rows[0]["outcome"]["usage"]["cost_usd_micros"])
        self.assertNotIn("secret-provider-output", json.dumps(rows))
        self.assertIsNone(read_capability(self.state, self.profile))

    def test_known_overspend_and_provider_error_keep_actual_usage(self):
        for operation, update, reason, cost in (
            ("overspend", {"total_cost_usd": .5}, "overspend", 500000),
            ("provider-error", {"is_error": True}, "provider_error", 2000),
            ("provider-budget-error", {"subtype": "error_max_budget_usd"}, "provider_error", 2000),
            ("provider-null-subtype", {"subtype": None}, "provider_error", 2000),
            ("provider-typed-error", {"is_error": "true"}, "provider_error", 2000),
            ("provider-wrong-record", {"type": "system"}, "provider_error", 2000),
            ("wrong-model", {"modelUsage": {"unexpected": {}}}, "model_mismatch", 2000),
            ("hidden-model", {"model": "claude-fable-5", "modelUsage": {"claude-fable-5": {}, "unexpected": {}}}, "model_mismatch", 2000),
            ("conflicting-model", {"model": "unexpected"}, "model_mismatch", 2000),
            ("conflicting-alias-map", {"model_usage": {"unexpected": {}}}, "model_mismatch", 2000),
        ):
            class Bad(Completed):
                stdout = json.dumps(dict(json.loads(Completed.stdout), **update)).encode()
            def runner(argv, **kwargs):
                return Help() if "--help" in argv else Bad()
            with self.assertRaises(ProviderError):
                self.call(runner, operation)
            last = next(row for row in self.inventory() if row["intent"]["operation_id"] == operation)
            self.assertEqual(last["outcome"]["status"], "failed")
            self.assertEqual(last["outcome"]["reason"], reason)
            self.assertEqual(last["outcome"]["usage"]["cost_usd_micros"], cost)
            self.assertIsNone(last["outcome"]["receipt"])

    def test_success_cache_failure_recovers_without_another_model_request(self):
        calls = []
        def runner(argv, **kwargs):
            if "--help" in argv:
                return Help()
            calls.append(argv)
            return Completed()
        with patch("camol.providers._publish_capability", side_effect=OSError("fixture cache failure")):
            with self.assertRaises(ProviderError):
                self.call(runner)
        self.assertEqual(self.inventory()[0]["outcome"]["status"], "succeeded")
        self.call(runner)
        self.assertEqual(len(calls), 1)

    def test_later_uncertain_call_holds_prior_green_cache(self):
        self.call(lambda argv, **kwargs: Help() if "--help" in argv else Completed())
        self.assertIsNotNone(read_capability(self.state, self.profile))
        def uncertain(argv, **kwargs):
            if "--help" in argv:
                return Help()
            raise subprocess.TimeoutExpired(argv, 120)
        with self.assertRaises(ProviderError):
            self.call(uncertain, "later")
        self.assertIsNone(read_capability(self.state, self.profile))
        with self.assertRaisesRegex(ProviderError, "unresolved preflight usage"):
            self.call(lambda argv, **kwargs: Help() if "--help" in argv else self.fail("repeated"))

    def test_status_cli_is_noncreating_and_reports_known_cost_without_models(self):
        import contextlib
        import io
        from camol.cli import main
        missing = self.root / "missing-state"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["preflight-status", "--state-dir", str(missing)]), 2)
        self.assertFalse(missing.exists())
        self.call(lambda argv, **kwargs: Help() if "--help" in argv else Completed())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["preflight-status", "--state-dir", str(self.state)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["known_cost_usd_micros"], 2000)
        self.assertEqual(result["unresolved_reserved_usd_cents"], 0)
        self.assertFalse(result["held"])

    def test_missing_tokens_are_not_fabricated_as_zero(self):
        class Missing(Completed):
            stdout = json.dumps(dict(json.loads(Completed.stdout), usage={})).encode()
        with self.assertRaises(ProviderError):
            self.call(lambda argv, **kwargs: Help() if "--help" in argv else Missing())
        row = self.inventory()[0]
        self.assertEqual(row["outcome"]["status"], "failed")
        self.assertIsNone(row["outcome"]["usage"]["input_tokens"])
        self.assertIsNone(row["outcome"]["usage"]["output_tokens"])
        self.assertEqual(row["outcome"]["usage"]["cost_usd_micros"], 2000)

    def test_invalid_cache_count_cannot_be_reported_as_complete_input_usage(self):
        class InvalidCache(Completed):
            stdout = json.dumps(dict(json.loads(Completed.stdout), is_error=True,
                                     usage={"input_tokens": 10, "cache_read_input_tokens": "unknown", "output_tokens": 4})).encode()
        with self.assertRaises(ProviderError):
            self.call(lambda argv, **kwargs: Help() if "--help" in argv else InvalidCache())
        usage = self.inventory()[0]["outcome"]["usage"]
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["output_tokens"], 4)
        self.assertEqual(usage["cost_usd_micros"], 2000)

    def test_invalid_json_shapes_cannot_publish_success(self):
        for index, raw in enumerate((b'[]', b'{"usage":{},"usage":{}}', b'{"result":NaN}', b'not-json')):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                self.state = Path(temporary) / "state"
                class Bad(Completed):
                    stdout = raw
                with self.assertRaises(ProviderError):
                    self.call(lambda argv, **kwargs: Help() if "--help" in argv else Bad(), "bad-" + str(index))
                self.assertEqual(self.inventory()[0]["outcome"]["status"], "unknown")

    def test_concurrent_different_ids_cannot_spend_behind_an_unresolved_call(self):
        entered, finish = threading.Event(), threading.Event()
        failures, calls = [], []
        def runner(argv, **kwargs):
            if "--help" in argv:
                return Help()
            calls.append(argv)
            entered.set()
            if not finish.wait(5):
                raise AssertionError("fixture release timeout")
            return Completed()
        def first():
            try:
                self.call(runner)
            except BaseException as error:
                failures.append(error)
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ProviderError, "unresolved preflight usage"):
                    self.call(runner, "second")
            finally:
                finish.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(len(calls), 1)

    def test_real_caller_crash_leaves_a_durable_hold(self):
        source = """
import os
from pathlib import Path
from camol.providers import create_claude_capability, load_model_profile
from tests.test_providers import Help
def runner(argv, **kwargs):
    if '--help' in argv: return Help()
    os._exit(19)
create_claude_capability(load_model_profile(Path(WORKSPACE), 'profile.yaml'), target_id='local:test',
    state_dir=Path(STATE), cwd=Path(WORKSPACE), accept_spend=True, operation_id='crashed', runner=runner)
"""
        source = "WORKSPACE=" + repr(str(self.workspace)) + "\nSTATE=" + repr(str(self.state)) + "\n" + source
        environment = dict(os.environ, PATH=str(self.bin))
        process = subprocess.run([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(process.returncode, 19, process.stderr)
        row = self.inventory()[0]
        self.assertIsNone(row["outcome"])
        with self.assertRaisesRegex(ProviderError, "unresolved preflight usage"):
            self.call(lambda argv, **kwargs: Help() if "--help" in argv else self.fail("dispatched"), "after-crash")

    def test_untrusted_paths_and_missing_no_tools_flags_never_dispatch(self):
        with self.assertRaisesRegex(ProviderError, "no-tools"):
            self.call(lambda argv, **kwargs: Completed())
        self.assertFalse(self.state.exists())
        class MissingBudget(Help):
            stdout = Help.stdout.replace(b"--max-budget-usd", b"")
        with self.assertRaisesRegex(ProviderError, "no-tools"):
            self.call(lambda argv, **kwargs: MissingBudget() if "--help" in argv else self.fail("unverified budget dispatched"))
        self.assertFalse(self.state.exists())
        with PreflightJournal(self.state) as journal:
            self.assertEqual(journal.inventory(), [])
        self.assertEqual((self.state / "provider-preflights").stat().st_mode & 0o777, 0o700)
        with PreflightJournal(self.state, read_only=True) as journal:
            with self.assertRaisesRegex(PreflightJournalError, "read-only"):
                with journal._locked(write=True):
                    self.fail("write lock granted")
        lock = self.state / "provider-preflights" / "ledger.lock"
        lock.chmod(0o644)
        with self.assertRaises((PreflightJournalError, OSError)):
            PreflightJournal(self.state)

    def test_hidden_turn_flag_requires_recognized_local_parser_error(self):
        class Hidden(Help):
            stdout = Help.stdout.replace(b"--max-turns", b"")
        for accepted in (False, True):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[-3:] == ["--safe-mode", "--max-turns", "--help"]:
                    return subprocess.CompletedProcess(argv, 1, b"", b"error: option '--max-turns <turns>' argument '--help' is invalid. must be a number\n"
                                                       if accepted else b"error: unknown option '--max-turns'\n")
                if "--help" in argv:
                    return Hidden()
                return Completed()
            if accepted:
                self.call(runner, "supported-hidden")
                self.assertEqual(len([argv for argv in calls if "-p" in argv]), 1)
            else:
                with self.assertRaisesRegex(ProviderError, "no-tools"):
                    self.call(runner, "unsupported-hidden")
                self.assertFalse(any("-p" in argv for argv in calls))
                self.assertFalse(self.state.exists())

    def test_symlink_fifo_and_unknown_records_are_fail_closed(self):
        with PreflightJournal(self.state):
            pass
        root = self.state / "provider-preflights"
        trap = root / ("a" * 64 + ".intent.json")
        trap.symlink_to(self.profile_file)
        with self.assertRaises((PreflightJournalError, OSError)):
            self.inventory()
        trap.unlink()
        os.mkfifo(trap, 0o600)
        began = time.monotonic()
        with self.assertRaises((PreflightJournalError, OSError)):
            self.inventory()
        self.assertLess(time.monotonic() - began, 1)
        trap.unlink()
        (root / "unclassified").write_text("private-unknown")
        with self.assertRaisesRegex(PreflightJournalError, "unclassified"):
            self.inventory()

    def test_writable_state_and_nonsticky_ancestor_cannot_hide_unknown_holds(self):
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o777)
        with self.assertRaisesRegex(PreflightJournalError, "ancestry"):
            PreflightJournal(self.state)
        self.assertFalse((self.state / "provider-preflights").exists())
        self.state.chmod(0o700)
        with PreflightJournal(self.state):
            pass
        original = self.root.stat().st_mode & 0o777
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(PreflightJournalError, "ancestry"):
                PreflightJournal(self.state)
        finally:
            self.root.chmod(original)


class BoundedPreflightProcessTests(unittest.TestCase):
    def run_python(self, code, timeout=2, input=None):
        return bounded_preflight_run([sys.executable, "-c", code], cwd=tempfile.gettempdir(), env={},
                                     input=input, timeout=timeout)

    def test_success_and_input_and_output_limit(self):
        result = self.run_python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", input=b"fixed-prompt")
        self.assertEqual(result.stdout, b"fixed-prompt")
        with self.assertRaisesRegex(OSError, "output ceiling"):
            self.run_python("import os; os.write(1,b'x'*2000000)")

    def test_timeout_reaps_owned_runtime(self):
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_python("import time; time.sleep(30)", timeout=.1)
        self.assertLess(time.monotonic() - began, 3)

    def test_descendant_with_open_pipe_does_not_defeat_deadline(self):
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_python("import os,time; pid=os.fork(); time.sleep(30) if pid==0 else os._exit(0)", timeout=.2)
        self.assertLess(time.monotonic() - began, 3)


if __name__ == "__main__":
    unittest.main()
