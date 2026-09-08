"""Ledger-backed candidate relationships and prospective verification impact.

Relationships are owner declarations, never Git mutations or gate approvals.
Immutable candidate evidence is historical truth, not transferable certification.
"""

from copy import deepcopy
from collections import deque

from .evaluation import CandidateRecord, IntegrationReceipt
from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier


EVENT = "VCS_RELATION_CHANGED"
RELATIONS = ("depends_on", "supersedes", "absorbs", "conflicts_with", "deploys", "backports", "abandons")
CONSUMES = frozenset({"depends_on", "absorbs", "deploys", "backports"})
CHANGES = ("merge", "rebase", "fold", "environment", "plan")
MAX_NODES, MAX_CHANGES = 10000, 20000


class VCSError(ValueError):
    pass


def _digest(value):
    return dict(value, digest=canonical_digest(value))


def _relation(source, target, relation):
    if relation not in RELATIONS:
        raise VCSError("unsupported VCS relationship")
    for value in (source, target):
        require_identifier(value, "exact candidate ID")
    if source == target:
        raise VCSError("a candidate cannot relate to itself")
    if relation == "conflicts_with":
        source, target = sorted((source, target))
    return dict(source=source, target=target, relation=relation)


def snapshot(state):
    """Pure bounded projection of an already replay-validated run."""
    if not state.get("run_id") or state.get("status") == "missing":
        raise VCSError("VCS inspection requires an existing exact run")
    if len(state.get("candidates", {})) > MAX_NODES or len(state.get("vcs_changes", {})) > MAX_CHANGES:
        raise VCSError("VCS snapshot exceeds its explicit node/change ceiling")
    integrations = {}
    for value in state.get("integrations", []):
        receipt = IntegrationReceipt.from_dict(value)
        integrations.setdefault(receipt.candidate_id, []).append(dict(
            integration_id=receipt.integration_id, revision=receipt.revision, branch=receipt.workspace.branch,
            repository_id=receipt.workspace.repository_id, checks_digest=receipt.checks_digest,
            evaluator_digest=receipt.evaluator_digest, receipt_digest=receipt.digest()))
    nodes = []
    for identity, value in sorted(state.get("candidates", {}).items()):
        candidate = CandidateRecord.from_dict(value)
        if candidate.candidate_id != identity or candidate.run_id != state["run_id"]:
            raise VCSError("candidate belongs to another identity or run")
        salvage = candidate.salvage
        # Salvage patch is base-to-workspace, not necessarily HEAD-to-workspace.
        content = dict(base_revision=salvage.base_revision, head_revision=salvage.head_revision,
                       base_to_workspace_patch_digest=salvage.patch_digest,
                       untracked=deepcopy(list(salvage.untracked)))
        accepted = integrations.get(identity, [])
        nodes.append(dict(candidate_id=identity, candidate_digest=candidate.digest(),
            task_id=candidate.task_id, box_id=candidate.agent_id, lease_id=candidate.lease_id,
            base_revision=salvage.base_revision, head_revision=salvage.head_revision,
            workspace_id=salvage.workspace_id, workspace_digest=salvage.workspace_digest,
            recorded_commit_ids=sorted({salvage.base_revision, salvage.head_revision} | {item["revision"] for item in accepted}),
            git_status_digest=None,
            captured_content_digest=canonical_digest(content), salvage_digest=salvage.digest(),
            evaluator_digest=candidate.evaluator_digest,
            integrations=accepted,
            remote_push_receipt=None, pull_request=None, remote_review_state="not_observed"))
    result = dict(schema="camol.vcs_snapshot", schema_version=1, run_id=state["run_id"],
        plan_digest=state["plan_digest"], event_cursor=state["last_seq"], nodes=nodes,
        relations=[deepcopy(value) for _, value in sorted(state.get("vcs_relations", {}).items())],
        basis="recorded_candidates_and_owner_declared_relations_not_live_git",
        coverage=dict(cross_run_relations=False, remote_push_observed=False, pull_requests_observed=False),
        execution_authority=False)
    return _digest(result)


def _owner(state, actor):
    if (not state.get("approved_by") or actor != state["approved_by"] or actor in state["agents"]
            or state["status"] in {"missing", "draft", "superseded"}):
        raise VCSError("VCS changes require the exact approved human owner and an unsealed run")


