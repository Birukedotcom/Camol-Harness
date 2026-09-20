"""Pure launch review plus read-only provider evidence checks.

A manifest is human acknowledgement of frozen policy, not proof of readiness.
There is no provider call, environment discovery, clock, or write in its builder.
"""

from copy import deepcopy
from pathlib import Path

from .debug_execution import validate_source
from .providers import ModelProfile, ProviderError
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest, require_digest, require_identifier


class LaunchError(ValueError):
    pass


SUPPORTED_ADAPTERS = frozenset({"process", "claude_cli", "codex_cli", "codex_oss"})
HOSTED_ADAPTERS = frozenset({"claude_cli", "codex_cli"})


def profiles_for_runbook(runbook):
    """No file-backed policy may change between review and execution."""
    profiles, identities = {}, {}
    for agent in runbook["agents"]:
        adapter = agent["adapter"]
        if adapter["kind"] not in SUPPORTED_ADAPTERS:
            raise LaunchError("this terminal launch supports process, Claude CLI, Codex CLI and Codex OSS only")
        if adapter["kind"] == "process":
            continue
        if "profile_snapshot" not in adapter:
            raise LaunchError("interactive provider plans require every adapter's exact embedded profile_snapshot; no file-backed policy is silently frozen")
        profile = ModelProfile.from_dict(adapter["profile_snapshot"])
        if profile.adapter_kind != adapter["kind"]:
            raise LaunchError("embedded profile kind differs from its worker")
        digest = profile.digest()
        if profile.profile_id in identities and identities[profile.profile_id] != digest:
            raise LaunchError("the same profile_id cannot name different profile digests in one launch")
        identities[profile.profile_id] = digest
        profiles[digest] = profile
    hosted = {profile.max_run_usd_cents for profile in profiles.values() if profile.adapter_kind in HOSTED_ADAPTERS}
    if len(hosted) > 1:
        raise LaunchError("all hosted profiles must freeze one common max_run_usd_cents ceiling; per-worker ceilings are never summed")
    return profiles


def build_manifest(plan, product_digest, source, state_dir, target_id, *, preflight_cents=10):
    require_digest(product_digest, "product-plan digest")
    if canonical_digest(plan) != product_digest:
        raise LaunchError("launch review names a different product-plan digest")
    if type(preflight_cents) is not int or not 1 <= preflight_cents <= 100:
        raise LaunchError("--preflight-cents must be an integer between 1 and 100")
    source = deepcopy(validate_source(source))
    require_identifier(target_id, "launch target")
    path = Path(state_dir)
    if not path.is_absolute():
        raise LaunchError("launch state directory must be absolute")
    expected = plan.get("source")
    if expected is not None and any(source.get(key) != value for key, value in expected.items()):
        raise LaunchError("launch source differs from the approved product plan")
    runbook = validate_runbook(plan["runbook"])
    profiles = profiles_for_runbook(runbook)
    hosted = [profile for profile in profiles.values() if profile.adapter_kind in HOSTED_ADAPTERS]
    clauses = {
        "readiness": "unverified until exact fresh kernel admission; launch acknowledgement is not a receipt",
        "worker_budget": "one common hosted run accounting envelope, never summed across workers; provider requests can overrun requested caps",
        "preflight_budget": "separate requested reservation, not a combined account cap; no automatic renewal or retry of spent/uncertain operations",
        "process_trust": "developer_trusted process commands are not a security sandbox; exact worker trust tiers remain authoritative",
        "codex_policy": "requested-only model, unknown quota, unsupported hard USD/inner-turn/egress limits; unknown paid usage holds further paid launches",
        "local_policy": "catalog is not inference, weights identity, resource fit or airgap proof; never implicit download/load",
        "reconciliation": "unknown preflight usage requires explicit external owner review; this UI cannot clear a hold or mint replacement operation IDs",
    }
    acknowledgements = {}
    if plan.get("schema_version") == 4:
        scope = plan["origin"]["creation_envelope"]["scope_policy"]
        if scope["enforcement"] != "os_scoped":
            acknowledgements["draft_policy_digest"] = product_digest
    profile_rows = []
    preflights = []
    for digest, profile in sorted(profiles.items()):
        profile_rows.append({"profile_id": profile.profile_id, "profile_digest": digest,
            "profile": profile.to_dict(), "target_id": target_id,
            "agent_ids": sorted(agent["id"] for agent in runbook["agents"]
                                if agent["adapter"].get("profile_snapshot") == profile.to_dict())})
        if profile.adapter_kind == "claude_cli":
            preflights.append({"profile_id": profile.profile_id, "profile_digest": digest,
                "target_id": target_id, "max_requested_usd_cents": min(preflight_cents, profile.max_turn_usd_cents)})
    value = {
        "schema": "camol.interactive_launch", "schema_version": 1,
        "product_plan_digest": product_digest, "runbook_digest": runbook_digest(runbook),
        "run_id": runbook["run"]["id"], "state_dir": str(path), "source": source,
        "target": {"target_id": target_id, "kind": "local", "readiness": "unverified"},
        "run_policy": deepcopy(runbook["run"]), "workers": deepcopy(sorted(runbook["agents"], key=lambda item: item["id"])),
        "task_scope": [{"task_id": task["id"], "dependencies": task["depends_on"],
                        "capabilities": task["capabilities"], "resource_requirements": task.get("resource_requirements")}
                       for task in sorted(runbook["tasks"], key=lambda item: item["id"])],
        "profiles": profile_rows, "hosted_spend_ack_required": bool(hosted),
        "common_hosted_worker_ceiling_usd_cents": hosted[0].max_run_usd_cents if hosted else None,
        "preflight_review_input_usd_cents": preflight_cents,
        "max_requested_preflight_total_usd_cents": sum(row["max_requested_usd_cents"] for row in preflights),
        "preflights": preflights, "additional_acknowledgements": acknowledgements,
        "limitations": clauses,
    }
    scope_digest = canonical_digest(value)
    for row in value["preflights"]:
        row["operation_id"] = "launch-" + canonical_digest({"scope": scope_digest, "profile": row["profile_digest"], "target": target_id})[7:]
    return value


def assert_preflight_clear(state_dir):
    """A missing journal is empty; a present uncertain/unsafe one is a hold."""
    path = Path(state_dir) / "provider-preflights"
    if not path.exists() and not path.is_symlink():
        return
    try:
        from .preflight_journal import PreflightJournal
        with PreflightJournal(state_dir, read_only=True) as journal:
            rows = journal.inventory()
        if any(row["outcome"] is None or row["outcome"]["status"] == "unknown" for row in rows):
            raise LaunchError("unresolved preflight usage holds this launch; inspect camol preflight-status and reconcile with the owner, never delete/retry the operation")
    except (ImportError, OSError, ValueError, ProviderError) as error:
        if isinstance(error, LaunchError):
            raise
        raise LaunchError("preflight ledger is unavailable, unsafe or busy; no launch authorized") from error
