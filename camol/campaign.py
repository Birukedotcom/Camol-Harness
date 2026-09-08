"""Pinned, resumable benchmark campaigns over trusted suite-adapter plugins.

This runner owns selection, pairing, admission and durable result accounting.
Suite plugins own isolated materialization, gold/no-op grading and enforcement of
the declared per-trial budget. Plugins are trusted host code, never worker input.
An interrupted trial is not silently retried: its reservation remains unknown.
"""

import asyncio
import fcntl
import json
import os
import re
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .benchmark import ARMS, BenchmarkError, BenchmarkTrial, compare_trials
from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier, require_non_negative_int


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise BenchmarkError(name + " has missing or unknown fields")


def validate_campaign(value):
    fields = {"schema", "schema_version", "campaign_id", "suite", "suite_version", "dataset_digest",
              "selection", "tasks", "arms", "repetitions", "configuration", "budgets", "promotion"}
    _exact(value, fields, "campaign")
    if value["schema"] != "camol.benchmark_campaign" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BenchmarkError("unsupported benchmark campaign schema")
    for name in ("campaign_id", "suite", "suite_version"):
        require_identifier(value[name], name)
    require_digest(value["dataset_digest"], "dataset_digest")
    _exact(value["selection"], {"method", "seed", "cohort"}, "selection")
    if value["selection"]["method"] != "explicit_pinned":
        raise BenchmarkError("tasks must be selected explicitly before execution")
    require_non_negative_int(value["selection"]["seed"], "selection seed")
    if value["selection"]["cohort"] not in {"development", "canary", "release"}:
        raise BenchmarkError("invalid benchmark cohort")
    if not isinstance(value["arms"], list) or len(value["arms"]) != 3 or not all(isinstance(item, str) for item in value["arms"]) or set(value["arms"]) != ARMS:
        raise BenchmarkError("a campaign requires direct, one-box and adaptive arms exactly once")
    if type(value["repetitions"]) is not int or not 1 <= value["repetitions"] <= 100:
        raise BenchmarkError("repetitions must be 1..100")
    tasks = value["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > 10000:
        raise BenchmarkError("campaign tasks must be a bounded non-empty array")
    identities = []
    for task in tasks:
        _exact(task, {"task_id", "task_digest", "source_revision", "evaluator_digest", "environment_digest", "public_tests_digest", "family"}, "campaign task")
        identities.append(require_identifier(task["task_id"], "task_id"))
        for name in ("task_digest", "evaluator_digest", "environment_digest", "public_tests_digest"):
            require_digest(task[name], name)
        for name in ("source_revision", "family"):
            require_identifier(task[name], name)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", task["source_revision"]):
            raise BenchmarkError("source_revision must be an immutable full commit identity")
    if len(set(identities)) != len(identities):
        raise BenchmarkError("campaign task IDs must be unique")
    configuration = value["configuration"]
    _exact(configuration, {"model", "model_version", "effort", "sampling_digest", "context_digest", "tool_policy_digest",
                           "network_policy_digest", "secrets_policy_digest", "hardware_digest", "harness_revisions"}, "configuration")
    for name, content in configuration.items():
        if name.endswith("_digest"):
            require_digest(content, name)
        elif name != "harness_revisions":
            require_identifier(content, name)
    _exact(configuration["harness_revisions"], ARMS, "harness revisions")
    for revision in configuration["harness_revisions"].values():
        require_identifier(revision, "harness revision")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise BenchmarkError("harness revisions must be immutable full commit identities")
    _exact(value["budgets"], {"max_tokens", "max_cost_usd_micros", "max_elapsed_ms"}, "per-trial budgets")
    for name, amount in value["budgets"].items():
        require_non_negative_int(amount, name)
        if name != "max_cost_usd_micros" and amount == 0:
            raise BenchmarkError(name + " must be positive")
    _exact(value["promotion"], {"minimum_repetitions", "minimum_accepted_gain", "forbid_task_regressions"}, "promotion")
    for name in ("minimum_repetitions", "minimum_accepted_gain"):
        require_non_negative_int(value["promotion"][name], name)
    if value["promotion"]["minimum_repetitions"] < 1 or value["promotion"]["forbid_task_regressions"] is not True:
        raise BenchmarkError("promotion requires repetitions and forbids task-level correctness regressions")
    if Redactor().value(value) != value:
        raise BenchmarkError("campaign metadata must not contain protected values")
    return deepcopy(value)


class BenchmarkSuite(Protocol):
    async def validate_gold(self, task: dict, manifest: dict) -> dict:
        """Readiness: executed gold accepted and no-op rejected under exact pins."""
        ...

    async def run_trial(self, task: dict, arm: str, seed: int, manifest: dict) -> dict:
        """Materialize, allocate, execute, independently grade and retain evidence."""
        ...

    async def teardown(self, trial_id: str) -> None:
        """Clean only isolated resources owned by this specific trial."""
        ...


class CampaignStore:
    def __init__(self, path: Path, *, read_only=False):
        path = Path(path)
        if path.is_symlink():
            raise BenchmarkError("campaign database cannot be a symlink")
        self.read_only = read_only
        self.path = path.resolve()
        if read_only:
            self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            self.connection.execute("PRAGMA query_only=ON")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            self.connection = sqlite3.connect(str(path))
            self.connection.execute("CREATE TABLE IF NOT EXISTS campaign_events (seq INTEGER PRIMARY KEY, campaign TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def execution_owner(self, campaign_id):
        if self.read_only:
            raise BenchmarkError("campaign store is read-only")
        directory = self.path.parent / ".camol-campaign-locks"
        if directory.is_symlink():
            raise BenchmarkError("campaign ownership directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        identity = canonical_digest({"database": str(self.path), "campaign_id": campaign_id}).split(":")[1]
        descriptor = os.open(str(directory / (identity + ".lock")), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BenchmarkError("campaign has an active execution owner; stop or wait before mutating it") from error
            yield
        finally:
            os.close(descriptor)

    def events(self, campaign_id):
        return [dict(seq=row[0], kind=row[1], payload=json.loads(row[2])) for row in self.connection.execute(
            "SELECT seq, kind, payload FROM campaign_events WHERE campaign=? ORDER BY seq", (campaign_id,))]

    def append(self, campaign_id, kind, payload, *, expected_seq):
        if self.read_only:
            raise BenchmarkError("campaign store is read-only")
        if Redactor().value(payload) != payload:
            raise BenchmarkError("campaign evidence contains protected metadata")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            latest = self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM campaign_events WHERE campaign=?", (campaign_id,)).fetchone()[0]
            if latest != expected_seq:
                raise BenchmarkError("campaign changed; reload before appending")
            # Replay the proposed transition inside the same write transaction;
            # malformed/conflicting events must never poison the durable stream.
            proposed = self.events(campaign_id) + [dict(seq=latest + 1, kind=kind, payload=payload)]
            projected = BenchmarkCampaign._project(proposed)
            if projected["manifest"]["campaign_id"] != campaign_id:
                raise BenchmarkError("campaign event belongs to a different manifest")
            self.connection.execute("INSERT INTO campaign_events(campaign,kind,payload) VALUES (?,?,?)", (campaign_id, kind, json.dumps(payload, sort_keys=True, allow_nan=False)))


def _ready(receipt, task, manifest):
    _exact(receipt, {"task_id", "task_digest", "evaluator_digest", "environment_digest", "manifest_digest",
                     "gold_accepted", "noop_rejected", "gold_evidence_digest", "noop_evidence_digest", "budget_enforced"}, "suite readiness")
    for name in ("task_id", "task_digest", "evaluator_digest", "environment_digest"):
        if receipt[name] != task[name]:
            raise BenchmarkError("suite readiness changed " + name)
    if receipt["manifest_digest"] != canonical_digest(manifest):
        raise BenchmarkError("suite readiness is from another campaign")
    for name in ("gold_evidence_digest", "noop_evidence_digest"):
        require_digest(receipt[name], name)
    if not all(receipt[name] is True for name in ("gold_accepted", "noop_rejected", "budget_enforced")):
        raise BenchmarkError("suite gold/no-op validation or budget enforcement is not ready")


def _trial_identity(manifest, task, repetition, arm):
    return "trial-" + canonical_digest({"manifest": canonical_digest(manifest), "task": task["task_id"],
                                        "repetition": repetition, "arm": arm}).split(":")[1][:32]


def _result(result, task, manifest, repetition, arm, *, allow_overbudget=False):
    _exact(result, {"trial", "candidate_digest", "artifact_manifest_digest", "environment_digest", "manifest_digest", "seed", "usage_observed"}, "trial result")
    trial = BenchmarkTrial.from_dict(result["trial"])
    expected = {name: task[name] for name in ("task_id", "task_digest", "source_revision", "evaluator_digest")}
    expected.update({name: manifest["configuration"][name] for name in ("model", "model_version", "effort", "tool_policy_digest")})
    expected.update(arm=arm, trial_id=_trial_identity(manifest, task, repetition, arm), budget_digest=canonical_digest(manifest["budgets"]))
    for name, content in expected.items():
        if getattr(trial, name) != content:
            raise BenchmarkError("trial changed frozen " + name)
    for name in ("candidate_digest", "artifact_manifest_digest"):
        require_digest(result[name], name)
    if (result["environment_digest"] != task["environment_digest"] or result["manifest_digest"] != canonical_digest(manifest)
            or type(result["seed"]) is not int or result["seed"] != manifest["selection"]["seed"] + repetition):
        raise BenchmarkError("trial environment, campaign or paired seed changed")
    if result["usage_observed"] is not True:
        raise BenchmarkError("unknown usage requires reconciliation; do not publish a zero-cost trial")
    for metric, ceiling in (("tokens", "max_tokens"), ("cost_usd_micros", "max_cost_usd_micros"), ("elapsed_ms", "max_elapsed_ms")):
        if not allow_overbudget and getattr(trial, metric) > manifest["budgets"][ceiling]:
            raise BenchmarkError("trial exceeded " + ceiling)
    return deepcopy(result)


def _reported_usage(result):
    trial = result.get("trial") if isinstance(result, dict) else None
    trial = trial if isinstance(trial, dict) else {}
    return {name: trial[name] if type(trial.get(name)) is int and trial[name] >= 0 else None
            for name in ("tokens", "cost_usd_micros", "elapsed_ms")}


class BenchmarkCampaign:
    def __init__(self, store: CampaignStore, campaign_id: str):
        self.store, self.campaign_id = store, require_identifier(campaign_id, "campaign_id")

    @classmethod
    def create(cls, store, manifest, *, approved_by, manifest_digest):
        manifest = validate_campaign(manifest)
        require_identifier(approved_by, "approved_by")
        if canonical_digest(manifest) != manifest_digest:
            raise BenchmarkError("campaign approval has a stale manifest digest")
        campaign = cls(store, manifest["campaign_id"])
        store.append(campaign.campaign_id, "CREATED", dict(manifest=manifest, manifest_digest=manifest_digest,
                                                         approved_by=approved_by), expected_seq=0)
        return campaign

    def state(self):
        return self._project(self.store.events(self.campaign_id))

    @staticmethod
    def _project(events):
        if not events or events[0]["kind"] != "CREATED":
            raise BenchmarkError("unknown campaign")
        _exact(events[0]["payload"], {"manifest", "manifest_digest", "approved_by"}, "campaign creation")
        state = dict(events[0]["payload"], last_seq=events[0]["seq"], trials={}, readiness={}, quarantined={})
        require_identifier(state["approved_by"], "approved_by")
        manifest = validate_campaign(state["manifest"])
        if canonical_digest(manifest) != state["manifest_digest"]:
            raise BenchmarkError("campaign manifest digest mismatch")
        tasks = {task["task_id"]: task for task in manifest["tasks"]}
        for event in events[1:]:
            kind, payload = event["kind"], event["payload"]
            if kind == "READY":
                _ready(payload, tasks[payload["task_id"]], manifest)
                state["readiness"][payload["task_id"]] = payload
            elif kind == "STARTED":
                _exact(payload, {"trial_id", "task_id", "repetition", "arm"}, "trial start")
                task = tasks.get(payload["task_id"])
                if (task is None or task["task_id"] not in state["readiness"] or payload["arm"] not in manifest["arms"]
                        or type(payload["repetition"]) is not int or not 0 <= payload["repetition"] < manifest["repetitions"]
                        or payload["trial_id"] != _trial_identity(manifest, task, payload["repetition"], payload["arm"])
                        or payload["trial_id"] in state["trials"]):
                    raise BenchmarkError("trial start is unready, duplicate or outside the frozen cohort")
                state["trials"][payload["trial_id"]] = dict(payload, status="running", cleanup_complete=False)
            elif kind in {"FINISHED", "RECONCILED", "RECONCILED_FAILURE"}:
                _exact(payload, {"trial_id", "result"} if kind == "FINISHED" else {"trial_id", "result", "approved_by"}, "trial result event")
                trial = state["trials"].get(payload["trial_id"])
                expected_status = "running" if kind == "FINISHED" else "attention"
                if trial is None or trial["status"] != expected_status:
                    raise BenchmarkError("trial result is unstarted or already accepted")
                if kind != "FINISHED" and payload["approved_by"] != state["approved_by"]:
                    raise BenchmarkError("only the campaign owner can reconcile a trial")
                trial.update(status="finished", policy_violated=kind == "RECONCILED_FAILURE",
                             result=_result(payload["result"], tasks[trial["task_id"]], manifest, trial["repetition"], trial["arm"],
                                            allow_overbudget=kind == "RECONCILED_FAILURE"))
            elif kind == "RECOVERY_DECLARED":
                _exact(payload, {"trial_id", "approved_by"}, "interrupted trial recovery")
                trial = state["trials"].get(payload["trial_id"])
                if payload["approved_by"] != state["approved_by"] or trial is None or trial["status"] != "running":
                    raise BenchmarkError("interrupted recovery requires the owner and an unresolved running trial")
                trial.update(status="attention", reason="owner declared an interrupted execution; reconcile evidence before proceeding",
                             reported_usage={"tokens": None, "cost_usd_micros": None, "elapsed_ms": None}, returned_result_digest=None)
            elif kind == "ATTENTION":
                _exact(payload, {"trial_id", "reason", "reported_usage", "returned_result_digest"}, "trial attention")
                _exact(payload["reported_usage"], {"tokens", "cost_usd_micros", "elapsed_ms"}, "unverified usage")
                for amount in payload["reported_usage"].values():
                    if amount is not None:
                        require_non_negative_int(amount, "unverified usage")
                if payload["returned_result_digest"] is not None:
                    require_digest(payload["returned_result_digest"], "returned_result_digest")
                trial = state["trials"].get(payload["trial_id"])
                if trial is None or trial["status"] != "running":
                    raise BenchmarkError("attention must name a running trial")
                trial.update(status="attention", reason=payload["reason"], reported_usage=payload["reported_usage"],
                             returned_result_digest=payload["returned_result_digest"])
            elif kind in {"CLEANED", "CLEANUP_FAILED"}:
                _exact(payload, {"trial_id"} if kind == "CLEANED" else {"trial_id", "reason"}, "trial cleanup")
                trial = state["trials"].get(payload["trial_id"])
                if trial is None or trial["cleanup_complete"] or trial["status"] == "running":
                    raise BenchmarkError("cleanup names an unknown or already cleaned trial")
                trial.update(cleanup_complete=kind == "CLEANED", cleanup_error=payload.get("reason"))
            elif kind == "QUARANTINED":
                _exact(payload, {"task_id", "reason"}, "task quarantine")
                if payload["task_id"] not in tasks:
                    raise BenchmarkError("quarantine names an unknown task")
                state["quarantined"][payload["task_id"]] = payload["reason"]
            else:
                raise BenchmarkError("unknown campaign event")
            state["last_seq"] = event["seq"]
        expected = len(manifest["tasks"]) * manifest["repetitions"] * len(manifest["arms"])
        finished = sum(item["status"] == "finished" and item["cleanup_complete"] and item["task_id"] not in state["quarantined"] for item in state["trials"].values())
        quarantined_slots = len(state["quarantined"]) * manifest["repetitions"] * len(manifest["arms"])
        unresolved = any(item["status"] != "finished" or not item["cleanup_complete"] for item in state["trials"].values())
        failed = any(item.get("policy_violated") for item in state["trials"].values())
        completed_status = "completed_with_failures" if failed else ("completed_with_quarantine" if state["quarantined"] else "completed")
        state["status"] = ("attention" if unresolved else completed_status
                           if finished + quarantined_slots == expected else "ready")
        state["expected_trials"] = expected
        state["reserved_unknown"] = {name: amount * sum(item["status"] != "finished" for item in state["trials"].values()) for name, amount in manifest["budgets"].items()}
        state["conservative_unresolved_usage"] = {metric: sum(max(manifest["budgets"][bound], item.get("reported_usage", {}).get(metric) or 0)
                                               for item in state["trials"].values() if item["status"] != "finished")
                                                  for metric, bound in (("tokens", "max_tokens"), ("cost_usd_micros", "max_cost_usd_micros"), ("elapsed_ms", "max_elapsed_ms"))}
        return state

    def _append(self, kind, payload, state):
        self.store.append(self.campaign_id, kind, payload, expected_seq=state["last_seq"])

    def reconcile(self, trial_id, result, *, approved_by):
        with self.store.execution_owner(self.campaign_id):
            return self._reconcile(trial_id, result, approved_by=approved_by)

    def reconcile_failure(self, trial_id, result, *, approved_by):
        """Acknowledge known failed/overbudget usage without granting a passing score."""
        with self.store.execution_owner(self.campaign_id):
            return self._reconcile(trial_id, result, approved_by=approved_by, failed=True)

    def recover_interrupted(self, trial_id, *, approved_by):
        with self.store.execution_owner(self.campaign_id):
            state = self.state()
            self._append("RECOVERY_DECLARED", dict(trial_id=trial_id, approved_by=approved_by), state)
            return self.state()

    def _reconcile(self, trial_id, result, *, approved_by, failed=False):
        state = self.state()
        if approved_by != state["approved_by"]:
            raise BenchmarkError("only the campaign owner can reconcile a trial")
        trial = state["trials"].get(trial_id)
        if trial is None or trial["status"] != "attention":
            raise BenchmarkError("only inactive attention trials may be reconciled; declare crashed execution recovery first")
        task = next(item for item in state["manifest"]["tasks"] if item["task_id"] == trial["task_id"])
        _result(result, task, state["manifest"], trial["repetition"], trial["arm"], allow_overbudget=failed)
        self._append("RECONCILED_FAILURE" if failed else "RECONCILED", dict(trial_id=trial_id, result=result, approved_by=approved_by), state)
        return self.state()

    async def run(self, suite: BenchmarkSuite):
        with self.store.execution_owner(self.campaign_id):
            return await self._run_owned(suite)

    async def _run_owned(self, suite):
        state = self.state()
        if state["status"] in {"attention", "completed", "completed_with_quarantine", "completed_with_failures"}:
            return state
        manifest = state["manifest"]
        for task in manifest["tasks"]:
            if task["task_id"] in self.state()["quarantined"]:
                continue
            # Reprove gold/no-op environment after every host restart, even when
            # earlier trials exist. A stored receipt alone is not live readiness.
            try:
                readiness = await asyncio.wait_for(suite.validate_gold(deepcopy(task), deepcopy(manifest)),
                                                   manifest["budgets"]["max_elapsed_ms"] / 1000)
                _ready(readiness, task, manifest)
            except Exception:
                self._append("QUARANTINED", dict(task_id=task["task_id"], reason="gold/no-op/environment/budget readiness failed; not an agent score"), self.state())
                continue
            self._append("READY", readiness, self.state())
            for repetition in range(manifest["repetitions"]):
                arms = manifest["arms"]
                # Rotate paired arm order to avoid systematically favoring one
                # arm when human attention or provider latency drifts.
                order = arms[repetition % len(arms):] + arms[:repetition % len(arms)]
                for arm in order:
                    state = self.state()
                    identity = _trial_identity(manifest, task, repetition, arm)
                    if identity in state["trials"]:
                        if state["trials"][identity]["status"] == "finished":
                            continue
                        return state
                    self._append("STARTED", dict(trial_id=identity, task_id=task["task_id"], repetition=repetition, arm=arm), state)
                    failed = False
                    result = None
                    try:
                        result = await asyncio.wait_for(suite.run_trial(deepcopy(task), arm, manifest["selection"]["seed"] + repetition, deepcopy(manifest)),
                                                        manifest["budgets"]["max_elapsed_ms"] / 1000)
                        _result(result, task, manifest, repetition, arm)
                        self._append("FINISHED", dict(trial_id=identity, result=result), self.state())
                    except asyncio.CancelledError:
                        self._attention(identity, result, "interrupted; usage and effects require reconciliation")
                        raise
                    except Exception:
                        self._attention(identity, result, "trial failed validation or execution; retained reservation requires reconciliation")
                        failed = True
                    finally:
                        await self._cleanup_owned(suite, identity)
                    if failed or self.state()["status"] == "attention":
                        return self.state()
        return self.state()

    def _attention(self, identity, result, reason):
        try:
            digest = canonical_digest(result) if result is not None else None
        except ValueError:
            digest = None
        self._append("ATTENTION", dict(trial_id=identity, reason=reason, reported_usage=_reported_usage(result),
                                       returned_result_digest=digest), self.state())

    async def cleanup(self, suite, trial_id):
        with self.store.execution_owner(self.campaign_id):
            return await self._cleanup_owned(suite, trial_id)

    async def _cleanup_owned(self, suite, trial_id):
        state = self.state()
        trial = state["trials"].get(trial_id)
        if trial is None:
            raise BenchmarkError("unknown cleanup trial")
        if trial["status"] == "running":
            raise BenchmarkError("a running trial cannot be cleaned; declare interrupted recovery first")
        if trial["cleanup_complete"]:
            return state
        try:
            await asyncio.wait_for(suite.teardown(trial_id), state["manifest"]["budgets"]["max_elapsed_ms"] / 1000)
        except Exception:
            self._append("CLEANUP_FAILED", dict(trial_id=trial_id, reason="isolated trial cleanup requires attention"), self.state())
        else:
            self._append("CLEANED", dict(trial_id=trial_id), self.state())
        return self.state()

    def report(self):
        state = self.state()
        pairs = []
        for task in state["manifest"]["tasks"]:
            if task["task_id"] in state["quarantined"]:
                continue
            for repetition in range(state["manifest"]["repetitions"]):
                trials = {arm: state["trials"].get(_trial_identity(state["manifest"], task, repetition, arm)) for arm in ARMS}
                direct = trials["claude_direct"]
                if not direct or direct["status"] != "finished":
                    continue
                for arm in ("camol_one", "camol_adaptive"):
                    candidate = trials[arm]
                    if candidate and candidate["status"] == "finished":
                        comparison = compare_trials(BenchmarkTrial.from_dict(direct["result"]["trial"]), BenchmarkTrial.from_dict(candidate["result"]["trial"]))
                        pairs.append(dict(task_id=task["task_id"], family=task["family"], repetition=repetition, **comparison))
        review = {}
        policy = state["manifest"]["promotion"]
        for arm in ("camol_one", "camol_adaptive"):
            arm_pairs = [pair for pair in pairs if pair["camol_arm"] == arm]
            gain = sum(pair["delta_camol_minus_direct"]["accepted_behavior"] for pair in arm_pairs)
            conditions = dict(
                full_clean_campaign=state["status"] == "completed",
                minimum_repetitions=state["manifest"]["repetitions"] >= policy["minimum_repetitions"],
                accepted_gain=gain >= policy["minimum_accepted_gain"],
                no_task_regressions=all(pair["delta_camol_minus_direct"]["accepted_behavior"] >= 0
                                        and pair["guardrails"]["camol_invariant_violations"] == 0
                                        and pair["guardrails"]["camol_regressions"] == 0 for pair in arm_pairs),
                complete_evidence=bool(arm_pairs) and all(all(pair["evidence_complete"].values()) for pair in arm_pairs),
                recovered=bool(arm_pairs) and all(pair["recovery"]["camol"] for pair in arm_pairs),
            )
            review[arm] = dict(conditions=conditions, accepted_gain=gain, eligible_for_human_review=all(conditions.values()))
        return dict(schema="camol.campaign_report", schema_version=1, campaign_id=self.campaign_id,
                    manifest_digest=state["manifest_digest"], status=state["status"], expected_trials=state["expected_trials"],
                    reserved_unknown=state["reserved_unknown"], conservative_unresolved_usage=state["conservative_unresolved_usage"],
                    policy_violated_trials=[key for key, value in state["trials"].items() if value.get("policy_violated")],
                    quarantined_tasks=state["quarantined"], promotion_review=review, pairs=pairs, statistical_claim=False,
                    note="Descriptive matched results; no blended score or automatic promotion. Review per-task safety and evidence before accepting a harness change.")
