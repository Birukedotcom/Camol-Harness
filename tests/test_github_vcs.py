import copy
import json
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

from camol import github_vcs as github
from camol.vcs import VCSError


TARGET = dict(schema="camol.github_vcs_target", schema_version=1, repository="owner/project", branch="feature/test", pull_request=12)
REVISION = "a" * 40
SECRET = "private-body-and-credential-sentinel"


def responses(value=TARGET, revision=REVISION):
    result = []
    for request in github.requests(value, revision):
        key = request["key"]
        if key.startswith("branch_"):
            body = dict(ref="refs/heads/" + value["branch"], object=dict(type="commit", sha=revision), ignored=SECRET)
        elif key.startswith("pull_"):
            body = dict(id=321, number=value["pull_request"], head=dict(sha=revision, ref=value["branch"], repo=dict(full_name=value["repository"])),
                base=dict(sha="b" * 40, repo=dict(full_name=value["repository"])), state="open", draft=False, merged=False,
                merge_commit_sha=None, body=SECRET)
        elif key == "checks":
            body = dict(total_count=1, check_runs=[dict(id=1, head_sha=revision, name="unit", status="completed", conclusion="success", output=dict(text=SECRET))])
        elif key == "statuses":
            body = dict(sha=revision, total_count=1, state="success", statuses=[dict(id=2, context="build", state="success", description=SECRET)])
        else:
            body = [dict(id=3, state="APPROVED", commit_id=revision, submitted_at="2026-09-08T00:00:00Z", body=SECRET)]
        result.append(dict(key=key, status=200, more=False, body=json.dumps(body)))
    return result


def change(raw, key, mutate):
    row = next(row for row in raw if row["key"] == key)
    body = json.loads(row["body"])
    mutate(body)
    row["body"] = json.dumps(body)