def _settled(state):
    if any(task.get("lease_id") or task["status"] in {"leased", "running", "verifying"}
           for task in state["tasks"].values()):
        raise VCSError("drain and settle task leases before changing VCS relationships")


def _validate_edges(edges):
    # Only consumption relationships form a DAG. Conflicts and replacement
    # decisions are not execution dependencies and may coexist with them.
    adjacency, indegree = {}, {}
    for edge in edges:
        if edge["relation"] not in CONSUMES:
            continue
        source, target = edge["source"], edge["target"]
        adjacency.setdefault(target, []).append(source)
        indegree.setdefault(target, 0)
        indegree[source] = indegree.get(source, 0) + 1
    ready = deque(key for key, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        for dependent in adjacency.get(key, []):
            indegree[dependent] -= 1
            if not indegree[dependent]:
                ready.append(dependent)
    if visited != len(indegree):
        raise VCSError("candidate consumption relationships cannot contain a cycle")


def propose(state, *, source, target, relation, action="add", reason):
    _owner(state, state.get("approved_by"))
    _settled(state)
    edge = _relation(source, target, relation)
    if not isinstance(action, str) or action not in {"add", "remove"}:
        raise VCSError("VCS relation action must be add or remove")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000 or any(not c.isprintable() for c in reason):
        raise VCSError("VCS change reason must be 1..2000 printable characters")
    if any(key not in state["candidates"] for key in (edge["source"], edge["target"])):
        raise VCSError("VCS relationships require exact captured candidates from this run")
    relation_id = canonical_digest(edge)
    existing = state.get("vcs_relations", {})
    if (relation_id in existing) == (action == "add"):
        raise VCSError("relationship already exists" if action == "add" else "relationship is absent")
    future = {key: value for key, value in existing.items() if key != relation_id}
    if action == "add":
        future[relation_id] = edge
    _validate_edges(future.values())
    if len(state.get("vcs_changes", {})) >= MAX_CHANGES:
        raise VCSError("VCS change history reached its ceiling")
    return _digest(dict(schema="camol.vcs_change", schema_version=1,
        run_id=state["run_id"], plan_digest=state["plan_digest"],
        graph_digest=snapshot(state)["digest"], action=action, relation_id=relation_id,
        **edge, reason=Redactor().text(reason), approved_owner=state["approved_by"],
        authority="relationship_record_only"))


def _validate_proposal(state, value):
    if not isinstance(value, dict):
        raise VCSError("VCS proposal must be an object")
    expected = propose(state, source=value.get("source"), target=value.get("target"),
                       relation=value.get("relation"), action=value.get("action"), reason=value.get("reason"))
    if value != expected or type(value.get("schema_version")) is not int:
        raise VCSError("VCS proposal changed or its exact graph snapshot is stale")
    return expected


def apply(state, event):
    _owner(state, event["actor_id"])
    if event["run_id"] != state["run_id"]:
        raise VCSError("VCS event belongs to another run")
    proposal = _validate_proposal(state, event["payload"])
    identity = proposal["relation_id"]
    relations = state.setdefault("vcs_relations", {})
    if proposal["action"] == "add":
        relations[identity] = dict(_relation(proposal["source"], proposal["target"], proposal["relation"]),
                                   relation_id=identity, proposal_digest=proposal["digest"])
    else:
        del relations[identity]
    state.setdefault("vcs_changes", {})[proposal["digest"]] = deepcopy(proposal)


class VCSMixin:
    def vcs_snapshot(self, run_id):
        return snapshot(self.state(run_id))

    def propose_vcs_relation(self, run_id, **kwargs):
        return propose(self.state(run_id), **kwargs)

    def apply_vcs_relation(self, run_id, proposal, *, by, review_digest):
        state = self.state(run_id)
        _owner(state, by)
        require_digest(review_digest, "exact VCS review digest")
        if not isinstance(proposal, dict) or proposal.get("digest") != review_digest:
            raise VCSError("confirm the exact VCS proposal digest")
        previous = state.get("vcs_changes", {}).get(review_digest)
        if previous is not None:
            if proposal != previous:
                raise VCSError("VCS request digest is already bound to different content")
            return deepcopy(previous)  # Exact retry never re-adds a removed link.
        value = _validate_proposal(state, proposal)
        self._emit(run_id, EVENT, value, actor_id=by, expected_seq=state["last_seq"])
        return deepcopy(value)


def render_snapshot(graph, *, offset=0, limit=50):
    from .overview import _label
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise VCSError("VCS display offset must be nonnegative and limit 1..200")
    lines = ["CAMOL VCS | run={} | cursor={}".format(_label(graph["run_id"]), graph["event_cursor"]),
             "Recorded code and owner declarations; no live Git/PR observation or execution authority.",
             "", "CANDIDATE | TASK | HEAD | ACCEPTED INTEGRATIONS"]
    for node in graph["nodes"][offset:offset + limit]:
        lines.append("{} | {} | {} | {}".format(_label(node["candidate_id"], 128), _label(node["task_id"]),
                     _label(node["head_revision"], 64), len(node["integrations"])))
    lines.extend(("", "RELATIONSHIPS (source -> target)"))
    for edge in graph["relations"][offset:offset + limit]:
        lines.append("{} --{}--> {}".format(_label(edge["source"], 128), edge["relation"], _label(edge["target"], 128)))
    lines.append("{} candidates / {} relationships; graph {}".format(len(graph["nodes"]), len(graph["relations"]), graph["digest"]))
    if offset + limit < max(len(graph["nodes"]), len(graph["relations"])):
        lines.append("More: /vcs {} (offset applies to both lists)".format(offset + limit))
    lines.append("Use camol vcs inspect/impact for complete JSON; camol vcs propose/apply reviews relationship changes.")
    return "\n".join(lines)


def impact(state, *, candidates, change):
    """Prospective rerun set; never changes past gates or starts verification."""
    graph = snapshot(state)
    if change not in CHANGES or not isinstance(candidates, list) or not candidates:
        raise VCSError("impact requires a change kind and exact candidate IDs")
    if any(not isinstance(key, str) or key not in state["candidates"] for key in candidates) or len(set(candidates)) != len(candidates):
        raise VCSError("impact candidates must be unique captured identities from this run")
    adjacency = {}
    def edge(source, target):
        adjacency.setdefault(source, set()).add(target)
    for key, item in state["candidates"].items():
        edge(("candidate", key), ("task", item["task_id"]))
        edge(("task", item["task_id"]), ("candidate", key))
    for key, task in state["tasks"].items():
        for dependency in task["depends_on"]:
            edge(("task", dependency), ("task", key))
    for relation in graph["relations"]:
        if relation["relation"] in CONSUMES:
            edge(("candidate", relation["target"]), ("candidate", relation["source"]))
    reached = {("candidate", key) for key in candidates}
    if change in {"environment", "plan"}:
        reached.update(("task", key) for key in state["tasks"])
    pending = deque(reached)
    while pending:
        for dependent in adjacency.get(pending.popleft(), ()):
            if dependent not in reached:
                reached.add(dependent)
                pending.append(dependent)
    affected = {key for kind, key in reached if kind == "candidate"}
    affected_tasks = {key for kind, key in reached if kind == "task"}
    decisions = [edge["relation_id"] for edge in graph["relations"]
                 if edge["relation"] not in CONSUMES and {edge["source"], edge["target"]} & affected]
    obligations = []
    for key in sorted(affected_tasks):
        task = state["tasks"][key]
        obligations.append(dict(task_id=key, phases=["candidate", "integration"],
            verification_purposes=[Redactor().text(item["purpose"]) for item in task["verification"]],
            acceptance=Redactor().value(task["acceptance"]), required_evidence=Redactor().value(task["required_evidence"])))
    return _digest(dict(schema="camol.vcs_impact", schema_version=1, run_id=state["run_id"],
        plan_digest=state["plan_digest"], graph_digest=graph["digest"], change=change,
        requested_candidate_ids=sorted(candidates),
        candidate_ids=sorted(affected), tasks=obligations, review_relationships=sorted(decisions),
        basis="prospective_conservative_rerun_set_not_observed_git_change", execution_authority=False,
        historical_evidence_unchanged=True,
        application="use an approved successor plan; current revision policy reverifies all destination tasks"))
