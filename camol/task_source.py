"""Task-bound source selection; not readiness, capacity, or a launch grant."""

from copy import deepcopy
from pathlib import Path

from .debug_execution import source_identity, validate_source
from .evaluation import IntegrationReceipt
from .recovery import RecoveryError
from .schema import canonical_digest, require_digest, require_identifier


def _context(state, task_id):
    require_identifier(task_id, "source task")
    task = state.get("tasks", {}).get(task_id)
    contracts = [item for item in state["runbook"]["tasks"] if item["id"] == task_id]
    if (task is None or len(contracts) != 1 or task["status"] not in {"pending", "waiting"}
            or any(state["tasks"][item]["status"] != "succeeded" for item in task["depends_on"])):
        raise RecoveryError("task source requires pending work with succeeded dependencies")
    baseline = state.get("source_binding")
    if baseline is None:
        raise RecoveryError("task source requires an approved source baseline")
    if state.get("integration_head"):
        rows = [item for item in state.get("integrations", []) if item["revision"] == state["integration_head"]]
        revision = state.get("revision")
        if not rows and revision and revision["base_revision"] == state["integration_head"]:
            return contracts[0], "inherited_revision", canonical_digest(revision), None, state["integration_head"]
        if len(rows) != 1:
            raise RecoveryError("task source has no unique accepted integration head")
        receipt = IntegrationReceipt.from_dict(rows[0])
        if receipt.run_id != state["run_id"]:
            raise RecoveryError("task source integration belongs to another run")
        return contracts[0], "accepted_integration", receipt.digest(), receipt.workspace.path, receipt.revision
    source = baseline["source"]
    return contracts[0], "approved_baseline", canonical_digest(baseline), source["workspace"], source["revision"]


def validate_selection(value):
    fields = {"schema", "schema_version", "task_id", "task_digest", "base_kind", "origin_digest", "source", "digest"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.task_source"
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise RecoveryError("invalid task source selection schema")
    require_identifier(value["task_id"], "source task")
    if len(value["task_id"]) > 128:
        raise RecoveryError("source task identifier exceeds its bound")
    for field in ("task_digest", "origin_digest"):
        require_digest(value[field], field)
    if not isinstance(value["base_kind"], str) or value["base_kind"] not in {"approved_baseline", "accepted_integration", "inherited_revision"}:
        raise RecoveryError("unknown task source origin")
    validate_source(value["source"])
    if value["digest"] != canonical_digest({k: v for k, v in value.items() if k != "digest"}):
        raise RecoveryError("task source selection digest differs")
    return deepcopy(value)


def authorize_selection(state, value):
    value = validate_selection(value)
    contract, kind, origin, path, revision = _context(state, value["task_id"])
    if (value["task_digest"] != canonical_digest(contract) or value["base_kind"] != kind
            or value["origin_digest"] != origin or (path is not None and value["source"]["workspace"] != path)
            or value["source"]["revision"] != revision):
        raise RecoveryError("task contract or accepted source head changed; review a new handoff")
    if kind == "approved_baseline" and value["source"] != state["source_binding"]["source"]:
        raise RecoveryError("task source differs from the approved baseline")
    return value


def select(state, task_id, *, source_workspace=None):
    """Read exact clean integration bytes; never create a workspace or execute code."""
    if source_workspace is not None:
        if (not isinstance(source_workspace, (str, Path)) or not str(source_workspace).isprintable()
                or not 1 <= len(str(source_workspace)) <= 4096 or not Path(source_workspace).is_absolute()):
            raise RecoveryError("explicit task source must be a bounded absolute workspace path")
    contract, kind, origin, path, _ = _context(state, task_id)
    if source_workspace is not None and path is not None and str(source_workspace) != path:
        raise RecoveryError("source workspace differs from the recorded task source")
    path = path or source_workspace or state["source_binding"]["source"]["workspace"]
    source = (deepcopy(state["source_binding"]["source"]) if kind == "approved_baseline"
              else source_identity(path))
    value = dict(schema="camol.task_source", schema_version=1, task_id=task_id,
                 task_digest=canonical_digest(contract), base_kind=kind, origin_digest=origin, source=source)
    value["digest"] = canonical_digest(value)
    return authorize_selection(state, value)
