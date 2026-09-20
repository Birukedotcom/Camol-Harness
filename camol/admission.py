"""Runtime admission: assemble and validate every input to ``READY_TO_LEASE``."""

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import uuid4

from .probes import (
    Probe,
    ProbeContext,
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
    CapacityReservation,
    CapabilityGrant,
    ProbePolicy,
    ProbeRequirement,
    ReadinessDecision,
    ReadinessReceipt,
    WorkspaceReceipt,
    assess_ready_to_lease,
)
from .sandbox import SandboxError, SandboxPolicy, select_backend, system_read_paths
from .providers import ModelProfile, model_profile_for_adapter
from .schema import canonical_digest, require_bool, require_digest, parse_timestamp
from .workspace import WorkspaceHandle, WorkspaceManager
from .git_view import ENVIRONMENT_NAMES, GitInspectionProbe, GitViewError, prepare_view, scratch_path
from .source_binding import SourceBaselineProbe
from .execution_placement import LocalExecutionPlacementProbe, required_placement


class AdmissionError(RuntimeError):
    """An admission record could not be assembled safely."""


def _timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise AdmissionError("admission clock must be timezone-aware")
    return value.astimezone(timezone.utc)


class SandboxBoundaryProbe(Probe):
    probe_id = "control-plane.sandbox"
    kind = "control_plane"
    VERSION = 1

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    def config(self, context):
        return {"sandbox_policy_digest": self.policy.digest(), "trust_tier": self.policy.trust_tier}

    def observe(self, context):
        try:
            backend = select_backend(self.policy)
        except SandboxError as error:
            return self.red(
                context,
                "no backend can enforce the frozen sandbox policy: {}".format(error),
                reason="POLICY_DENIED",
                wake="install or configure an enforcing sandbox backend, or explicitly approve developer_trusted execution",
                missing=["sandbox backend for {}".format(self.policy.trust_tier)],
                facts={"sandbox_policy_digest": self.policy.digest()},
            )
        summary = (
            "unsandboxed developer-trusted compatibility backend is explicitly frozen"
            if backend.name == "developer_trusted"
            else "sandbox backend {} can enforce the frozen policy".format(backend.name)
        )
        return self.green(
            context,
            summary,
            facts={"backend": backend.name, "sandbox_policy_digest": self.policy.digest()},
        )


@dataclass(frozen=True)
class AdmissionBundle:
    """The exact persisted inputs from which one lease decision is made."""

    SCHEMA = "camol.admission_bundle"
    SCHEMA_VERSION = 1

    binding: BoxBinding
    workspace: WorkspaceReceipt
    authority_policy: AuthorityPolicy
    probe_policy: ProbePolicy
    grant: CapabilityGrant
    reservation: CapacityReservation
    receipt: ReadinessReceipt
    sandbox_policy: SandboxPolicy
    evaluator_digest: str
    control_plane_ready: bool
    evaluator_ready: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "binding": self.binding.to_dict(),
            "workspace": self.workspace.to_dict(),
            "authority_policy": self.authority_policy.to_dict(),
            "probe_policy": self.probe_policy.to_dict(),
            "grant": self.grant.to_dict(),
            "reservation": self.reservation.to_dict(),
            "receipt": self.receipt.to_dict(),
            "sandbox_policy": self.sandbox_policy.to_dict(),
            "evaluator_digest": self.evaluator_digest,
            "control_plane_ready": self.control_plane_ready,
            "evaluator_ready": self.evaluator_ready,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AdmissionBundle":
        if not isinstance(payload, dict):
            raise AdmissionError("admission bundle must be an object")
        expected = {
            "schema", "schema_version", "binding", "workspace", "authority_policy",
            "probe_policy", "grant", "reservation", "receipt", "sandbox_policy",
            "evaluator_digest", "control_plane_ready", "evaluator_ready",
        }
        unknown = sorted(set(payload) - expected)
        missing = sorted(expected - set(payload))
        if unknown or missing or payload.get("schema") != cls.SCHEMA or payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise AdmissionError("invalid admission bundle fields (missing={}, unknown={})".format(missing, unknown))
        return cls(
            binding=BoxBinding.from_dict(payload["binding"]),
            workspace=WorkspaceReceipt.from_dict(payload["workspace"]),
            authority_policy=AuthorityPolicy.from_dict(payload["authority_policy"]),
            probe_policy=ProbePolicy.from_dict(payload["probe_policy"]),
            grant=CapabilityGrant.from_dict(payload["grant"]),
            reservation=CapacityReservation.from_dict(payload["reservation"]),
            receipt=ReadinessReceipt.from_dict(payload["receipt"]),
            sandbox_policy=SandboxPolicy.from_dict(payload["sandbox_policy"]),
            evaluator_digest=require_digest(payload["evaluator_digest"], "admission evaluator_digest"),
            control_plane_ready=require_bool(payload["control_plane_ready"], "admission control_plane_ready"),
            evaluator_ready=require_bool(payload["evaluator_ready"], "admission evaluator_ready"),
        )

    def decision(self, *, now: str, plan_digest: str, plan_frozen: bool, dependencies_green: bool) -> ReadinessDecision:
        return assess_ready_to_lease(
            now=now,
            binding=self.binding,
            plan_digest=plan_digest,
            plan_frozen=plan_frozen,
            control_plane_ready=self.control_plane_ready,
            dependencies_green=dependencies_green,
            workspace=self.workspace,
            receipt=self.receipt,
            evaluator_digest=self.evaluator_digest,
            evaluator_ready=self.evaluator_ready,
            authority_policy=self.authority_policy,
            grant=self.grant,
            probe_policy=self.probe_policy,
            reservation=self.reservation,
        )


