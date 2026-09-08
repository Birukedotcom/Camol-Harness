"""Stopped-owner revision review and crash-safe interactive session handoff.

This service never starts a supervisor, worker, model call, or capability probe.
Its public methods own the session lock; the private ``*_locked`` methods are for
InteractiveController, which already holds that same lock while dispatching.
"""

import json
import os
import stat
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .api import Harness
from .debug_execution import validate_source
from .json_contracts import load_contract
from .planning import reject_sensitive_text
from .revisions import RevisionError, _quiescent, _validate_proposal, collect_revision_lineage
from .runbook import load_runbook, runbook_digest, validate_runbook
from .sandbox import validate_process_invocation
from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier
from .session import MAX_SESSION_BYTES, validate_session
from .source_binding import assert_source
from .supervisor import SupervisorPaths


MAX_REVIEW_BYTES = 8 << 20
MAX_REVIEW_CHARS = 200_000
_PLAN_FIELDS = {"schema", "schema_version", "proposal", "run_id", "execution_status",
                "execution_limitation", "runbook", "source", "origin"}
_ORIGIN_FIELDS = {"kind", "parent_product_digest", "source_run_id", "source_plan_digest", "proposal_digest"}
_REVIEW_FIELDS = {"schema", "schema_version", "review_digest", "created_at", "owner", "workspace",
                  "state_dir", "session_id", "parent_product_plan", "parent_product_digest",
                  "proposal", "successor_product_plan", "successor_product_digest", "risk_delta"}


def validate_revision_envelope(value):
    if (not isinstance(value, dict) or set(value) != _PLAN_FIELDS
            or value["schema"] != "camol.product_plan" or type(value["schema_version"]) is not int
            or value["schema_version"] != 5 or value["proposal"] is not None):
        raise RevisionError("linked revision product plan has missing, unknown, or unsupported fields")
    source = validate_source(value["source"])
    runbook = validate_runbook(value["runbook"])
    if runbook["schema_version"] < 5 or runbook["run"]["id"] != value["run_id"]:
        raise RevisionError("linked revision needs its exact V5+ successor runbook")
    origin = value["origin"]
    if not isinstance(origin, dict) or set(origin) != _ORIGIN_FIELDS or origin["kind"] != "linked_plan_revision":
        raise RevisionError("linked revision origin is invalid")
    require_identifier(origin["source_run_id"], "source run")
    if origin["source_run_id"] == value["run_id"]:
        raise RevisionError("revision successor must have a distinct run ID")
    for key in ("parent_product_digest", "source_plan_digest", "proposal_digest"):
        require_digest(origin[key], key)
    if not isinstance(value["execution_status"], str) or value["execution_status"] not in {"ready", "preflight_required"} or not isinstance(value["execution_limitation"], str) or not value["execution_limitation"]:
        raise RevisionError("revision execution declaration is invalid")
    result = deepcopy(value)
    result.update(source=source, runbook=runbook)
    return result


def _parent_plan(value):
    if isinstance(value, dict) and value.get("schema_version") == 5:
        return validate_revision_envelope(value)
    from .app import validate_envelope
    result = validate_envelope(value)
    if result["schema_version"] not in {3, 4}:
        raise RevisionError("interactive revision requires an existing full source-bound parent; legacy unbound plans need an explicit baseline migration")
    return result


def _successor_plan(parent, proposal):
    runbook = proposal["new_runbook"]
    process_only = all(agent["adapter"]["kind"] == "process" for agent in runbook["agents"])
    return validate_revision_envelope(dict(schema="camol.product_plan", schema_version=5, proposal=None,
        run_id=proposal["destination_run_id"], runbook=runbook, source=parent["source"],
        execution_status="ready" if process_only else "preflight_required",
        execution_limitation="Linked human-approved revision; all tasks restart pending and require new readiness and evaluator gates. /run remains a separate explicit action with current provider-policy and spend acknowledgements.",
        origin=dict(kind="linked_plan_revision", parent_product_digest=canonical_digest(parent),
                    source_run_id=proposal["source_run_id"], source_plan_digest=proposal["source_plan_digest"],
                    proposal_digest=proposal["proposal_digest"])))


