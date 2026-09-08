"""Approved source identity across client/daemon/runner handoffs."""

import hashlib
import json
import os
import stat
import tempfile
from copy import deepcopy
from pathlib import Path

from .json_contracts import decode_contract
from .probes import Probe
from .schema import canonical_digest, require_digest, require_identifier


class SourceBindingError(ValueError):
    pass


def make_binding(source, run_id, plan_digest):
    from .debug_execution import validate_source
    source = deepcopy(validate_source(source))
    return validate_binding(dict(schema="camol.source_baseline", schema_version=1,
        run_id=run_id, plan_digest=plan_digest, source=source, source_digest=canonical_digest(source)))


def validate_binding(value):
    from .debug_execution import validate_source
    if not isinstance(value, dict) or set(value) != {"schema", "schema_version", "run_id", "plan_digest", "source", "source_digest"}:
        raise SourceBindingError("source binding has missing or unknown fields")
    if value["schema"] != "camol.source_baseline" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SourceBindingError("unsupported source baseline binding")
    require_identifier(value["run_id"], "source binding run ID")
    require_digest(value["plan_digest"], "source binding plan digest")
    source = validate_source(value["source"])
    if value["source_digest"] != canonical_digest(source):
        raise SourceBindingError("source baseline identity digest is invalid")
    return deepcopy(value)


def apply_source_binding(state, event):
    binding = validate_binding(event["payload"])
    if (state["status"] != "draft" or state.get("source_binding") is not None
            or binding["run_id"] != state["run_id"] or binding["run_id"] != event["run_id"]
            or binding["plan_digest"] != state["plan_digest"] or event["actor_id"] in state["agents"]):
        raise SourceBindingError("source baseline must bind this draft exactly once before approval")
    state["source_binding"] = binding


def binding_path(state_dir, run_id):
    name = hashlib.sha256(run_id.encode()).hexdigest() + ".json"
    return Path(state_dir).resolve() / "control" / "source-bindings" / name


def load_binding(state_dir, run_id):
    path = binding_path(state_dir, run_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.parent.resolve() != path.parent:
            raise OSError("symlink parent")
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > 65536:
                raise OSError("unsafe source binding file")
            result = validate_binding(decode_contract(stream.read(65537), max_bytes=65536))
        if result["run_id"] != run_id:
            raise SourceBindingError("source binding belongs to another run")
        return result
    except OSError as error:
        raise SourceBindingError("persisted source binding is missing or unsafe") from error


def persist_binding(state_dir, binding):
    binding = validate_binding(binding)
    path = binding_path(state_dir, binding["run_id"])
    prior = load_binding(state_dir, binding["run_id"])
    if prior is not None:
        if prior != binding:
            raise SourceBindingError("the approved source binding cannot be replaced")
        return canonical_digest(binding)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent:
        raise SourceBindingError("source binding storage must not traverse symlinks")
    fd, temporary = tempfile.mkstemp(prefix=".binding-", dir=str(path.parent))
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(binding, sort_keys=True).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
        except FileExistsError:
            if load_binding(state_dir, binding["run_id"]) != binding:
                raise SourceBindingError("concurrent source binding differs from the approved identity")
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink()
    return canonical_digest(binding)


def assert_source(source, workspace):
    from .debug_execution import source_identity, DebugExecutionError
    if source["workspace"] != str(Path(workspace).resolve()):
        raise SourceBindingError("approved source belongs to another workspace")
    try:
        actual = source_identity(workspace)
    except (DebugExecutionError, OSError) as error:
        raise SourceBindingError("approved source is unavailable or no longer a clean committed checkout") from error
    if actual != source:
        raise SourceBindingError("source changed since human approval; review a new source-bound proposal")


class SourceBaselineProbe(Probe):
    probe_id = "control-plane.source-baseline"
    kind = "control_plane"
    VERSION = 1

    def __init__(self, binding):
        self.binding = validate_binding(binding)

    def config(self, context):
        return {"source_binding_digest": canonical_digest(self.binding)}

    def observe(self, context):
        try:
            assert_source(self.binding["source"], Path(self.binding["source"]["workspace"]))
        except SourceBindingError:
            return self.red(context, "approved source identity changed", reason="WORKSPACE_CONFLICT",
                wake="review and approve a new source-bound proposal", missing=["exact approved source identity"], facts=self.config(context))
        return self.green(context, "exact human-approved source identity is unchanged",
                          facts=self.config(context))


def require_source_admission(state, admission):
    binding = state.get("source_binding")
    if binding is None:
        return
    expected = SourceBaselineProbe(binding).definition_digest(None)
    if not any(probe.probe_id == SourceBaselineProbe.probe_id and probe.definition_digest == expected
               for probe in admission.probe_policy.required_probes):
        raise SourceBindingError("admission lacks the exact approved source baseline proof")
    approved_bases = {binding["source"]["revision"], state.get("integration_head"), (state.get("revision") or {}).get("base_revision")}
    approved_bases.update(item["revision"] for item in state.get("integrations", []))
    if admission.workspace.base_revision not in approved_bases:
        raise SourceBindingError("admission materialized an unapproved source revision")
