"""Bounded GitHub REST observations; no mutation, redirect, proxy or login."""

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from .json_contracts import decode_contract
from .preflight_process import bounded_preflight_run
from .probes import Redactor
from .schema import canonical_digest, require_timestamp
from .vcs import VCSError


API_VERSION = "2026-03-10"
BODY_LIMIT = 2 << 20
OUTPUT_LIMIT = 16 << 20

# Run only stdlib in the harness's own interpreter, with isolated mode and no
# site initialization. The existing process runner bounds DNS, headers, body,
# elapsed time, output and cancellation, including retained descendant pipes.
# Credentials travel on stdin, never command arguments, environment or disk.
HTTP_PROGRAM = r'''
import http.client, json, sys
try:
    request = json.loads(sys.stdin.buffer.read(4097))
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Camol-readonly-vcs",
               "X-GitHub-Api-Version": "2026-03-10", "Accept-Encoding": "identity"}
    if request["token"] is not None:
        headers["Authorization"] = "Bearer " + request["token"]
    results = []
    for item in request["requests"]:
        connection = http.client.HTTPSConnection("api.github.com", timeout=10)
        try:
            connection.request("GET", item["path"], headers=headers)
            response = connection.getresponse()
            if response.status == 200:
                body = response.read((2 << 20) + 1)
                if len(body) > 2 << 20:
                    raise ValueError("bound")
                body = body.decode("utf-8", "strict")
            else:
                body = None  # Never print a provider error body or follow a redirect.
            link = response.getheader("Link") or ""
            if len(link) > 16384:
                raise ValueError("bound")
            results.append({"key": item["key"], "status": response.status,
                            "more": 'rel="next"' in link, "body": body})
        finally:
            connection.close()
    print(json.dumps(results, separators=(",", ":")))
except Exception:
    sys.exit(2)  # No exception text, endpoint body or authentication value.
'''


def target(value):
    fields = {"schema", "schema_version", "repository", "branch", "pull_request"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.github_vcs_target"
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise VCSError("GitHub VCS target requires the exact versioned fields")
    repository(value["repository"])
    branch(value["branch"])
    number = value["pull_request"]
    if number is not None and (type(number) is not int or not 1 <= number <= 2**31 - 1):
        raise VCSError("pull_request must be an integer ID or null")
    return dict(value)


def repository(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value)
            or value.split("/")[1] in {".", ".."}):
        raise VCSError("GitHub repository must be an explicit OWNER/REPO, not a URL")
    return value


def branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)
            or ".." in value or "//" in value or value.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise VCSError("GitHub branch must be an explicit bounded branch name")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise VCSError("GitHub response requires an exact commit object ID")
    return value