class AdmissionController:
    """Prepares one isolated candidate and produces its complete admission bundle."""

    def __init__(
        self,
        runbook: Dict[str, Any],
        workspaces: WorkspaceManager,
        *,
        target_id: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        registry_factory: Callable[[], ProbeRegistry] = default_registry,
    ):
        self.runbook = runbook
        self.workspaces = workspaces
        self.state_dir = workspaces.state_dir
        self.target_id = target_id or local_target_id()
        self.clock = clock
        self.registry_factory = registry_factory

    def _ttl(self) -> int:
        return self.runbook["run"].get("readiness_policy", {}).get("receipt_ttl_seconds", 300)

    def _sandbox_policy(self, handle: WorkspaceHandle, task: Dict[str, Any], agent: Dict[str, Any], *, git_view=None) -> SandboxPolicy:
        packet_dir = self.state_dir / "packets" / _safe(self.runbook["run"]["id"]) / _safe(task["id"])
        packet_dir.mkdir(parents=True, exist_ok=True)
        worker_output = packet_dir / "worker-output"
        profile: Optional[ModelProfile] = None
        if agent["adapter"]["kind"] == "process":
            argv0 = agent["adapter"]["argv"][0]
            worker_output.mkdir(parents=True, exist_ok=True)
        else:
            profile = model_profile_for_adapter(handle.path, agent["adapter"])
            if profile.adapter_kind != agent["adapter"]["kind"]:
                raise AdmissionError("adapter kind does not match the frozen model profile")
            argv0 = profile.runtime_binary
        executable = shutil.which(argv0) if "/" not in argv0 else str(handle.path / argv0)
        trust_tier = agent.get("trust_tier", "developer_trusted")
        network: Tuple[str, ...] = profile.network_destinations if profile else ()
        credential_refs: Tuple[str, ...] = profile.credential_refs if profile else ()
        credential_reads = ()
        if profile:
            home = str(Path.home())
            credential_reads = tuple(
                item.replace("{home}", home) for item in profile.credential_read_paths
            )
        inspection_reads, scratch_writes, readonly = (), (), ()
        if git_view is not None:
            scratch = scratch_path(self.state_dir, handle.receipt.workspace_id)
            scratch.mkdir(parents=True, exist_ok=True)
            if scratch.resolve() != scratch:
                raise GitViewError("worker scratch must not be a symlink")
            inspection_reads = (str(git_view), str(scratch))
            scratch_writes = (str(scratch),)
            readonly = (str(git_view),) if trust_tier != "developer_trusted" else ()
        peer_reads, peer_environment = (), ()
        if profile is not None and profile.peer_policy is not None:
            from .native_peers import admission_paths, ENV_NAMES
            peer_reads = admission_paths(self.state_dir, self.runbook["run"]["id"], task["id"], agent["id"])
            peer_environment = ENV_NAMES
        return SandboxPolicy(
            policy_id="sandbox-{}-{}-{}".format(_safe(self.runbook["run"]["id"]), _safe(task["id"]), _safe(agent["id"])),
            workspace=str(handle.path),
            read_paths=(str(handle.path), str(packet_dir)) + system_read_paths(executable) + credential_reads + inspection_reads + peer_reads,
            write_paths=(str(handle.path),) + ((str(worker_output),) if profile is None else ()) + scratch_writes,
            environment_names=("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL") + (ENVIRONMENT_NAMES if git_view is not None else ()) + peer_environment,
            network_destinations=network,
            credential_refs=credential_refs,
            trust_tier=trust_tier,
            readonly_paths=readonly,
        )

    def prepare(
        self,
        *,
        plan_digest: str,
        task: Dict[str, Any],
        agent: Dict[str, Any],
        granted_by: str,
        base_revision: Optional[str] = None,
        expected_evaluator_digest: Optional[str] = None,
    ) -> Tuple[AdmissionBundle, WorkspaceHandle]:
        now = _parse_clock(self.clock)
        observed_at = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=self._ttl()))
        handle = self.workspaces.prepare_task(
            self.runbook["run"]["id"], task["id"], agent["id"], base_revision=base_revision
        )
        workspace = self.workspaces.refresh_receipt(handle)
        binding = BoxBinding(
            run_id=self.runbook["run"]["id"],
            task_id=task["id"],
            box_id=agent["id"],
            worker_id=agent["id"],
            target_id=self.target_id,
            plan_digest=plan_digest,
            workspace_id=workspace.workspace_id,
            workspace_digest=workspace.digest(),
            bound_at=observed_at,
        )
        git_root, git_manifest = prepare_view(self.workspaces.source, self.state_dir, handle)
        sandbox_policy = self._sandbox_policy(handle, task, agent, git_view=git_root)
        capabilities = ["execute", "read", "write"]
        if sandbox_policy.network_destinations:
            capabilities.append("network")
        if sandbox_policy.credential_refs:
            capabilities.append("credential")
        authority = AuthorityPolicy(
            run_id=binding.run_id,
            task_id=binding.task_id,
            required_capabilities=capabilities,
            filesystem_paths=sandbox_policy.write_paths,
            network_destinations=sandbox_policy.network_destinations,
            credential_refs=sandbox_policy.credential_refs,
            trust_tier=sandbox_policy.trust_tier,
        )
        grant = CapabilityGrant(
            grant_id="grant-" + uuid4().hex,
            **binding.subject(),
            authority_digest=authority.digest(),
            capabilities=authority.required_capabilities,
            filesystem_paths=authority.filesystem_paths,
            network_destinations=authority.network_destinations,
            credential_refs=authority.credential_refs,
            trust_tier=authority.trust_tier,
            granted_by=granted_by,
            granted_at=observed_at,
            expires_at=expires_at,
        )
        reservation = CapacityReservation(
            reservation_id="reservation-" + uuid4().hex,
            **binding.subject(),
            concurrency_slots=1,
            max_tokens=self.runbook["run"]["token_policy"]["max_tokens_per_turn"],
            max_usd_cents=(
                model_profile_for_adapter(handle.path, agent["adapter"]).max_turn_usd_cents
                if agent["adapter"]["kind"] != "process" else None
            ),
            status="reserved",
            reserved_at=observed_at,
            expires_at=expires_at,
        )

        registry = self.registry_factory()
        registry.register(SandboxBoundaryProbe(sandbox_policy))
        placement = required_placement(task)
        if placement:
            registry.register(LocalExecutionPlacementProbe(placement, sandbox_policy.trust_tier))
        registry.register(GitInspectionProbe(git_root, git_manifest))
        source_binding = self.workspaces._assert_bound_source(binding.run_id)
        if source_binding is not None:
            registry.register(SourceBaselineProbe(source_binding))
        context = ProbeContext.guarded(
            runbook=self.runbook,
            workspace=handle.path,
            source_workspace=self.workspaces.source,
            state_dir=self.state_dir,
            now=now,
            ttl_seconds=self._ttl(),
            target_id=self.target_id,
            redactor=Redactor(),
            services=(),
            expected_dirty_digest=workspace.dirty_digest,
            sandbox_policy=sandbox_policy.to_dict(),
            reservation_id=reservation.reservation_id,
        )
        shared_probes = registry.shared_probes(context)
        agent_probes = registry.agent_probes(agent)
        outcomes: Dict[str, ProbeOutcome] = {}
        for probe in list(shared_probes) + list(agent_probes):
            outcomes[probe.probe_id] = probe.observe(context)
        evaluator_probe = next(
            (probe for probe in shared_probes if probe.probe_id == "evaluator.bundle"), None
        )
        observed_evaluator = outcomes.get("evaluator.bundle")
        if expected_evaluator_digest is not None:
            expected_evaluator_digest = require_digest(
                expected_evaluator_digest, "expected evaluator digest"
            )
            if (
                evaluator_probe is None
                or observed_evaluator is None
                or observed_evaluator.facts.get("evaluator_digest") != expected_evaluator_digest
            ):
                if evaluator_probe is None:
                    raise AdmissionError("probe registry has no evaluator.bundle probe")
                outcomes["evaluator.bundle"] = evaluator_probe.red(
                    context,
                    "workspace evaluator assets differ from the run's frozen evaluator",
                    reason="EVALUATOR_NOT_READY",
                    wake="restore the frozen evaluator assets before requesting another lease",
                    missing=["workspace bytes matching the frozen evaluator digest"],
                    method="filesystem",
                    facts={
                        "evaluator_digest": (
                            observed_evaluator.facts.get("evaluator_digest")
                            if observed_evaluator is not None else canonical_digest({"missing": True})
                        ),
                        "expected_evaluator_digest": expected_evaluator_digest,
                    },
                )
        policy = adapter_policy(agent, context)

        def required(probe: Probe) -> bool:
            if probe.required:
                return True
            if probe.kind == "provider":
                return policy["requires_provider_proof"]
            if probe.kind == "network":
                return policy["requires_network_policy"]
            return False

        required_probes = [probe for probe in list(shared_probes) + list(agent_probes) if required(probe)]
        probe_policy = ProbePolicy(
            run_id=binding.run_id,
            task_id=binding.task_id,
            required_probes=tuple(
                ProbeRequirement(
                    probe_id=probe.probe_id,
                    kind=probe.kind,
                    target_bound=probe.target_bound,
                    definition_digest=probe.definition_digest(context),
                )
                for probe in required_probes
            ),
        )
        results = tuple(outcomes[probe.probe_id].result for probe in required_probes)
        status = "green" if results and all(result.status == "green" for result in results) else "red"
        adapter_outcome = outcomes.get("adapter." + sanitize_identifier(agent["id"]))
        facts = adapter_outcome.facts if adapter_outcome else {}
        runtime = facts.get("resolved_binary") or facts.get("binary_path") or (
            agent["adapter"]["argv"][0] if "argv" in agent["adapter"] else agent["adapter"]["kind"]
        )
        runtime_id = sanitize_identifier(
            "{}@{}".format(Path(str(runtime)).name, facts.get("version") or "unverified"), "runtime-unverified"
        )
        evaluator_digest = outcomes["evaluator.bundle"].facts["evaluator_digest"]
        receipt = ReadinessReceipt(
            receipt_id="receipt-" + uuid4().hex,
            run_id=binding.run_id,
            plan_digest=plan_digest,
            task_id=binding.task_id,
            box_id=binding.box_id,
            worker_id=binding.worker_id,
            target_id=binding.target_id,
            transport_id="local-process" if agent["adapter"]["kind"] == "process" else "local-provider-cli",
            runtime_id=runtime_id,
            box_binding_digest=binding.digest(),
            workspace_digest=workspace.digest(),
            evaluator_digest=evaluator_digest,
            authority_digest=authority.digest(),
            probe_policy_digest=probe_policy.digest(),
            adapter_kind=agent["adapter"]["kind"],
            requested_model=(
                model_profile_for_adapter(handle.path, agent["adapter"]).requested_model
                if agent["adapter"]["kind"] != "process" else None
            ),
            credential_scopes=sandbox_policy.credential_refs,
            reservation_id=reservation.reservation_id,
            probes=results,
            status=status,
            observed_at=observed_at,
            expires_at=min(
                [parse_timestamp(expires_at, "receipt expiry")]
                + [parse_timestamp(result.expires_at, "probe expiry") for result in results if result.expires_at is not None]
            ).isoformat(timespec="microseconds"),
            clock="system",
        )
        control_ready = all(
            outcomes[probe.probe_id].result.status == "green"
            for probe in shared_probes if required(probe) and probe.kind == "control_plane"
        )
        evaluator_ready = outcomes.get("evaluator.bundle") is not None and outcomes["evaluator.bundle"].result.status == "green"
        return AdmissionBundle(
            binding=binding,
            workspace=workspace,
            authority_policy=authority,
            probe_policy=probe_policy,
            grant=grant,
            reservation=reservation,
            receipt=receipt,
            sandbox_policy=sandbox_policy,
            evaluator_digest=evaluator_digest,
            control_plane_ready=control_ready,
            evaluator_ready=evaluator_ready,
        ), handle


def _safe(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)