def _risk_delta(old, new):
    global_changes = {}
    for key in sorted((set(old) | set(new)) - {"tasks"}):
        before, after = old.get(key), new.get(key)
        if key == "run":
            before = {name: item for name, item in before.items() if name != "id"}
            after = {name: item for name, item in after.items() if name != "id"}
        if before != after:
            global_changes[key] = {"before": before, "after": after}
    prior = {item["id"]: item for item in old["tasks"]}
    following = {item["id"]: item for item in new["tasks"]}
    task_changes = {}
    for task_id in sorted(set(prior) | set(following)):
        before, after = prior.get(task_id), following.get(task_id)
        if before != after:
            task_changes[task_id] = {"before": before, "after": after}
    return dict(global_contract_changes=global_changes, task_contract_changes=task_changes,
                mode="reverify_all", inherited_checks_are_authority=False, automatic_execution=False)


def validate_review(value):
    if (not isinstance(value, dict) or set(value) != _REVIEW_FIELDS
            or value["schema"] != "camol.interactive_revision_review"
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise RevisionError("revision review has missing, unknown, or unsupported fields")
    for key in ("owner", "session_id"):
        require_identifier(value[key], key)
    for key in ("workspace", "state_dir"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise RevisionError("revision review paths must be absolute")
    parse_timestamp(value["created_at"], "revision review creation")
    parent = _parent_plan(value["parent_product_plan"])
    proposal = _validate_proposal(value["proposal"])
    successor = _successor_plan(parent, proposal)
    if (canonical_digest(parent) != value["parent_product_digest"]
            or parent["run_id"] != proposal["source_run_id"]
            or runbook_digest(parent["runbook"]) != proposal["source_plan_digest"]
            or parent["source"]["workspace"] != value["workspace"]
            or value["successor_product_plan"] != successor
            or value["successor_product_digest"] != canonical_digest(successor)
            or value["risk_delta"] != _risk_delta(parent["runbook"], proposal["new_runbook"])):
        raise RevisionError("revision review changed its exact plan/source/risk binding")
    if value["review_digest"] != canonical_digest({key: item for key, item in value.items() if key != "review_digest"}):
        raise RevisionError("revision review digest is invalid")
    reject_sensitive_text(json.dumps(value), "revision review")
    return deepcopy(value)


def render_review(value):
    review = validate_review(value)
    proposal = review["proposal"]
    sections = ["STOPPED-OWNER REVISION — review " + review["review_digest"],
        "Nothing runs during review or apply. Every successor task must reverify; no old green gate transfers.",
        "Human owner: " + review["owner"] + "\nReason: " + proposal["reason"],
        "Parent " + proposal["source_run_id"] + " -> successor " + proposal["destination_run_id"],
        "Reason: " + proposal["reason"],
        "Accepted integration baseline: " + str(proposal["integration_head"]),
        "Inherited charges/unknown usage: " + json.dumps(proposal["inherited_usage"], sort_keys=True),
        "Requested-only/local provider capabilities and developer-trusted isolation remain limited by their exact profiles. This review is not proof of no-egress, hostile-process containment or hard provider-side spending caps.",
        "Explicit prior-effect reuse: " + json.dumps(proposal["effect_reruns"], sort_keys=True),
        "Impact: " + json.dumps(proposal["impact"], indent=2, sort_keys=True),
        "Policy and task contract delta:\n" + json.dumps(review["risk_delta"], indent=2, sort_keys=True),
        "Exact successor runbook:\n" + json.dumps(proposal["new_runbook"], indent=2, sort_keys=True),
        "Apply only after review: /revise apply " + review["review_digest"],
        "Applying seals the parent and approves the linked successor, but does not start /run."]
    rendered = "\n\n".join(sections)
    if len(rendered) > MAX_REVIEW_CHARS:
        raise RevisionError("revision is too large for complete terminal review; narrow the change rather than truncate authority")
    return rendered


class RevisionUI:
    def __init__(self, session_store, *, harness_factory=Harness):
        self.sessions = session_store
        self.workspace = Path(session_store.workspace).resolve()
        self.root = session_store.project_dir / "revision-reviews"
        self.harness_factory = harness_factory

    def _current(self, expected):
        expected = validate_session(expected)
        current = self.sessions.load()
        if any(current[key] != expected[key] for key in ("session_id", "workspace", "state_dir", "run_id", "plan_digest", "approved_digest")):
            raise RevisionError("interactive session advanced; inspect its current plan before revising")
        if current["workspace"] != str(self.workspace) or current["approved_digest"] != current["plan_digest"] or current["plan"] is None:
            raise RevisionError("revision needs the exact approved current product plan")
        _parent_plan(current["plan"])
        return current

    def _directory(self, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RevisionError("revision review directory must be private and non-symlinked")

    def _path(self, digest):
        require_digest(digest, "review digest")
        return self.root / (digest[7:] + ".json")

    def _pending(self, session):
        identity = canonical_digest({key: session[key] for key in ("session_id", "state_dir", "run_id", "plan_digest")})
        return self.root / ("pending-" + identity[7:] + ".json")

    def _write(self, path, value, *, once=False):
        self._directory(create=True)
        raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(raw) > MAX_REVIEW_BYTES:
            raise RevisionError("revision review exceeds its durable byte ceiling")
        descriptor, temporary = tempfile.mkstemp(prefix=".review-", dir=str(self.root))
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if once:
                os.link(str(temporary), str(path), follow_symlinks=False)
            else:
                if path.is_symlink():
                    raise RevisionError("revision pointer cannot be a symlink")
                os.replace(str(temporary), str(path))
            directory = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, path):
        self._directory()
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RevisionError("revision review must be an owner-private regular file")
        return load_contract(path, max_bytes=MAX_REVIEW_BYTES)

    def _read_pending(self, session):
        pointer = self._read(self._pending(session))
        if not isinstance(pointer, dict) or set(pointer) != {"review_digest"}:
            raise RevisionError("revision review pointer is invalid")
        review = validate_review(self._read(self._path(pointer["review_digest"])))
        if any(review[key] != session[key] for key in ("session_id", "workspace", "state_dir")) or review["parent_product_digest"] != session["plan_digest"]:
            raise RevisionError("revision review belongs to a different interactive parent")
        return review

    def _existing_harness(self, session):
        root = Path(session["state_dir"])
        database = SupervisorPaths.under(root).database
        if root.is_symlink() or not root.is_dir() or database.is_symlink() or not database.is_file():
            raise RevisionError("revision requires the existing stopped owner's event store")
        return self.harness_factory(self.workspace, root)

    @staticmethod
    def _session_capacity(session, successor):
        # SessionStore may evict old displayed messages, never plan authority.
        # Prove that the irreducible handoff can fit before sealing the parent.
        candidate = dict(session, plan=successor, plan_digest=canonical_digest(successor),
            approved_digest=canonical_digest(successor), run_id=successor["run_id"],
            goal=successor["runbook"]["run"]["objective"], grill=None, messages=[],
            status="approved", selected_box=None, event_cursor=0)
        validate_session(candidate)
        if len((json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()) > MAX_SESSION_BYTES:
            raise RevisionError("successor exceeds durable interactive-session capacity; narrow it before approval")

    def _source(self, harness, session, owner, *, quiescent=True):
        state = harness.orchestrator.state(session["run_id"])
        parent = _parent_plan(session["plan"])
        if (state["run_id"] != session["run_id"] or state["plan_digest"] != runbook_digest(parent["runbook"])
                or state["approved_by"] != owner or owner in state["agents"]):
            raise RevisionError("revision source or exact human owner differs from the approved session")
        if (state.get("source_binding") or {}).get("source") != parent["source"]:
            raise RevisionError("revision parent lacks the exact approved source baseline")
        assert_source(parent["source"], self.workspace)
        if quiescent:
            _quiescent(state)
            packet_root = Path(session["state_dir"]) / "packets"
            if packet_root.is_symlink():
                raise RevisionError("invocation directory cannot be a symlink")
            for index, path in enumerate(packet_root.rglob("*.invocation.json")):
                if index >= 10_000:
                    raise RevisionError("invocation inspection exceeds its bound; reconcile before revision")
                invocation = validate_process_invocation(load_contract(path, max_bytes=65536))
                if invocation["state"] == "active":
                    raise RevisionError("reconcile active or orphan process invocation before revision; a stopped client is not proof of stopped work")
        # Harness defaults to latest_run_id; this UI selects only its explicitly
        # validated parent, never an unrelated latest row from the same database.
        harness.run_id = session["run_id"]
        return state

    def propose(self, session, runbook, *, reason, owner, effect_reruns=None):
        with self.sessions.transaction():
            return self._propose_locked(session, runbook, reason=reason, owner=owner, effect_reruns=effect_reruns)

    def _planning_context_locked(self, session, document, *, reason, owner, effect_reruns):
        """Read-only pre-model check, not a revision or an execution grant."""
        from .revisions import execution_digest, _effect_policy
        session = self._current(session)
        document = validate_runbook(document)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1500:
            raise RevisionError("delegation reason must contain 1-1500 characters")
        reject_sensitive_text(reason, "revision reason")
        if not isinstance(effect_reruns, list):
            raise RevisionError("effect reuse policy must be an explicit array")
        with self._existing_harness(session) as harness:
            state = self._source(harness, session, owner)
            destination = document["run"]["id"]
            if destination == state["run_id"] or harness.store.has_run(destination):
                raise RevisionError("delegation requires a new, distinct successor run ID")
            if owner in {agent["id"] for agent in document["agents"]}:
                raise RevisionError("delegation owner cannot also be a successor worker")
            _effect_policy(state, document, effect_reruns)
            if len(state["tasks"]) > 64:
                raise RevisionError("delegation planning context exceeds the 64-task bound")
            return dict(source_run_id=state["run_id"], source_plan_digest=state["plan_digest"],
                source_state_digest=execution_digest(state), source_cursor=state["last_seq"],
                parent_product_digest=session["plan_digest"], destination_run_id=destination,
                integration_head=state.get("integration_head"), run_status=state["status"],
                tasks=[dict(task_id=identity, status=task["status"], assigned_box=task.get("agent_id"),
                    depends_on=task["depends_on"], attempts=task["attempts"])
                    for identity, task in sorted(state["tasks"].items())],
                evidence_basis="recorded_metadata_not_current_readiness", automatic_execution=False)

    def _propose_locked(self, session, runbook, *, reason, owner, effect_reruns=None, expected_context=None):
        session = self._current(session)
        if effect_reruns is None:
            effect_reruns = []
        elif not isinstance(effect_reruns, list):
            raise RevisionError("effect reuse policy must be an explicit array")
        document = load_runbook(runbook) if isinstance(runbook, (str, Path)) else validate_runbook(runbook)
        reject_sensitive_text(json.dumps(document), "revision runbook")
        reject_sensitive_text(reason, "revision reason")
        with self._existing_harness(session) as harness:
            state = self._source(harness, session, owner)
            if expected_context is not None:
                from .revisions import execution_digest
                if (expected_context["source_run_id"] != state["run_id"]
                        or expected_context["source_plan_digest"] != state["plan_digest"]
                        or expected_context["parent_product_digest"] != session["plan_digest"]
                        or expected_context["source_cursor"] != state["last_seq"]
                        or expected_context["source_state_digest"] != execution_digest(state)
                        or expected_context["destination_run_id"] != document["run"]["id"]):
                    raise RevisionError("delegation source advanced during planning; request a new reviewed proposal")
            proposal = harness.propose_revision(document, reason=reason, effect_reruns=effect_reruns)
            successor = _successor_plan(session["plan"], proposal)
            self._session_capacity(session, successor)
            review = dict(schema="camol.interactive_revision_review", schema_version=1,
                created_at=datetime.now(timezone.utc).isoformat(), owner=owner,
                workspace=str(self.workspace), state_dir=session["state_dir"], session_id=session["session_id"],
                parent_product_plan=session["plan"], parent_product_digest=session["plan_digest"],
                proposal=proposal, successor_product_plan=successor, successor_product_digest=canonical_digest(successor),
                risk_delta=_risk_delta(session["plan"]["runbook"], document))
            review["review_digest"] = canonical_digest(review)
            review = validate_review(review)
            render_review(review)
            self._write(self._path(review["review_digest"]), review, once=True)
            self._write(self._pending(session), {"review_digest": review["review_digest"]})
            return review

    def inspect(self, session):
        with self.sessions.transaction():
            return self._inspect_locked(session)

    def _inspect_locked(self, session):
        return self._read_pending(self._current(session))

    def _handoff(self, session, review, harness, successor):
        proposal = review["proposal"]
        source = harness.orchestrator.state(proposal["source_run_id"])
        if (source["status"] != "superseded" or source.get("successor", {}).get("proposal_digest") != proposal["proposal_digest"]
                or successor["run_id"] != proposal["destination_run_id"]
                or successor["plan_digest"] != proposal["destination_plan_digest"]
                or successor.get("revision", {}).get("proposal_digest") != proposal["proposal_digest"]
                or successor["approved_by"] != review["owner"]
                or (successor.get("source_binding") or {}).get("source") != review["successor_product_plan"]["source"]):
            raise RevisionError("committed successor does not match this exact reviewed handoff")
        collect_revision_lineage(harness.store, successor["run_id"])
        if successor["status"] == "superseded":
            raise RevisionError("successor advanced to another revision; recover its reviewed lineage explicitly")
        ui_status = ("approved" if successor["status"] == "ready" else
                     "terminal" if successor["status"] in {"completed", "blocked", "canceled", "failed"} else "running")
        return self.sessions.update(session, plan=review["successor_product_plan"],
            plan_digest=review["successor_product_digest"], approved_digest=review["successor_product_digest"],
            run_id=successor["run_id"], state_dir=review["state_dir"],
            goal=proposal["new_runbook"]["run"]["objective"], grill=None,
            status=ui_status, selected_box=None, event_cursor=0)

    def apply(self, session, review_digest, *, owner):
        with self.sessions.transaction():
            return self._apply_locked(session, review_digest, owner=owner)

    def _apply_locked(self, session, review_digest, *, owner):
        session = self._current(session)
        review = self._read_pending(session)
        if review["review_digest"] != review_digest or review["owner"] != owner:
            raise RevisionError("apply requires the exact current revision-review digest and human owner")
        self._session_capacity(session, review["successor_product_plan"])
        with self._existing_harness(session) as harness:
            source = self._source(harness, session, owner, quiescent=False)
            if source["status"] == "superseded":
                successor = harness.orchestrator.state(review["proposal"]["destination_run_id"])
                return self._handoff(session, review, harness, successor)
            self._source(harness, session, owner)
            stored = source.get("revision_proposals", {}).get(review["proposal"]["proposal_digest"])
            if stored != review["proposal"]:
                raise RevisionError("revision proposal changed or is absent from its source ledger")
            successor = harness.apply_revision(by=owner, proposal_digest=review["proposal"]["proposal_digest"])
            return self._handoff(session, review, harness, successor)

    def recover(self, session, *, owner):
        with self.sessions.transaction():
            return self._recover_locked(session, owner=owner)

    def _recover_locked(self, session, *, owner):
        session = self._current(session)
        review = self._read_pending(session)
        if review["owner"] != owner:
            raise RevisionError("revision recovery requires the exact human owner")
        with self._existing_harness(session) as harness:
            source = self._source(harness, session, owner, quiescent=False)
            if source["status"] != "superseded":
                raise RevisionError("revision has not committed; review and explicitly apply it first")
            successor = harness.orchestrator.state(review["proposal"]["destination_run_id"])
            return self._handoff(session, review, harness, successor)