def limits(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise VCSError("VCS observation timeout must be within 1..60 seconds")
    return dict(timeout_seconds=float(timeout), max_response_bytes=BODY_LIMIT,
                max_output_bytes=OUTPUT_LIMIT, max_items_per_collection=100, automatic_retries=0)


def requests(value, revision):
    value, revision = target(value), sha(revision)
    root = "/repos/" + value["repository"]
    ref = root + "/git/ref/heads/" + quote(value["branch"], safe="/")
    result = [dict(key="branch_before", path=ref)]
    pull = value["pull_request"]
    if pull is not None:
        pr = root + "/pulls/" + str(pull)
        result.append(dict(key="pull_before", path=pr))
    result.extend((dict(key="checks", path=root + "/commits/" + revision + "/check-runs?per_page=100"),
                   dict(key="statuses", path=root + "/commits/" + revision + "/status?per_page=100")))
    if pull is not None:
        result.extend((dict(key="reviews", path=pr + "/reviews?per_page=100"), dict(key="pull_after", path=pr)))
    result.append(dict(key="branch_after", path=ref))
    return result


def fetch(value, revision, *, token=None, timeout=30, cancel_event=None):
    plan = requests(value, revision)
    policy = limits(timeout)
    if token is not None and (not isinstance(token, str) or not 8 <= len(token) <= 2048 or not token.isascii()
                              or any(not char.isprintable() or char.isspace() for char in token)):
        raise VCSError("invalid explicitly supplied GitHub credential")
    body = json.dumps(dict(requests=plan, token=token), separators=(",", ":")).encode()
    if len(body) > 4096:
        raise VCSError("VCS request exceeds its fixed private input ceiling")
    try:
        result = bounded_preflight_run([str(Path(sys.executable).resolve()), "-I", "-S", "-c", HTTP_PROGRAM],
            cwd=os.path.dirname(os.__file__), env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            input=body, timeout=policy["timeout_seconds"], max_output_bytes=OUTPUT_LIMIT, cancel_event=cancel_event)
        if result.returncode:
            raise VCSError("GitHub observation transport did not return a complete bounded response")
        raw = decode_contract(result.stdout, max_bytes=OUTPUT_LIMIT)
        return normalize(raw, value, revision, redactor=_capture_redactor(token))
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise VCSError("GitHub observation unavailable, cancelled, malformed or beyond its bounds") from error


def _int(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise VCSError("invalid GitHub numeric identity/count")
    return value


def _text(value, maximum=512):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(not c.isprintable() for c in value):
        raise VCSError("invalid GitHub metadata text")
    return value


def _optional_sha(value):
    return None if value is None else sha(value)


def _pull(data, value):
    if (not isinstance(data, dict) or data.get("number") != value["pull_request"]
            or type(data.get("number")) is not int or data.get("base", {}).get("repo", {}).get("full_name", "").lower() != value["repository"].lower()):
        raise VCSError("pull request belongs to another repository or number")
    head, base = data["head"], data["base"]
    for field in ("draft", "merged"):
        if type(data.get(field)) is not bool:
            raise VCSError("pull request lacks a known draft/merge observation")
    head_repo = head.get("repo")
    return dict(id=_int(data["id"]), number=data["number"],
        base_repository=repository(base["repo"]["full_name"]), head_repository=repository(head_repo["full_name"]) if head_repo else None,
        head_ref=branch(head["ref"]), head_sha=sha(head["sha"]), base_sha=sha(base["sha"]),
        state=_text(data["state"], 64), draft=data["draft"], merged=data["merged"],
        merge_commit_sha=_optional_sha(data.get("merge_commit_sha")))


def _collection(items):
    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
        raise VCSError("GitHub collection exceeds its first-page item bound")
    ids = [_int(item["id"]) for item in items]
    if len(set(ids)) != len(ids):
        raise VCSError("GitHub collection contains duplicate identities")
    return items


class _Retained:
    def value(self, value):
        return value


def _capture_redactor(token=None):
    return Redactor(env={**os.environ, "CAMOL_EXPLICIT_GITHUB_TOKEN": token or ""})


def normalize(raw, value, revision, *, redactor=None):
    """Discard bodies/prose, tolerate additional provider fields, bind required fields."""
    plan = requests(value, revision)
    if not isinstance(raw, list) or len(raw) != len(plan):
        raise VCSError("GitHub observation response set is incomplete")
    normalized = []
    redactor = _capture_redactor() if redactor is None else redactor
    try:
        for item, expected in zip(raw, plan):
            if (not isinstance(item, dict) or set(item) != {"key", "status", "more", "body"}
                    or item["key"] != expected["key"] or type(item["status"]) is not int
                    or not 100 <= item["status"] <= 599 or type(item["more"]) is not bool):
                raise VCSError("invalid GitHub transport observation")
            key, code = item["key"], item["status"]
            data = None
            if code == 200:
                original = redactor.value(decode_contract(item["body"], max_bytes=BODY_LIMIT))
                if key.startswith("branch_"):
                    if original["ref"] != "refs/heads/" + value["branch"] or original["object"]["type"] != "commit":
                        raise VCSError("GitHub ref response belongs to another ref/type")
                    data = dict(ref=original["ref"], sha=sha(original["object"]["sha"]))
                elif key.startswith("pull_"):
                    data = _pull(original, value)
                elif key == "checks":
                    items = _collection(original["check_runs"])
                    total = _int(original["total_count"])
                    if total < len(items) or any(sha(row["head_sha"]) != revision for row in items):
                        raise VCSError("check runs do not bind the requested commit/count")
                    data = dict(total_count=total, complete=not item["more"] and total == len(items),
                        items=[dict(id=row["id"], head_sha=row["head_sha"], name=_text(row["name"]),
                                    status=_text(row["status"], 64), conclusion=None if row.get("conclusion") is None else _text(row["conclusion"], 64)) for row in items])
                elif key == "statuses":
                    items = _collection(original["statuses"])
                    total = _int(original["total_count"])
                    if sha(original["sha"]) != revision or total < len(items):
                        raise VCSError("status response does not bind the requested commit/count")
                    data = dict(sha=revision, total_count=total, state=_text(original["state"], 64),
                        complete=not item["more"] and total == len(items),
                        items=[dict(id=row["id"], context=_text(row["context"]), state=_text(row["state"], 64)) for row in items])
                else:
                    items = _collection(original)
                    rows = []
                    for row in items:
                        submitted = row.get("submitted_at")
                        if submitted is not None:
                            require_timestamp(submitted, "GitHub review timestamp")
                        rows.append(dict(id=row["id"], state=_text(row["state"], 64), commit_id=_optional_sha(row.get("commit_id")), submitted_at=submitted))
                    data = dict(complete=not item["more"], items=rows)
            elif item["body"] is not None:
                raise VCSError("non-success GitHub bodies must not be retained")
            normalized.append(dict(key=key, status=code, data=data))
    except (KeyError, TypeError, AttributeError) as error:
        raise VCSError("GitHub observation lacks required identity fields") from error
    return result(normalized, value, revision)


def result(observations, value, revision):
    """Derive only exact readback facts, never a review-policy/gate verdict."""
    by_key = {item["key"]: item["data"] for item in observations}
    before, after = by_key["branch_before"], by_key["branch_after"]
    same_ref = before is not None and before == after
    pr_before, pr_after = by_key.get("pull_before"), by_key.get("pull_after")
    same_pr = pr_before is not None and pr_before == pr_after
    pr_match = same_pr and pr_after["head_sha"] == revision and pr_after["head_repository"] is not None and pr_after["head_repository"].lower() == value["repository"].lower() and pr_after["head_ref"] == value["branch"]
    report = dict(schema="camol.github_vcs_result", schema_version=1, observations=observations,
        branch_readback=dict(endpoints_equal=same_ref, matches_integration=same_ref and after["sha"] == revision,
                             before_sha=before["sha"] if before else None, after_sha=after["sha"] if after else None),
        pull_request=dict(endpoints_equal=same_pr, matches_integration=bool(pr_match), latest=pr_after) if value["pull_request"] is not None else None,
        review_state="observed_reviews_not_approval_policy" if by_key.get("reviews") is not None else "not_observed",
        basis="successive_external_observations_not_atomic_or_gate_evidence", execution_authority=False)
    return dict(report, digest=canonical_digest(report))


def validate_result(report, value, revision):
    """Replay validation of the minimized retained provider fields and derivation."""
    try:
        if not isinstance(report, dict) or type(report.get("schema_version")) is not int:
            raise VCSError("invalid retained GitHub result")
        observations = report["observations"]
        if not isinstance(observations, list) or len(observations) != len(requests(value, revision)):
            raise VCSError("invalid retained observation set")
        raw = []
        for item in observations:
            if not isinstance(item, dict) or set(item) != {"key", "status", "data"}:
                raise VCSError("invalid retained observation fields")
            key, data = item["key"], item["data"]
            more, original = False, None
            if data is not None:
                if key.startswith("branch_"):
                    original = dict(ref=data["ref"], object=dict(type="commit", sha=data["sha"]))
                elif key.startswith("pull_"):
                    original = dict(id=data["id"], number=data["number"], state=data["state"], draft=data["draft"], merged=data["merged"], merge_commit_sha=data["merge_commit_sha"],
                        base=dict(sha=data["base_sha"], repo=dict(full_name=data["base_repository"])),
                        head=dict(sha=data["head_sha"], ref=data["head_ref"], repo=dict(full_name=data["head_repository"]) if data["head_repository"] else None))
                else:
                    if type(data["complete"]) is not bool:
                        raise VCSError("collection completeness must be boolean")
                    more = not data["complete"]
                    if key == "checks":
                        original = dict(total_count=data["total_count"], check_runs=data["items"])
                    elif key == "statuses":
                        original = dict(sha=data["sha"], total_count=data["total_count"], state=data["state"], statuses=data["items"])
                    elif key == "reviews":
                        original = data["items"]
                    else:
                        raise VCSError("unknown observation key")
            raw.append(dict(key=key, status=item["status"], more=more,
                            body=json.dumps(original) if original is not None else None))
        expected = normalize(raw, value, revision, redactor=_Retained())
        if canonical_digest(report) != canonical_digest(expected):
            raise VCSError("retained GitHub fields, digest or derived readback were changed")
        return expected
    except (KeyError, TypeError, AttributeError) as error:
        raise VCSError("invalid retained GitHub observation") from error


def redact_result(report, value, revision, *, token=None):
    retained = validate_result(report, value, revision)
    observations = _capture_redactor(token).value(retained["observations"])
    return validate_result(result(observations, value, revision), value, revision)