class GithubVCSParserTests(unittest.TestCase):
    def test_exact_heads_required_and_minimized_fields_cannot_claim_approval(self):
        raw = responses()
        report = github.normalize(raw, TARGET, REVISION)
        self.assertTrue(report["branch_readback"]["matches_integration"])
        self.assertTrue(report["pull_request"]["matches_integration"])
        self.assertFalse(report["execution_authority"])
        self.assertEqual(report["review_state"], "observed_reviews_not_approval_policy")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertEqual(github.validate_result(report, TARGET, REVISION), report)
        change(raw, "branch_after", lambda value: value["object"].update(sha="c" * 40))
        self.assertFalse(github.normalize(raw, TARGET, REVISION)["branch_readback"]["matches_integration"])
        raw = responses()
        for key in ("pull_before", "pull_after"):
            change(raw, key, lambda value: value["head"]["repo"].update(full_name="different/fork"))
        self.assertFalse(github.normalize(raw, TARGET, REVISION)["pull_request"]["matches_integration"])

    def test_partial_collections_failed_checks_and_404_are_observations_not_readiness(self):
        raw = responses()
        change(raw, "checks", lambda value: value.update(total_count=500))
        change(raw, "statuses", lambda value: value.update(state="failure"))
        next(item for item in raw if item["key"] == "reviews")["more"] = True
        raw[-1].update(status=404, body=None)
        report = github.normalize(raw, TARGET, REVISION)
        by_key = {row["key"]: row["data"] for row in report["observations"]}
        self.assertFalse(by_key["checks"]["complete"])
        self.assertFalse(by_key["reviews"]["complete"])
        self.assertEqual(by_key["statuses"]["state"], "failure")
        self.assertFalse(report["branch_readback"]["matches_integration"])
        self.assertIsNone(report["branch_readback"]["after_sha"])
        self.assertEqual(github.validate_result(report, TARGET, REVISION), report)

    def test_foreign_duplicate_missing_and_tampered_identity_fields_are_rejected(self):
        for key, mutate in (
            ("checks", lambda body: body["check_runs"][0].update(head_sha="c" * 40)),
            ("checks", lambda body: body["check_runs"].append(dict(body["check_runs"][0]))),
            ("pull_after", lambda body: body.update(number=13)),
            ("pull_after", lambda body: body["base"]["repo"].update(full_name="foreign/repo")),
            ("statuses", lambda body: body.update(sha="b" * 40)),
            ("checks", lambda body: body.pop("total_count")),
        ):
            raw = responses()
            change(raw, key, mutate)
            with self.assertRaises(ValueError):
                github.normalize(raw, TARGET, REVISION)
        raw = responses()
        raw[0]["body"] = '{"ref":"one","ref":"two"}'
        with self.assertRaises(ValueError):
            github.normalize(raw, TARGET, REVISION)
        report = github.normalize(responses(), TARGET, REVISION)
        for mutate in (lambda row: row.update(extra="unknown"), lambda row: row.update(execution_authority=0),
                       lambda row: row["branch_readback"].update(matches_integration=1)):
            altered = copy.deepcopy(report)
            mutate(altered)
            with self.assertRaises(ValueError):
                github.validate_result(altered, TARGET, REVISION)

    def test_manifest_cannot_select_host_credentials_path_traversal_or_options(self):
        for edits in ({"host": "evil.test"}, {"token": SECRET}, {"repository": "https://github.com/a/b"},
                      {"repository": "owner/.."}, {"branch": "../other"}, {"branch": "a?query=x"},
                      {"branch": "a.lock"}, {"schema_version": True}, {"pull_request": True}):
            with self.assertRaises(ValueError):
                github.target(dict(TARGET, **edits))
        with patch("camol.github_vcs.bounded_preflight_run", side_effect=AssertionError("no process")):
            for token in ("x\nAuthorization: other", "x" * 2049, ""):
                with self.assertRaises(ValueError):
                    github.fetch(TARGET, REVISION, token=token)
            for timeout in (True, 0, float("nan"), 61):
                with self.assertRaises(ValueError):
                    github.fetch(TARGET, REVISION, timeout=timeout)
        self.assertEqual(len(github.requests(dict(TARGET, pull_request=None), REVISION)), 4)

    def test_capture_redacts_explicit_credential_echo_but_replay_ignores_later_environment(self):
        raw = responses()
        change(raw, "checks", lambda value: value["check_runs"][0].update(name="stable-check-name"))
        report = github.normalize(raw, TARGET, REVISION)
        with patch.dict(os.environ, UNRELATED_SERVICE_TOKEN="stable-check-name"):
            self.assertEqual(github.validate_result(report, TARGET, REVISION), report)
        safe = github.redact_result(report, TARGET, REVISION, token="stable-check-name")
        self.assertNotIn("stable-check-name", json.dumps(safe))
        self.assertIn("[REDACTED]", json.dumps(safe))
        self.assertEqual(github.validate_result(safe, TARGET, REVISION), safe)

    def test_real_bounded_subprocess_runs_only_fixed_get_and_keeps_credentials_off_argv_env(self):
        raw = responses()
        # Real isolated child; only HTTPSConnection is replaced by a deterministic
        # provider fixture. No endpoint, credentials or real network is used.
        prelude = '''
import http.client, json, os
assert "MALICIOUS_PROVIDER_TOKEN" not in os.environ
_rows = iter(%r)
class Response:
    def __init__(self, row): self.row, self.status = row, row["status"]
    def read(self, limit): return self.row["body"].encode()
    def getheader(self, key): return ""
class Connection:
    def __init__(self, host, timeout): assert host == "api.github.com"
    def request(self, method, path, headers):
        assert method == "GET" and path.startswith("/repos/owner/project/")
        assert headers["Authorization"] == "Bearer " + request["token"]
        assert headers["X-GitHub-Api-Version"] == "2026-03-10"
    def getresponse(self): return Response(next(_rows))
    def close(self): pass
http.client.HTTPSConnection = Connection
''' % raw
        from camol.preflight_process import bounded_preflight_run
        seen = []
        def run(argv, **kwargs):
            self.assertNotIn("fixture-token", repr(argv))
            self.assertNotIn("fixture-token", repr(kwargs["env"]))
            self.assertIn(b"fixture-token", kwargs["input"])
            seen.append(argv)
            return bounded_preflight_run(argv, **kwargs)
        with patch.dict(os.environ, MALICIOUS_PROVIDER_TOKEN=SECRET), \
                patch("camol.github_vcs.HTTP_PROGRAM", prelude + github.HTTP_PROGRAM), \
                patch("camol.github_vcs.bounded_preflight_run", side_effect=run):
            result = github.fetch(TARGET, REVISION, token="fixture-token", timeout=5)
        self.assertEqual(len(seen), 1)
        self.assertTrue(result["branch_readback"]["matches_integration"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_slow_flooding_and_cancelled_processes_are_reaped(self):
        native = subprocess.Popen
        processes = []
        def track(*args, **kwargs):
            process = native(*args, **kwargs)
            processes.append(process)
            return process
        for program in ("import time; time.sleep(10)", "import sys; sys.stdout.write('x' * 2000)"):
            with patch("camol.github_vcs.HTTP_PROGRAM", program), patch("camol.github_vcs.OUTPUT_LIMIT", 1024), \
                    patch("camol.preflight_process.subprocess.Popen", side_effect=track), self.assertRaises(VCSError):
                github.fetch(TARGET, REVISION, timeout=1)
        self.assertTrue(all(process.poll() is not None for process in processes))
        cancelled = threading.Event()
        cancelled.set()
        with patch("camol.preflight_process.subprocess.Popen", side_effect=AssertionError("cancel before process")), self.assertRaises(VCSError):
            github.fetch(TARGET, REVISION, cancel_event=cancelled)
