"""``camol doctor``: prove task-specific readiness without beginning any work (M1).

The doctor validates a runbook, runs every registered read-only probe once,
and then, for every task and every statically eligible worker, assembles the
exact records the lease predicate will need: a :class:`WorkspaceReceipt` for
the observed checkout, a :class:`BoxBinding`, an :class:`AuthorityPolicy`, a
:class:`ProbePolicy`, and a candidate :class:`ReadinessReceipt`. A candidate
is *coherent* when the receipt is green, which requires every required probe
to be green. The doctor also shows what the full ``READY_TO_LEASE`` predicate
would still demand (grant, reservation), so a green doctor is visibly not a
lease.

The doctor writes nothing: not the state directory, not the artifact sink, not
the repository, not a receipt file. Missing preparation is reported as a typed
red result with the exact action a human or an approved preparation step must
take.

Exit codes: ``0`` every task has at least one coherent candidate; ``2`` some
readiness requirement is red, unknown, stale, or needs preparation/approval;
``3`` a probe could not be performed or interpreted.
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO

from .probes import (
    EvaluatorBundleProbe,
    Probe,
    ProbeContext,
    ProbeExecutionError,
    ProbeOutcome,
    ProbeRegistry,
    Redactor,
    adapter_policy,
    default_registry,
    local_target_id,
    sanitize_identifier,
)
from .readiness import (
    AuthorityPolicy,
    BoxBinding,
    ProbePolicy,
    ProbeRequirement,
    ReadinessReceipt,
    WaitingReason,
    WorkspaceReceipt,
    assess_ready_to_lease,
)
from .runbook import load_runbook, runbook_digest
from .schema import SchemaError, canonical_digest, parse_timestamp
from .providers import ProviderError, model_profile_for_adapter
from .execution_placement import LocalExecutionPlacementProbe, required_placement

__all__ = ["DoctorOptions", "DoctorReport", "run_doctor", "EXIT_READY", "EXIT_NOT_READY", "EXIT_PROBE_FAILURE"]

EXIT_READY = 0
EXIT_NOT_READY = 2
EXIT_PROBE_FAILURE = 3

REPORT_SCHEMA = "camol.doctor_report"
REPORT_SCHEMA_VERSION = 1

# The current process adapter is an unsandboxed local process that reads,
# executes, and writes inside its box. This is the factual authority it
# exercises, labeled per the build plan; it is not a policy default.
PROCESS_ADAPTER_CAPABILITIES = ("execute", "read", "write")
PROCESS_ADAPTER_TRUST_TIER = "developer_trusted"


@dataclass(frozen=True)
class DoctorOptions:
    runbook: Path
    workspace: Path
    state_dir: Path
    json_output: bool = False
    now: Optional[str] = None
    receipt_ttl_seconds: Optional[int] = None
    min_free_bytes: int = 1 << 30
    services: Sequence[str] = ()
    target_id: Optional[str] = None


@dataclass
class DoctorReport:
    payload: Dict[str, Any]

    @property
    def exit_code(self) -> int:
        return self.payload["exit_code"]


def _now(option: Optional[str]) -> datetime:
    if option:
        return parse_timestamp(option, "--now")
    return datetime.now(timezone.utc)


DEFAULT_V1_RECEIPT_TTL_SECONDS = 300


def _ttl(runbook: Dict[str, Any], option: Optional[int]) -> int:
    """Receipt TTL: frozen in a v2 plan; for v1 plans a documented CLI value (default 300 s)."""
    policy = runbook["run"].get("readiness_policy")
    if policy is not None:
        if option is not None and option != policy["receipt_ttl_seconds"]:
            raise SchemaError("--receipt-ttl-seconds conflicts with the frozen run.readiness_policy")
        return policy["receipt_ttl_seconds"]
    ttl = DEFAULT_V1_RECEIPT_TTL_SECONDS if option is None else option
    if type(ttl) is not int or ttl <= 0:
        raise SchemaError("--receipt-ttl-seconds must be a positive integer")
    return ttl


def _eligible(task: Dict[str, Any], agent: Dict[str, Any]) -> bool:
    return set(task["capabilities"]).issubset(set(agent["capabilities"]))


def _probe_entry(outcome: ProbeOutcome, probe: Probe) -> Dict[str, Any]:
    reason = outcome.result.waiting_reason()
    return dict(
        outcome.result.to_dict(),
        required=probe.required,
        informational=not probe.required,
        reason=reason.to_dict() if reason else None,
        result_digest=outcome.result.digest(),
    )


def run_doctor(options: DoctorOptions, *, registry: Optional[ProbeRegistry] = None, env: Optional[Dict[str, str]] = None, stdout: Optional[TextIO] = None) -> DoctorReport:
    out = stdout or sys.stdout
    registry = registry or default_registry()
    redactor = Redactor(env)
    errors: List[str] = []

    runbook = load_runbook(options.runbook)
    plan_digest = runbook_digest(runbook)
    now = _now(options.now)
    clock = "synthetic" if options.now else "system"
    ttl = _ttl(runbook, options.receipt_ttl_seconds)
    target_id = options.target_id or local_target_id()
    context = ProbeContext.guarded(
        runbook=runbook,
        workspace=Path(options.workspace),
        state_dir=Path(options.state_dir),
        now=now,
        ttl_seconds=ttl,
        target_id=target_id,
        redactor=redactor,
        services=tuple(options.services),
        min_free_bytes=options.min_free_bytes,
    )

    shared: List[Dict[str, Any]] = []
    shared_outcomes: Dict[str, ProbeOutcome] = {}
    shared_probes = registry.shared_probes(context)
    for probe in shared_probes:
        try:
            outcome = probe.observe(context)
        except ProbeExecutionError as error:
            errors.append("{}: {}".format(probe.probe_id, redactor.text(str(error))))
            continue
        shared_outcomes[probe.probe_id] = outcome
        shared.append(_probe_entry(outcome, probe))

    repo_facts = shared_outcomes.get("source.repository")
    evaluator_facts = shared_outcomes.get("evaluator.bundle")
    evaluator_digest = (
        evaluator_facts.facts.get("evaluator_digest") if evaluator_facts else None
    ) or canonical_digest({"definition": EvaluatorBundleProbe.bundle(runbook), "assets_valid": False})

    tasks_report: List[Dict[str, Any]] = []
    all_ready = True
    for task in runbook["tasks"]:
        candidates: List[Dict[str, Any]] = []
        for agent in runbook["agents"]:
            if not _eligible(task, agent):
                continue
            agent_probes = list(registry.agent_probes(agent))
            placement = required_placement(task)
            if placement:
                # This read-only inspection does not prepare or verify the
                # execution sandbox; never promote its requested trust tier.
                agent_probes.append(LocalExecutionPlacementProbe(placement, None))
            agent_outcomes: List[ProbeOutcome] = []
            failed = False
            for probe in agent_probes:
                try:
                    agent_outcomes.append(probe.observe(context))
                except ProbeExecutionError as error:
                    errors.append("{}: {}".format(probe.probe_id, redactor.text(str(error))))
                    failed = True
            if failed:
                candidates.append({"agent_id": agent["id"], "status": "probe_failure"})
                continue
            try:
                candidate = _candidate(context, clock, runbook, plan_digest, evaluator_digest, task, agent, shared_probes, shared_outcomes, agent_probes, agent_outcomes, repo_facts)
            except SchemaError as error:
                errors.append("candidate {}/{}: could not assemble a coherent receipt: {}".format(task["id"], agent["id"], redactor.text(str(error))))
                candidates.append({"agent_id": agent["id"], "status": "probe_failure"})
                continue
            candidates.append(candidate)
        task_waiting: List[Dict[str, Any]] = []
        if not candidates:
            task_waiting.append(
                WaitingReason(
                    code="CAPACITY_EXHAUSTED",
                    detail="no registered worker covers task capabilities {}".format(", ".join(task["capabilities"])),
                    wake_condition="register a worker whose capabilities include the task's",
                    task_id=task["id"],
                ).to_dict()
            )
        ready = any(candidate.get("status") == "green" for candidate in candidates)
        all_ready = all_ready and ready
        tasks_report.append({"task_id": task["id"], "ready": ready, "waiting": task_waiting, "candidates": candidates})

    if errors:
        exit_code = EXIT_PROBE_FAILURE
        verdict = "probe_failure"
    elif all_ready and tasks_report:
        exit_code = EXIT_READY
        verdict = "ready"
    else:
        exit_code = EXIT_NOT_READY
        verdict = "not_ready"

    payload = {
        "schema": REPORT_SCHEMA,
        "schema_version": REPORT_SCHEMA_VERSION,
        "verdict": verdict,
        "exit_code": exit_code,
        "run_id": runbook["run"]["id"],
        "runbook_schema_version": runbook["schema_version"],
        "plan_digest": plan_digest,
        "evaluator_digest": evaluator_digest,
        "target_id": target_id,
        "observed_at": context.observed_at(),
        "expires_at": context.expires_at(),
        "receipt_ttl_seconds": ttl,
        "workspace": redactor.text(str(context.workspace)),
        "state_dir": redactor.text(str(context.state_dir)),
        "probes": shared,
        "tasks": tasks_report,
        "errors": errors,
        "read_only": True,
        "clock": clock,
        "synthetic_clock": clock == "synthetic",
        "usable_evidence": clock == "system",
        "note": "doctor proves readiness dimensions only; a green doctor is not a lease (no grant, no reservation, no fence). Provider-connection and network-policy probes are informational for a verified local interpreter and required for any other adapter; a green doctor for a verified local process adapter still proves nothing about hosted-model availability or network egress. Receipts produced with a synthetic clock (--now) are fixtures, never evidence, and READY_TO_LEASE rejects them.",
    }
    report = DoctorReport(payload=payload)
    if options.json_output:
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        out.write(render_text(payload))
    return report


def _candidate(
    context: ProbeContext,
    clock: str,
    runbook: Dict[str, Any],
    plan_digest: str,
    evaluator_digest: str,
    task: Dict[str, Any],
    agent: Dict[str, Any],
    shared_probes: Sequence[Probe],
    shared_outcomes: Dict[str, ProbeOutcome],
    agent_probes: Sequence[Probe],
    agent_outcomes: Sequence[ProbeOutcome],
    repo_facts: Optional[ProbeOutcome],
) -> Dict[str, Any]:
    run_id = runbook["run"]["id"]
    box_id = agent["id"]
    workspace_id = "ws-" + sanitize_identifier(agent["id"])
    facts = repo_facts.facts if repo_facts else {}
    workspace = WorkspaceReceipt(
        workspace_id=workspace_id,
        repository_id=facts.get("repository_id") or "unobserved",
        base_revision=facts.get("base_revision") or "unobserved",
        branch=facts.get("branch") or "unobserved",
        path=agent["box"],
        dirty_digest=facts.get("dirty_digest") or canonical_digest(["unobserved"]),
        # Honest posture of the current process adapter: the box directory lives
        # inside the shared checkout. M2 replaces this with isolated worktrees.
        filesystem_policy="shared_checkout_write",
        cleanup_owner="adopted",
        created_at=context.observed_at(),
    )
    binding = BoxBinding(
        run_id=run_id,
        task_id=task["id"],
        box_id=box_id,
        worker_id=agent["id"],
        target_id=context.target_id,
        plan_digest=plan_digest,
        workspace_id=workspace_id,
        workspace_digest=workspace.digest(),
        bound_at=context.observed_at(),
    )
    profile = None
    if agent["adapter"]["kind"] != "process":
        try:
            profile = model_profile_for_adapter(context.workspace, agent["adapter"])
        except ProviderError:
            profile = None
    capabilities = list(PROCESS_ADAPTER_CAPABILITIES)
    if profile is not None and profile.network_destinations:
        capabilities.append("network")
    if profile is not None and profile.credential_refs:
        capabilities.append("credential")
    authority = AuthorityPolicy(
        run_id=run_id,
        task_id=task["id"],
        required_capabilities=capabilities,
        filesystem_paths=[agent["box"]],
        network_destinations=profile.network_destinations if profile else (),
        credential_refs=profile.credential_refs if profile else (),
        trust_tier=agent.get("trust_tier", PROCESS_ADAPTER_TRUST_TIER),
    )
    policy = adapter_policy(agent, context)
    # Requiredness derives from the adapter: informational probes (provider
    # connection, network policy) become required for any adapter that is not a
    # verified local interpreter, so an unverified/hosted adapter cannot be green
    # while provider auth, model availability, or network policy is unknown.
    def is_required(probe: Probe) -> bool:
        if probe.required:
            return True
        if probe.kind == "provider":
            return policy["requires_provider_proof"]
        if probe.kind == "network":
            return policy["requires_network_policy"]
        return False

    required_probes = [probe for probe in list(shared_probes) + list(agent_probes) if is_required(probe)]
    probe_policy = ProbePolicy(
        run_id=run_id,
        task_id=task["id"],
        required_probes=[
            ProbeRequirement(probe_id=probe.probe_id, kind=probe.kind, target_bound=probe.target_bound, definition_digest=probe.definition_digest(context))
            for probe in required_probes
        ],
    )
    outcomes = {probe_id: outcome for probe_id, outcome in shared_outcomes.items()}
    for outcome in agent_outcomes:
        outcomes[outcome.result.probe_id] = outcome
    results = [outcomes[probe.probe_id].result for probe in required_probes if probe.probe_id in outcomes]
    status = "green" if results and len(results) == len(required_probes) and all(result.status == "green" for result in results) else "red"
    adapter_outcome = outcomes.get("adapter." + sanitize_identifier(agent["id"]))
    adapter_facts = adapter_outcome.facts if adapter_outcome else {}
    resolved_binary = adapter_facts.get("resolved_binary") or adapter_facts.get("binary_path") or (
        agent["adapter"]["argv"][0] if "argv" in agent["adapter"] else agent["adapter"]["kind"]
    )
    runtime_id = sanitize_identifier("{}@{}".format(Path(str(resolved_binary)).name, adapter_facts.get("version") or "unverified"), "runtime-unverified")
    receipt = ReadinessReceipt(
        receipt_id="doctor-{}-{}".format(sanitize_identifier(task["id"]), sanitize_identifier(agent["id"])),
        run_id=run_id,
        plan_digest=plan_digest,
        task_id=task["id"],
        box_id=box_id,
        worker_id=agent["id"],
        target_id=context.target_id,
        transport_id="local-process" if agent["adapter"]["kind"] == "process" else "local-provider-cli",
        runtime_id=runtime_id,
        box_binding_digest=binding.digest(),
        workspace_digest=workspace.digest(),
        evaluator_digest=evaluator_digest,
        authority_digest=authority.digest(),
        probe_policy_digest=probe_policy.digest(),
        adapter_kind=agent["adapter"]["kind"],
        requested_model=profile.requested_model if profile else None,
        credential_scopes=profile.credential_refs if profile else (),
        reservation_id=None,
        probes=results,
        status=status,
        observed_at=context.observed_at(),
        expires_at=min(
            [parse_timestamp(context.expires_at(), "doctor receipt expiry")]
            + [parse_timestamp(result.expires_at, "probe expiry") for result in results if result.expires_at is not None]
        ).isoformat(timespec="microseconds"),
        clock=clock,
    )
    coverage = probe_policy.coverage_errors(receipt)
    waiting: List[Dict[str, Any]] = []
    for result in results:
        reason = result.waiting_reason()
        if reason:
            waiting.append(WaitingReason(code=reason.code, detail=reason.detail, wake_condition=reason.wake_condition, task_id=task["id"], box_id=box_id).to_dict())
    for code, error in coverage:
        waiting.append(WaitingReason(code=code, detail=error, wake_condition="re-probe under the frozen probe policy", task_id=task["id"], box_id=box_id).to_dict())
    coherent = status == "green" and not coverage
    lease_preview = assess_ready_to_lease(
        now=context.observed_at(),
        binding=binding,
        plan_digest=plan_digest,
        plan_frozen=True,
        control_plane_ready=all(outcomes[probe.probe_id].result.status == "green" for probe in shared_probes if probe.required and probe.kind == "control_plane" and probe.probe_id in outcomes),
        dependencies_green=not task["depends_on"],
        workspace=workspace,
        receipt=receipt,
        evaluator_digest=evaluator_digest,
        evaluator_ready=outcomes["evaluator.bundle"].result.status == "green" if "evaluator.bundle" in outcomes else False,
        authority_policy=authority,
        grant=None,
        probe_policy=probe_policy,
        reservation=None,
    )
    return {
        "agent_id": agent["id"],
        "box_id": box_id,
        "worker_id": agent["id"],
        "target_id": context.target_id,
        "status": "green" if coherent else "red",
        "coherent": coherent,
        "workspace": workspace.to_dict(),
        "workspace_digest": workspace.digest(),
        "box_binding": binding.to_dict(),
        "box_binding_digest": binding.digest(),
        "authority_policy": authority.to_dict(),
        "authority_digest": authority.digest(),
        "probe_policy": probe_policy.to_dict(),
        "probe_policy_digest": probe_policy.digest(),
        "agent_probes": [_probe_entry(outcome, probe) for probe, outcome in zip(agent_probes, agent_outcomes)],
        "runtime_id": runtime_id,
        "adapter_policy": policy,
        "clock": clock,
        "evidence": coherent and receipt.is_evidence(),
        "receipt_digest": receipt.digest() if coherent else None,
        "receipt": receipt.to_dict(),
        "waiting": waiting,
        "lease_preview": lease_preview.to_dict(),
    }


def render_text(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("camol doctor  run={}  plan={}  target={}".format(payload["run_id"], payload["plan_digest"][:19], payload["target_id"]))
    lines.append("observed {}  valid until {}  clock={}  (read-only; nothing was prepared, launched, or spent)".format(payload["observed_at"], payload["expires_at"], payload["clock"]))
    if payload["synthetic_clock"]:
        lines.append("SYNTHETIC CLOCK: receipts below are fixtures, not evidence; READY_TO_LEASE rejects them")
    lines.append("")
    lines.append("SHARED PROBES")
    for probe in payload["probes"]:
        lines.append(_probe_line(probe))
    for task in payload["tasks"]:
        lines.append("")
        lines.append("TASK {}  {}".format(task["task_id"], "READY" if task["ready"] else "NOT READY"))
        for reason in task.get("waiting", []):
            lines.append("  wait {}: {} -> {}".format(reason["code"], reason["detail"], reason["wake_condition"]))
        for candidate in task["candidates"]:
            if candidate.get("status") == "probe_failure":
                lines.append("  box {}: PROBE FAILURE".format(candidate["agent_id"]))
                continue
            lines.append(
                "  box {}: {}  binding={}  probe-policy={}  authority={}  receipt={}".format(
                    candidate["agent_id"],
                    "GREEN" if candidate["coherent"] else "RED",
                    candidate["box_binding_digest"][:19],
                    candidate["probe_policy_digest"][:19],
                    candidate["authority_digest"][:19],
                    (candidate["receipt_digest"] or "none (incoherent)")[:19],
                )
            )
            for probe in candidate["agent_probes"]:
                lines.append("    " + _probe_line(probe))
            for reason in candidate["waiting"]:
                lines.append("    wait {}: {} -> {}".format(reason["code"], reason["detail"], reason["wake_condition"]))
            still = [reason["code"] for reason in candidate["lease_preview"]["reasons"]]
            lines.append("    lease predicate still requires: {}".format(", ".join(sorted(set(still))) or "nothing"))
    if payload["errors"]:
        lines.append("")
        lines.append("PROBE FAILURES")
        for error in payload["errors"]:
            lines.append("  " + error)
    lines.append("")
    lines.append("verdict: {} (exit {})".format(payload["verdict"], payload["exit_code"]))
    return "\n".join(lines) + "\n"


def _probe_line(probe: Dict[str, Any]) -> str:
    glyph = {"green": "■", "red": "□", "unknown": "?"}[probe["status"]]
    flag = "" if probe["required"] else " (informational; excluded from the probe policy)"
    method = "process: " + " ".join(probe["command"]) if probe["command"] else probe["method"]
    line = "{} {:<28} {}{}  [{}]".format(glyph, probe["probe_id"], probe["status"], flag, method)
    if probe.get("tool_version"):
        line += "  {}".format(probe["tool_version"])
    line += "\n      {}".format(probe["summary"])
    if probe["missing_requirements"]:
        line += "\n      missing: {}".format(", ".join(probe["missing_requirements"]))
    if probe["reason"]:
        line += "\n      {} -> {}".format(probe["reason"]["code"], probe["reason"]["wake_condition"])
    return line
