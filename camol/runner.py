"""Long-running execution-slot scheduler and verification loop."""

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import uuid4

from .admission import AdmissionBundle, AdmissionController, AdmissionError
from .adapter import AdapterError, create_agent_adapter
from .artifacts import ArtifactError, ArtifactRef, ArtifactStore
from .evaluation import (
    CandidateRecord,
    CounterexampleRecord,
    EvaluationError,
    EvaluatorBundle,
    EvaluatorStore,
    IntegrationReceipt,
)
from .orchestrator import Orchestrator, StateTransitionError
from .readiness import WaitingReason
from .sandbox import SandboxError, SandboxPolicy, select_backend, system_read_paths
from .workspace import WorkspaceError, WorkspaceHandle, WorkspaceManager
from .providers import ProviderError, model_profile_for_adapter
from .schema import canonical_digest
from .schema import parse_timestamp
from .leases import effective_expiry
from .usage import provider_cost_used
from .provider_budget import ProviderBudgetError, ProviderBudgetPending, budget_baseline
from .gates import GateError
from .probes import AdapterBinaryProbe, ProbeContext
from .capacity import CapacityError
from .capacity_runtime import CapacityCoordinator, enabled as capacity_enabled
from .git_view import execution_environment, GitViewError
from .source_binding import SourceBindingError, load_binding, require_source_admission


class HarnessRunner:
    def __init__(self, orchestrator: Orchestrator, workspace: Path, *, state_dir: Optional[Path] = None, capacity_broker=None):
        self.orchestrator = orchestrator
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else None
        self.workspaces = WorkspaceManager(self.workspace, self.state_dir) if self.state_dir else None
        self.artifacts = ArtifactStore(self.state_dir, redactor=self.orchestrator.redactor) if self.state_dir else None
        self.evaluators = EvaluatorStore(self.state_dir) if self.state_dir else None
        self._evaluator_bundle: Optional[EvaluatorBundle] = None
        self._verification_lock = None
        self._handles: Dict[Tuple[str, str], WorkspaceHandle] = {}
        self._paused_tasks = set()
        self._provider_turns = {}
        self.capacity = CapacityCoordinator(orchestrator, self.state_dir, broker=capacity_broker) if self.state_dir else None

    def close(self):
        if self.capacity is not None:
            self.capacity.close()

    def _ensure_source_binding(self, run_id):
        state = self.orchestrator.state(run_id)
        binding = state.get("source_binding")
        if self.workspaces is None:
            if binding is not None:
                raise WorkspaceError("source-bound runs require a persistent isolated workspace")
            return None
        try:
            persisted = load_binding(self.state_dir, run_id)
            if binding is None and persisted is not None:
                raise SourceBindingError("persisted source baseline is not bound in the approved event ledger")
            if binding is not None:
                if persisted is not None and persisted != binding:
                    raise SourceBindingError("persisted source baseline differs from the event ledger")
                self.workspaces.bind_source_contract(binding)
                self.workspaces.assert_source_ready(run_id)
            return binding
        except SourceBindingError as error:
            raise WorkspaceError(str(error)) from error

    async def _before_launch(self, run_id, assignment, turn_number):
        if self.capacity is None:
            return
        while True:
            decision = self.capacity.before_turn(run_id, assignment, turn_number)
            if decision["status"] == "granted":
                return
            if decision["status"] != "waiting":
                raise CapacityError("new invocation denied by shared capacity policy: {}".format(decision))
            # Rate waits preserve the same attempt and remain visible in the
            # ledger. The watchdog still renews the held slot and can cancel.
            wake = decision.get("wake_at")
            delay = max(0.05, min(15.0, (parse_timestamp(wake, "capacity wake") - self.orchestrator.clock()).total_seconds())) if wake else 1.0
            await asyncio.sleep(delay)

    def _agent_for(self, state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
        return state["agents"][agent_id]

    def _execution_agent(self, state, assignment, workspace):
        """Snapshot the exact admitted profile, never trust a worker's rewrite."""
        agent = state["agents"][assignment["agent_id"]]
        if agent["adapter"]["kind"] == "process":
            return agent
        profile = model_profile_for_adapter(workspace, agent["adapter"])
        bound_agent = dict(agent, adapter=dict(agent["adapter"], profile_snapshot=profile.to_dict()))
        admission = self._admission_for(state, assignment)
        context = ProbeContext.guarded(
            runbook=state["runbook"], workspace=workspace, state_dir=self.state_dir,
            now=self.orchestrator.clock(), ttl_seconds=300, target_id=admission.binding.target_id,
            redactor=self.orchestrator.redactor,
        )
        probe = AdapterBinaryProbe(bound_agent)
        requirement = next((item for item in admission.probe_policy.required_probes if item.probe_id == probe.probe_id), None)
        if requirement is None or probe.definition_digest(context) != requirement.definition_digest:
            raise ProviderError("provider profile changed from its admitted model/tool/budget policy; a human-approved amendment is required")
        return bound_agent

    @staticmethod
    def _provider_cost_remaining(state: Dict[str, Any], agent: Dict[str, Any], task_id: str, workspace: Path) -> Optional[int]:
        if agent["adapter"]["kind"] == "process":
            return None
        if (state.get("revision") or {}).get("inherited_usage", {}).get("unknown_usage"):
            return 0
        if any(item.get("kind") == "model_usage" and item.get("producer") == "adapter"
               and item.get("epistemic_status") == "OBSERVED"
               and item.get("data", {}).get("cost_usd_micros") is None
               and item.get("data", {}).get("reserved_cost_usd_micros", 0) > 0
               for item in state.get("evidence", {}).values()):
            return 0
        profile = model_profile_for_adapter(workspace, agent["adapter"])
        run_used_micros = provider_cost_used(state)
        task_used_micros = provider_cost_used(state, task_id=task_id)
        run_remaining = max(0, profile.max_run_usd_cents - (run_used_micros + 9_999) // 10_000)
        task_remaining = max(0, profile.max_task_usd_cents - (task_used_micros + 9_999) // 10_000)
        return min(profile.max_turn_usd_cents, task_remaining, run_remaining)

    async def _bounded_stream(self, stream: asyncio.StreamReader, limit: int = 1 << 20):
        retained = bytearray()
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if len(retained) < limit:
                retained.extend(chunk[: limit - len(retained)])
        return bytes(retained), "sha256:" + digest.hexdigest(), total, total > len(retained)

    async def _run_check(
        self,
        box: Path,
        command: Dict[str, Any],
        assignment: Dict[str, str],
        timeout_seconds: int = 900,
        *,
        workspace_root: Optional[Path] = None,
        phase: str = "candidate",
        trust_tier: str = "developer_trusted",
        evaluated_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        evaluated_task_id = evaluated_task_id or assignment["task_id"]
        identifier = uuid4().hex
        if command.get("cwd", "box") == "workspace_root":
            execution_cwd = Path(workspace_root or self.workspace).resolve()
        else:
            execution_cwd = Path(box).resolve()
        try:
            sandboxed = None
            if self.state_dir is not None and workspace_root is not None:
                packet_dir = self.state_dir / "packets" / assignment["fence"]["run_id"] / assignment["task_id"]
                packet_dir.mkdir(parents=True, exist_ok=True)
                executable = command["argv"][0]
                hardened = trust_tier != "developer_trusted"
                scratch = self.state_dir / "evaluator-scratch" / identifier
                if hardened:
                    scratch.mkdir(parents=True, exist_ok=False)
                policy = SandboxPolicy(
                    policy_id="evaluator-{}-{}".format(evaluated_task_id, phase),
                    workspace=str(workspace_root),
                    read_paths=(str(workspace_root), str(packet_dir)) + ((str(scratch),) if hardened else ()) + system_read_paths(executable),
                    write_paths=(str(scratch),) if hardened else (str(workspace_root),),
                    readonly_paths=(str(workspace_root),) if hardened else (),
                    environment_names=(
                        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE",
                    ),
                    network_destinations=(), credential_refs=(), trust_tier=trust_tier,
                )
                sandboxed = await select_backend(policy, invocation_root=packet_dir).run(
                    command["argv"], cwd=execution_cwd, policy=policy,
                    timeout_seconds=timeout_seconds,
                    environment=dict({"PYTHONDONTWRITEBYTECODE": "1"}, **({"TMPDIR": str(scratch)} if hardened else {})),
                    invocation_record=packet_dir / ("evaluator-" + identifier + ".invocation.json"),
                )
                stdout = (
                    sandboxed.stdout, sandboxed.stdout_sha256, sandboxed.stdout_bytes,
                    sandboxed.stdout_truncated,
                )
                stderr = (
                    sandboxed.stderr, sandboxed.stderr_sha256, sandboxed.stderr_bytes,
                    sandboxed.stderr_truncated,
                )
                exit_code = sandboxed.exit_code
            else:
                process = await asyncio.create_subprocess_exec(
                    *command["argv"], cwd=str(execution_cwd), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True,
                )
                stdout_task = asyncio.create_task(self._bounded_stream(process.stdout))
                stderr_task = asyncio.create_task(self._bounded_stream(process.stderr))
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                exit_code = process.returncode
            references = []
            if self.artifacts is not None:
                invocation_id = "verify-{}".format(identifier)
                producer = {
                    "run_id": assignment["fence"]["run_id"],
                    "task_id": assignment["task_id"],
                    "agent_id": assignment["agent_id"],
                    "lease_id": assignment["lease_id"],
                    "invocation_id": invocation_id,
                    "role": "verifier",
                }
                for channel, capture in (("stdout", stdout), ("stderr", stderr)):
                    reference = self.artifacts.put_bytes(
                        capture[0],
                        producer=dict(producer, channel=channel),
                        media_type="text/plain",
                        redact=True,
                        source_sha256=capture[1],
                        source_bytes=capture[2],
                        truncated=capture[3],
                    )
                    references.append(reference.to_dict())
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "cwd": command.get("cwd", "box"),
                "phase": phase,
                "exit_code": exit_code,
                "passed": exit_code == 0,
                "stdout_sha256": stdout[1],
                "stderr_sha256": stderr[1],
                "stdout_bytes": stdout[2],
                "stderr_bytes": stderr[2],
                "stdout_truncated": stdout[3],
                "stderr_truncated": stderr[3],
                "artifact_refs": references,
                "sandbox_backend": sandboxed.backend if sandboxed else "legacy-unsandboxed",
                "sandbox_policy_digest": sandboxed.policy_digest if sandboxed else None,
                "sandbox_policy": policy.to_dict() if sandboxed else None,
                "evaluated_task_id": evaluated_task_id,
                "source_write_policy": "os_readonly_with_scratch" if sandboxed and trust_tier != "developer_trusted" else "hash_checks_only_unenforced",
            }
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            if "stdout_task" in locals():
                await asyncio.gather(stdout_task, stderr_task)
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": None,
                "passed": False,
                "error": "verification_timeout",
                "phase": phase,
                "evaluated_task_id": evaluated_task_id,
            }
        except (OSError, SandboxError) as error:
            return {
                "purpose": command["purpose"],
                "argv": command["argv"],
                "exit_code": None,
                "passed": False,
                "error": "verification_launch_error: {}".format(error),
                "phase": phase,
                "evaluated_task_id": evaluated_task_id,
            }

    def _record_verifier_check(
        self, run_id: str, assignment: Dict[str, str], check: Dict[str, Any]
    ) -> None:
        self.orchestrator.record_observed_evidence(
            run_id, assignment, kind="command",
            data={key: value for key, value in check.items() if key != "artifact_refs"},
            epistemic_status="EXECUTED", producer="verifier",
            artifact_refs=tuple({
                item["digest"]: ArtifactRef.from_dict(item)
                for item in check.get("artifact_refs", [])
            }.values()),
        )

    def _record_evaluator_result(
        self, run_id: str, assignment: Dict[str, str], checks: Any
    ) -> None:
        references = {
            item["digest"]: ArtifactRef.from_dict(item)
            for check in checks for item in check.get("artifact_refs", [])
        }
        self.orchestrator.record_observed_evidence(
            run_id, assignment, kind="test_result",
            data={
                "passed": bool(checks) and all(check.get("passed") is True for check in checks),
                "checks": [
                    {key: value for key, value in check.items() if key != "artifact_refs"}
                    for check in checks
                ],
            },
            epistemic_status="EXECUTED", producer="verifier",
            artifact_refs=tuple(references.values()),
        )

    @staticmethod
    def _salvage_fingerprint(salvage: Any) -> str:
        return canonical_digest({
            "patch_digest": salvage.patch_digest,
            "patch_bytes": salvage.patch_bytes,
            "untracked": list(salvage.untracked),
        })

    def _check_data(self, check):
        """Digest exactly the normalized command evidence that replay consumes."""
        return self.orchestrator.redactor.value({
            key: value for key, value in check.items() if key != "artifact_refs"
        })

    def _counterexample(
        self, run_id: str, candidate: CandidateRecord, phase: str, checks: Any
    ) -> None:
        failed = next((check for check in checks if check.get("passed") is not True), {})
        summary = failed.get("error") or failed.get("purpose") or "evaluator rejected the candidate"
        self.orchestrator.record_counterexample(
            run_id,
            CounterexampleRecord(
                counterexample_id="counterexample-" + uuid4().hex,
                run_id=run_id, task_id=candidate.task_id, candidate_id=candidate.candidate_id,
                evaluator_digest=candidate.evaluator_digest, phase=phase,
                checks_digest=canonical_digest([
                    self._check_data(check)
                    for check in checks
                ]),
                summary=self.orchestrator.redactor.text(str(summary)),
                recorded_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ),
        )

    async def _verify(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> Optional[bool]:
        if self._verification_lock is None:
            self._verification_lock = asyncio.Lock()
        async with self._verification_lock:
            return await self._verify_serial(run_id, assignment)

    async def _verify_serial(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> Optional[bool]:
        state = self.orchestrator.state(run_id)
        task = state["tasks"][assignment["task_id"]]
        agent = state["agents"][assignment["agent_id"]]
        if self.workspaces is None or self.evaluators is None:
            checks = []
            box = (self.workspace / agent["box"]).resolve()
            for command in task["verification"]:
                check = await self._run_check(box, command, assignment)
                checks.append(check)
                self._record_verifier_check(run_id, assignment, check)
            self._record_evaluator_result(run_id, assignment, checks)
            return self.orchestrator.record_verification(run_id, assignment, checks)

        bundle_payload = state["admissions"]["{}:{}".format(task["id"], agent["id"])]
        admission = AdmissionBundle.from_dict(bundle_payload)
        bundle = self.evaluators.load(run_id, admission.evaluator_digest)
        if bundle.plan_digest != state["plan_digest"]:
            raise EvaluationError("frozen evaluator is bound to another plan")
        self._execution_workspace(run_id, assignment)
        source_handle = self._handles[(task["id"], agent["id"])]
        existing = next(
            (
                CandidateRecord.from_dict(item) for item in state["candidates"].values()
                if item["lease_id"] == assignment["lease_id"]
            ),
            None,
        )
        if existing is None:
            salvage = self.workspaces.salvage(source_handle)
            candidate = CandidateRecord(
                candidate_id="candidate-" + uuid4().hex,
                run_id=run_id, task_id=task["id"], agent_id=agent["id"],
                lease_id=assignment["lease_id"], fence_digest=assignment["fence_digest"],
                evaluator_digest=admission.evaluator_digest, salvage=salvage,
                captured_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            )
            self.orchestrator.record_candidate(run_id, assignment, candidate)
        else:
            candidate = existing

        verifier = self.workspaces.prepare_verifier(
            run_id, task["id"], "verify-" + uuid4().hex,
            base_revision=candidate.salvage.base_revision,
        )
        checks = []
        try:
            self.workspaces.materialize_candidate(candidate.salvage, verifier)
            self.evaluators.verify_assets(bundle, verifier.path)
            before = self._salvage_fingerprint(self.workspaces.salvage(verifier))
        except (WorkspaceError, EvaluationError) as error:
            checks.append({
                "purpose": "Materialize candidate in independent verifier",
                "phase": "candidate", "passed": False, "exit_code": None,
                "error": str(error), "artifact_refs": [],
                "evaluator_digest": bundle.evaluator_digest,
                "workspace_id": verifier.receipt.workspace_id,
            })
            before = None
        box = (verifier.path / agent["box"]).resolve()
        for command in task["verification"] if before is not None else []:
            check = await self._run_check(
                box, command, assignment, workspace_root=verifier.path,
                phase="candidate", trust_tier=agent.get("trust_tier", "developer_trusted"),
                evaluated_task_id=task["id"],
            )
            check.update(
                evaluator_digest=bundle.evaluator_digest, workspace_id=verifier.receipt.workspace_id,
            )
            checks.append(check)
        if before is not None:
            after = self._salvage_fingerprint(self.workspaces.salvage(verifier))
            if after != before:
                checks.append({
                    "purpose": "Evaluator must not mutate candidate context",
                    "phase": "candidate", "passed": False, "exit_code": None,
                    "error": "evaluator mutated the independent verifier workspace",
                    "artifact_refs": [], "evaluator_digest": bundle.evaluator_digest,
                    "workspace_id": verifier.receipt.workspace_id,
                })
        for check in checks:
            self._record_verifier_check(run_id, assignment, check)
        assessment = self.orchestrator.assess_task_gate(
            run_id, assignment, candidate.candidate_id, checks, phase="candidate"
        )
        if assessment is not None and assessment.status == "AWAITING_HUMAN":
            return None
        if assessment is not None and not assessment.passed and all(check.get("passed") is True for check in checks):
            checks.append({"purpose": "Candidate invariant gate", "phase": "candidate", "passed": False,
                           "exit_code": None, "error": "; ".join(assessment.reasons), "artifact_refs": []})
        if not checks or not all(check.get("passed") is True for check in checks):
            self._counterexample(run_id, candidate, "candidate", checks)
            self._record_evaluator_result(run_id, assignment, checks)
            return self.orchestrator.record_verification(run_id, assignment, checks)

        state = self.orchestrator.state(run_id)
        accepted = next(
            (item for item in state["integrations"] if item["candidate_id"] == candidate.candidate_id),
            None,
        )
        integration_checks = []
        pending_receipt = None
        if accepted is not None:
            integration_checks.append({
                "purpose": "Recover already accepted integration receipt",
                "phase": "integration", "passed": True, "exit_code": 0,
                "recovered": True, "receipt_digest": IntegrationReceipt.from_dict(accepted).digest(),
                "artifact_refs": [], "evaluator_digest": bundle.evaluator_digest,
                "workspace_id": accepted["workspace"]["workspace_id"],
            })
        else:
            base_revision = state.get("integration_head") or self.workspaces.head_revision()
            provisional = state.get("gate_assessments", {}).get(candidate.candidate_id + ":integration", {}).get("integration_receipt")
            provisional = IntegrationReceipt.from_dict(provisional) if provisional else None
            if provisional is not None and provisional.workspace.base_revision != base_revision:
                provisional = None
            integration = (
                self.workspaces.restore_receipt(provisional.workspace) if provisional else
                self.workspaces.prepare_integration_generation(
                    run_id, task["id"], "integrate-" + uuid4().hex, base_revision=base_revision,
                )
            )
            try:
                if provisional is None:
                    self.workspaces.materialize_candidate(candidate.salvage, integration)
                self.evaluators.verify_assets(bundle, integration.path)
                revision = provisional.revision if provisional else self.workspaces.commit_workspace(
                    integration, "Camol integration {} {}".format(task["id"], candidate.candidate_id)
                )
                if self.workspaces.head_revision(integration) != revision:
                    raise WorkspaceError("retained integration revision changed while awaiting approval")
                before_integration = self._salvage_fingerprint(self.workspaces.salvage(integration))
                accepted_task_ids = {item["task_id"] for item in state["integrations"]}
                evaluation_tasks = [
                    item for item in bundle.definition["tasks"]
                    if item["id"] == task["id"] or item["id"] in accepted_task_ids
                ]
                for evaluated in evaluation_tasks:
                    task_state = state["tasks"][evaluated["id"]]
                    evaluated_agent_id = agent["id"] if evaluated["id"] == task["id"] else task_state["agent_id"]
                    evaluated_agent = state["agents"][evaluated_agent_id]
                    evaluated_box = (integration.path / evaluated_agent["box"]).resolve()
                    for command in evaluated["verification"]:
                        check = await self._run_check(
                            evaluated_box, command, assignment, workspace_root=integration.path,
                            phase="integration",
                            trust_tier=evaluated_agent.get("trust_tier", "developer_trusted"),
                            evaluated_task_id=evaluated["id"],
                        )
                        check.update(
                            evaluator_digest=bundle.evaluator_digest,
                            workspace_id=integration.receipt.workspace_id,
                        )
                        integration_checks.append(check)
                after_integration = self._salvage_fingerprint(self.workspaces.salvage(integration))
                if after_integration != before_integration:
                    integration_checks.append({
                        "purpose": "Evaluator must not mutate integration context",
                        "phase": "integration", "passed": False, "exit_code": None,
                        "error": "evaluator mutated the integration workspace", "artifact_refs": [],
                        "evaluator_digest": bundle.evaluator_digest,
                        "workspace_id": integration.receipt.workspace_id,
                    })
                if integration_checks and all(check.get("passed") is True for check in integration_checks):
                    pending_receipt = IntegrationReceipt(
                        integration_id="integration-" + uuid4().hex,
                        run_id=run_id, task_id=task["id"], candidate_id=candidate.candidate_id,
                        evaluator_digest=bundle.evaluator_digest, revision=revision,
                        workspace=self.workspaces.refresh_receipt(integration),
                        checks_digest=canonical_digest([
                            self._check_data(check)
                            for check in integration_checks
                        ]),
                        accepted_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    )
            except (WorkspaceError, EvaluationError) as error:
                integration_checks.append({
                    "purpose": "Apply candidate to isolated integration generation",
                    "phase": "integration", "passed": False, "exit_code": None,
                    "error": str(error), "artifact_refs": [],
                    "evaluator_digest": bundle.evaluator_digest,
                })
        for check in integration_checks:
            self._record_verifier_check(run_id, assignment, check)
        if accepted is None and pending_receipt is not None:
            assessment = self.orchestrator.assess_task_gate(
                run_id, assignment, candidate.candidate_id, integration_checks, phase="integration",
                revision=pending_receipt.revision, integration_receipt=pending_receipt,
            )
            if assessment is not None and assessment.status == "AWAITING_HUMAN":
                return None
            if assessment is None or assessment.passed:
                self.orchestrator.accept_integration(run_id, assignment, pending_receipt)
            else:
                integration_checks.append({"purpose": "Integration invariant gate", "phase": "integration", "passed": False,
                                           "exit_code": None, "error": "; ".join(assessment.reasons), "artifact_refs": []})
        checks.extend(integration_checks)
        if not integration_checks or not all(check.get("passed") is True for check in integration_checks):
            self._counterexample(run_id, candidate, "integration", integration_checks)
        self._record_evaluator_result(run_id, assignment, checks)
        return self.orchestrator.record_verification(run_id, assignment, checks)

    def _record_observed(
        self, run_id: str, assignment: Dict[str, str], evidence: Dict[str, Any]
    ) -> None:
        expected = {"kind", "data", "epistemic_status", "producer", "artifact_refs"}
        if not isinstance(evidence, dict) or set(evidence) != expected:
            raise AdapterError("adapter evidence envelope is malformed")
        references = tuple(
            {
                item["digest"]: ArtifactRef.from_dict(item)
                for item in evidence["artifact_refs"]
            }.values()
        )
        if self.artifacts is not None:
            for reference in references:
                self.artifacts.verify(reference)
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind=evidence["kind"],
            data=evidence["data"],
            epistemic_status=evidence["epistemic_status"],
            producer=evidence["producer"],
            artifact_refs=references,
        )

    @staticmethod
    def _text_artifact(path: Path) -> bool:
        return path.suffix.lower() in {
            ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml",
            ".sh", ".zsh", ".bash", ".sql", ".csv", ".log",
        }

    def _capture_worker_artifact(
        self,
        run_id: str,
        assignment: Dict[str, str],
        execution_workspace: Path,
        evidence: Dict[str, Any],
        *, artifact_cwd: Optional[Path] = None,
    ) -> None:
        if self.artifacts is None:
            return
        data = evidence.get("data")
        path_value = data.get("path") if isinstance(data, dict) else None
        if not isinstance(path_value, str) or not path_value:
            raise ArtifactError("worker artifact evidence must name a path")
        raw = Path(path_value)
        lexical = raw if raw.is_absolute() else Path(artifact_cwd or execution_workspace) / raw
        candidate = lexical.resolve()
        try:
            relative = candidate.relative_to(execution_workspace.resolve())
        except ValueError as error:
            raise ArtifactError("worker artifact path escapes its task workspace") from error
        if lexical.is_symlink() or not candidate.is_file():
            raise ArtifactError("worker artifact must be a regular non-symlink file")
        reference = self.artifacts.put_file(
            candidate,
            producer={
                "run_id": run_id,
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "channel": "worker-artifact",
                "role": "collector",
            },
            media_type="text/plain" if self._text_artifact(candidate) else "application/octet-stream",
            redact=self._text_artifact(candidate),
        )
        claimed = data.get("sha256")
        if isinstance(claimed, str):
            normalized = claimed if claimed.startswith("sha256:") else "sha256:" + claimed
            if normalized != reference.source_sha256:
                raise ArtifactError("worker artifact hash claim does not match observed bytes")
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind="artifact",
            data={
                "path": str(relative),
                "source_sha256": reference.source_sha256,
                "source_bytes": reference.source_bytes,
                "worker_hash_matched": isinstance(claimed, str),
            },
            epistemic_status="OBSERVED",
            producer="collector",
            artifact_refs=(reference,),
        )

    def _capture_workspace_diff(self, run_id: str, assignment: Dict[str, str]) -> None:
        if self.workspaces is None or self.artifacts is None:
            return
        handle = self._handles[(assignment["task_id"], assignment["agent_id"])]
        patch, files, status = self.workspaces.diff_snapshot(handle)
        producer = {
            "run_id": run_id,
            "task_id": assignment["task_id"],
            "agent_id": assignment["agent_id"],
            "lease_id": assignment["lease_id"],
            "role": "collector",
        }
        references = [
            self.artifacts.put_bytes(
                patch,
                producer=dict(producer, channel="git-diff"),
                media_type="text/x-diff",
                redact=True,
            )
        ]
        file_records = []
        for path in files:
            reference = self.artifacts.put_file(
                path,
                producer=dict(producer, channel="workspace-file"),
                media_type="text/plain" if self._text_artifact(path) else "application/octet-stream",
                redact=self._text_artifact(path),
            )
            references.append(reference)
            file_records.append(
                {
                    "path": str(path.relative_to(handle.path)),
                    "source_sha256": reference.source_sha256,
                    "source_bytes": reference.source_bytes,
                }
            )
        unique = {reference.digest: reference for reference in references}
        self.orchestrator.record_observed_evidence(
            run_id,
            assignment,
            kind="diff",
            data={"status": list(status), "files": file_records},
            epistemic_status="OBSERVED",
            producer="collector",
            artifact_refs=tuple(unique.values()),
        )

    async def _execute_assignment(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> None:
        state = self.orchestrator.state(run_id)
        agent = self._agent_for(state, assignment["agent_id"])
        execution_workspace = self._execution_workspace(run_id, assignment)
        if self.state_dir is not None:
            bundle = self._admission_for(state, assignment)
            adapter = create_agent_adapter(
                agent["adapter"]["kind"],
                execution_workspace,
                run_id,
                state_dir=self.state_dir,
                sandbox_backend=select_backend(bundle.sandbox_policy),
                sandbox_policy=bundle.sandbox_policy,
                artifact_store=self.artifacts,
                redactor=self.orchestrator.redactor,
            )
        else:
            adapter = create_agent_adapter(agent["adapter"]["kind"], execution_workspace, run_id)
        async def authorize_launch(owned, number):
            self._ensure_source_binding(run_id)
            if self.state_dir is not None:
                adapter.execution_environment = execution_environment(self.state_dir, execution_workspace, bundle)
            if capacity_enabled(state):
                await self._before_launch(run_id, owned, number)
                self._ensure_source_binding(run_id)
                if self.state_dir is not None:
                    adapter.execution_environment = execution_environment(self.state_dir, execution_workspace, bundle)
            if agent["adapter"]["kind"] not in {"process", "codex_oss"}:
                from .revisions import collect_revision_lineage
                from .state import project
                latest = self.orchestrator.state(run_id)
                ancestors = collect_revision_lineage(self.orchestrator.store, run_id) if latest.get("revision") else {}
                adapter.budget_baselines = [budget_baseline(latest)] + [budget_baseline(project(events)) for events in ancestors.values()]
        adapter.before_launch = authorize_launch
        task_status = state["tasks"][assignment["task_id"]]["status"]
        if task_status == "leased":
            if self.workspaces is not None:
                observed_workspace = self.workspaces.refresh_receipt(
                    self._handles[(assignment["task_id"], assignment["agent_id"])]
                )
                if observed_workspace.digest() != bundle.workspace.digest():
                    self.orchestrator.reject_launch(
                        run_id,
                        assignment,
                        WaitingReason(
                            code="WORKSPACE_CONFLICT",
                            detail="workspace changed after admission and before process launch",
                            wake_condition="workspace state is re-probed and a new fenced lease is issued",
                            task_id=assignment["task_id"],
                            box_id=assignment["agent_id"],
                        ),
                    )
                    return
            if not self.orchestrator.start_task(run_id, assignment):
                return
        elif task_status != "running":
            raise StateTransitionError("cannot execute task from {}".format(task_status))

        while True:
            self.orchestrator.heartbeat(run_id, assignment)
            state = self.orchestrator.state(run_id)
            task = state["tasks"][assignment["task_id"]]
            if task.get("runtime_wait"):
                self.orchestrator._emit(run_id, "PROVIDER_BUDGET_WAIT_CLEARED", dict(
                    task_id=task["id"], agent_id=assignment["agent_id"], lease_id=assignment["lease_id"],
                    wait_digest=task["runtime_wait"]["wait_digest"], reason="restart_recheck"))
            packet = self.orchestrator.context_packet(run_id, assignment)
            try:
                execution_agent = self._execution_agent(state, assignment, execution_workspace)
            except (ProviderError, StateTransitionError) as error:
                self._pause_assignment(run_id, assignment, "POLICY_DENIED", str(error))
                return
            if execution_agent["adapter"]["kind"] != "process":
                packet["provider_policy"] = execution_agent["adapter"]["profile_snapshot"]
            cost_budget = self._provider_cost_remaining(state, execution_agent, task["id"], execution_workspace)
            try:
                result = await self._tracked_provider_turn(adapter, run_id,
                    execution_agent,
                    assignment,
                    packet,
                    task["turn_count"] + 1,
                    cost_budget_cents=cost_budget,
                )
            except asyncio.CancelledError as error:
                for observed in getattr(error, "observed_evidence", ()):
                    self._record_observed(run_id, assignment, observed)
                raise
            except CapacityError as error:
                self._pause_assignment(run_id, assignment, "CAPACITY_EXHAUSTED", str(error))
                return
            except ProviderBudgetPending as error:
                try:
                    if getattr(adapter, "hosted_invocation_id", None) is not None:
                        raise CapacityError("budget wait occurred after an invocation intent; reconciliation required")
                    if await self._wait_for_provider_budget(run_id, assignment, error):
                        continue
                except CapacityError as capacity_error:
                    self._pause_assignment(run_id, assignment, "OPERATOR_ATTENTION", str(capacity_error))
                    return
                self._pause_assignment(run_id, assignment, "OPERATOR_ATTENTION",
                    "outstanding provider reservations are not owned live invocations in this runner; reconciliation required")
                return
            except ProviderBudgetError as error:
                self._pause_assignment(run_id, assignment, "OPERATOR_ATTENTION", str(error))
                return
            except (AdapterError, SandboxError, ArtifactError, ProviderError, OSError) as error:
                for observed in getattr(error, "observed_evidence", ()):
                    self._record_observed(run_id, assignment, observed)
                if cost_budget == 0:
                    self._pause_assignment(run_id, assignment, "OPERATOR_ATTENTION", "provider budget exhausted or an unknown charge requires reconciliation: {}".format(error))
                    return
                self.orchestrator.retry_or_block(run_id, assignment, "adapter_error: {}".format(error))
                return

            try:
                observed_evidence = result.pop("_camol_observed_evidence", [])
                usage_invocation_id = result.pop("_camol_usage_invocation_id", None)
                for observed in observed_evidence:
                    self._record_observed(run_id, assignment, observed)
                violations = self.orchestrator.record_turn(
                    run_id, assignment, result, **(
                        {"usage_invocation_id": usage_invocation_id} if usage_invocation_id else {}
                    ),
                )
                for evidence in result["evidence"]:
                    self.orchestrator.record_evidence(run_id, assignment, evidence)
                    if evidence.get("kind") == "artifact":
                        self._capture_worker_artifact(run_id, assignment, execution_workspace, evidence,
                            artifact_cwd=execution_workspace / agent["box"])
                self._capture_workspace_diff(run_id, assignment)
                for message in result.get("messages", []):
                    self.orchestrator.route_message(
                        run_id,
                        assignment,
                        message.get("to_task_id"),
                        message.get("kind", "information"),
                        message.get("body"),
                    )
            except (ValueError, StateTransitionError) as error:
                self.orchestrator.retry_or_block(
                    run_id, assignment, "invalid_agent_result: {}".format(error)
                )
                return
            if violations:
                self.orchestrator.retry_or_block(
                    run_id, assignment, "budget violations: {}".format(", ".join(violations))
                )
                return
            if result["status"] == "blocked":
                self.orchestrator.block_task(run_id, assignment, result["blocker"])
                return
            if result["status"] == "continue":
                continue

            try:
                self.orchestrator.submit_task(run_id, assignment, result["summary"])
                verification_passed = await self._verify(run_id, assignment)
                if verification_passed is None:
                    return
                if not verification_passed:
                    self.orchestrator.retry_or_block(
                        run_id, assignment, "one or more verification commands failed"
                    )
                    return
                self.orchestrator.succeed_task(run_id, assignment)
            except (StateTransitionError, EvaluationError, WorkspaceError, GateError) as error:
                self.orchestrator.retry_or_block(run_id, assignment, str(error))
            return

    async def _resume_verification(
        self,
        run_id: str,
        assignment: Dict[str, str],
    ) -> None:
        state = self.orchestrator.state(run_id)
        task = state["tasks"][assignment["task_id"]]
        if (
            task["verification_history"]
            and task["verification_history"][-1]["passed"] is True
            and task["verification_history"][-1].get("lease_id") == assignment["lease_id"]
            and task["verification_history"][-1].get("fence_digest") == assignment["fence_digest"]
        ):
            try:
                self.orchestrator.succeed_task(run_id, assignment)
            except StateTransitionError as error:
                self.orchestrator.retry_or_block(run_id, assignment, str(error))
            return
        try:
            passed = await self._verify(run_id, assignment)
        except (EvaluationError, WorkspaceError, GateError) as error:
            self.orchestrator.retry_or_block(run_id, assignment, str(error))
            return
        if passed is None:
            return
        if not passed:
            self.orchestrator.retry_or_block(run_id, assignment, "verification failed after resume")
            return
        try:
            self.orchestrator.succeed_task(run_id, assignment)
        except StateTransitionError as error:
            self.orchestrator.retry_or_block(run_id, assignment, str(error))

    def _active_assignments(self, run_id: str, *, include_gate_wait=False) -> Any:
        state = self.orchestrator.state(run_id)
        return [
            {
                "task_id": task["id"],
                "agent_id": task["agent_id"],
                "lease_id": task["lease_id"],
                "status": task["status"],
                "fence_digest": task["fence_digest"],
                "fence": task["lease_fence"],
                "admission_digest": task.get("admission_digest"),
            }
            for task in state["tasks"].values()
            if task["status"] in {"leased", "running", "verifying"}
            and (include_gate_wait or not task.get("gate_wait"))
        ]

    def _admission_for(self, state: Dict[str, Any], assignment: Dict[str, Any]) -> AdmissionBundle:
        payload = state["admissions"].get("{}:{}".format(assignment["task_id"], assignment["agent_id"]))
        if payload is None:
            raise StateTransitionError("active assignment has no admission bundle")
        bundle = AdmissionBundle.from_dict(payload)
        if bundle.digest() != assignment.get("admission_digest"):
            raise StateTransitionError("active assignment admission digest changed")
        return bundle

    def _execution_workspace(self, run_id: str, assignment: Dict[str, Any]) -> Path:
        if self.workspaces is None:
            return self.workspace
        key = (assignment["task_id"], assignment["agent_id"])
        handle = self._handles.get(key)
        if handle is None:
            handle = self.workspaces.prepare_task(run_id, assignment["task_id"], assignment["agent_id"])
            self._handles[key] = handle
        return handle.path

    def _ensure_evaluator(self, run_id: str) -> Optional[EvaluatorBundle]:
        if self.evaluators is None:
            return None
        state = self.orchestrator.state(run_id)
        evaluator_workspace = self.workspace
        source_binding = self._ensure_source_binding(run_id)
        revision_base = (state.get("revision") or {}).get("base_revision")
        if revision_base:
            if self.workspaces is None:
                raise EvaluationError("revised plans need an isolated inherited evaluator baseline")
            baseline = self.workspaces.prepare_verifier(
                run_id, "revision-baseline", "revision-" + revision_base, base_revision=revision_base,
            )
            evaluator_workspace = baseline.path
        elif source_binding:
            baseline = self.workspaces.prepare_verifier(run_id, "source-baseline", "source-" + source_binding["source"]["revision"],
                base_revision=source_binding["source"]["revision"])
            evaluator_workspace = baseline.path
        bundle = self.evaluators.compile(
            state["runbook"], evaluator_workspace, state["plan_digest"]
        )
        self._evaluator_bundle = bundle
        return bundle

    def _ensure_admissions(self, run_id: str) -> None:
        if self.workspaces is None:
            return
        source_binding = self._ensure_source_binding(run_id)
        state = self.orchestrator.state(run_id)
        runbook = state["runbook"]
        maximum = runbook["run"].get("max_concurrency", runbook["run"].get("max_agents"))
        active_reservations = set(self.orchestrator.active_reservation_ids(run_id))
        remaining = max(0, maximum - len(active_reservations))
        if remaining == 0:
            return
        reserved_workers = {
            AdmissionBundle.from_dict(payload).binding.worker_id
            for payload in state["admissions"].values()
            if AdmissionBundle.from_dict(payload).reservation.reservation_id in active_reservations
        }
        controller = AdmissionController(
            runbook,
            self.workspaces,
            clock=self.orchestrator.clock,
        )
        integration_base = state.get("integration_head") or (source_binding["source"]["revision"] if source_binding else self.workspaces.head_revision())
        tasks = [
            task for task in state["tasks"].values()
            if task["status"] in {"pending", "waiting"}
            and task["id"] not in self._paused_tasks
            and all(state["tasks"][item]["status"] == "succeeded" for item in task["depends_on"])
        ]
        idle_agents = [
            agent for agent in state["agents"].values()
            if agent["status"] == "idle" and agent["id"] not in reserved_workers
        ]
        for task in tasks:
            if remaining == 0:
                break
            already = []
            for agent in idle_agents:
                payload = state["admissions"].get("{}:{}".format(task["id"], agent["id"]))
                if payload is not None:
                    bundle = AdmissionBundle.from_dict(payload)
                    if bundle.reservation.reservation_id in active_reservations:
                        already.append(agent["id"])
            if already:
                continue
            eligible = [
                agent for agent in idle_agents
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            if not eligible:
                continue
            for agent in sorted(eligible, key=lambda candidate: self.orchestrator._agent_order(candidate, task)):
                try:
                    bundle, handle = controller.prepare(
                        plan_digest=state["plan_digest"], task=task, agent=agent,
                        granted_by=state["approved_by"], base_revision=integration_base,
                        expected_evaluator_digest=(self._evaluator_bundle.evaluator_digest if self._evaluator_bundle else None),
                    )
                except (AdmissionError, WorkspaceError, SandboxError) as error:
                    code = "WORKSPACE_CONFLICT" if isinstance(error, WorkspaceError) else "POLICY_DENIED"
                    self.orchestrator.wait_task(run_id, task["id"], WaitingReason(
                        code=code, detail="admission preparation failed: {}".format(error),
                        wake_condition="repair the admission prerequisite and retry probing",
                        task_id=task["id"], box_id=agent["id"],
                    ))
                    continue
                self._handles[(task["id"], agent["id"])] = handle
                capacity = self.capacity.reserve_admission(run_id, bundle)
                if capacity["status"] in {"waiting", "denied"}:
                    self.orchestrator.wait_task(run_id, task["id"], WaitingReason(
                        code="CAPACITY_EXHAUSTED" if capacity["status"] == "waiting" else "POLICY_DENIED",
                        detail="shared capacity admission: {}".format(capacity.get("reasons", [])),
                        wake_condition="required capacity is observed fresh and available in the shared broker",
                        task_id=task["id"], box_id=agent["id"],
                    ))
                    continue
                self._ensure_source_binding(run_id)
                require_source_admission(self.orchestrator.state(run_id), bundle)
                decision = self.orchestrator.record_admission(run_id, bundle)
                state = self.orchestrator.state(run_id)
                if decision.ready:
                    remaining -= 1
                    idle_agents = [item for item in idle_agents if item["id"] != agent["id"]]
                    break

    def _deadlock_details(self, run_id: str) -> Dict[str, Any]:
        state = self.orchestrator.state(run_id)
        details = {}
        for task in state["tasks"].values():
            if task["status"] not in {"pending", "waiting"}:
                continue
            unmet = [
                dependency
                for dependency in task["depends_on"]
                if state["tasks"][dependency]["status"] != "succeeded"
            ]
            eligible = [
                agent["id"]
                for agent in state["agents"].values()
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            details[task["id"]] = {
                "unmet_dependencies": unmet,
                "eligible_agents": eligible,
                "attempts": task["attempts"],
            }
        return details

    def force_interrupt(self, run_id: str, *, requested_by: str) -> Dict[str, Any]:
        """After process shutdown, salvage active boxes and revoke their fences."""
        salvaged = []
        revoked = 0
        for assignment in self._active_assignments(run_id, include_gate_wait=True):
            if self.workspaces is not None:
                handle = self._handles.get((assignment["task_id"], assignment["agent_id"]))
                if handle is None:
                    handle = self.workspaces.prepare_task(run_id, assignment["task_id"], assignment["agent_id"])
                    self._handles[(assignment["task_id"], assignment["agent_id"])] = handle
                receipt = self.workspaces.salvage(handle)
                self.orchestrator.record_salvage(
                    run_id, assignment, receipt, reason="forced interruption by {}".format(requested_by)
                )
                salvaged.append({"task_id": assignment["task_id"], "salvage_digest": receipt.digest()})
            self.orchestrator.cancel_lease(run_id, assignment, requested_by)
            revoked += 1
        if self.capacity is not None:
            self.capacity.release_finished(run_id, processes_stopped=True)
        return {"salvaged": salvaged, "revoked": revoked}

    async def _tracked_provider_turn(self, adapter, run_id, agent, assignment, packet, turn_number, **kwargs):
        key = (run_id, assignment["task_id"], assignment["agent_id"], assignment["lease_id"], turn_number)
        if key in self._provider_turns:
            raise ProviderBudgetError("duplicate live provider turn ownership")
        adapter.hosted_invocation_id = None
        self._provider_turns[key] = adapter
        try:
            return await adapter.execute_turn(agent, assignment, packet, turn_number, **kwargs)
        finally:
            self._provider_turns.pop(key, None)

    async def _wait_for_provider_budget(self, run_id, assignment, error):
        keys = {(item["run_id"], item["task_id"], item["agent_id"], item["lease_id"], item["turn_number"])
                for item in error.pending}
        def owned():
            return all(getattr(self._provider_turns.get((item["run_id"], item["task_id"], item["agent_id"],
                       item["lease_id"], item["turn_number"])), "hosted_invocation_id", None) == item["invocation_id"]
                       for item in error.pending)
        if (not keys or any(key[0] != run_id or key[1] == assignment["task_id"] for key in keys)
                or not owned()):
            return False
        pending = sorted((dict(item) for item in error.pending), key=lambda item: item["invocation_id"])
        payload = dict(task_id=assignment["task_id"], agent_id=assignment["agent_id"],
                       lease_id=assignment["lease_id"], code="BUDGET_RESERVED", pending=pending)
        payload["wait_digest"] = canonical_digest(payload)
        self.orchestrator._emit(run_id, "PROVIDER_BUDGET_WAITING", payload)
        reason = "settlement_recheck"
        try:
            if getattr(self, "capacity", None) is not None:
                self.capacity.defer_unlaunched(run_id, assignment, error.unlaunched_binding)
            # No model turn, lease reissue or payment intent is created while
            # waiting. The outer assignment loop continues readiness heartbeats.
            while owned():
                await asyncio.sleep(0.1)
            return True
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            state = self.orchestrator.state(run_id)
            task = state["tasks"][assignment["task_id"]]
            if task.get("lease_id") == assignment["lease_id"] and task.get("runtime_wait", {}).get("wait_digest") == payload["wait_digest"]:
                self.orchestrator._emit(run_id, "PROVIDER_BUDGET_WAIT_CLEARED", dict(
                    task_id=assignment["task_id"], agent_id=assignment["agent_id"], lease_id=assignment["lease_id"],
                    wait_digest=payload["wait_digest"], reason=reason))

    def _pause_assignment(self, run_id: str, assignment: Dict[str, Any], code: str, detail: str) -> None:
        state = self.orchestrator.state(run_id)
        task = state["tasks"][assignment["task_id"]]
        if task["lease_id"] != assignment["lease_id"] or task["status"] not in {"leased", "running", "verifying"}:
            return
        self._paused_tasks.add(task["id"])
        if self.workspaces is not None:
            try:
                self._execution_workspace(run_id, assignment)
                salvage = self.workspaces.salvage(self._handles[(task["id"], assignment["agent_id"])])
                self.orchestrator.record_salvage(run_id, assignment, salvage, reason=detail)
            except (WorkspaceError, OSError) as error:
                code = "OPERATOR_ATTENTION"
                detail += "; worktree retained; salvage failed: {}".format(error)
        self.orchestrator.revoke_lease(
            run_id, assignment, WaitingReason(
                code=code, detail=detail,
                wake_condition="inspect retained work and retry after the prerequisite is repaired",
                task_id=task["id"], box_id=assignment["agent_id"],
            ),
        )

    async def _run_assignment(self, run_id: str, assignment: Dict[str, Any]) -> None:
        """Keep bounded proof alive while a worker or evaluator is in flight."""
        try:
            self._ensure_source_binding(run_id)
        except WorkspaceError as error:
            self._pause_assignment(run_id, assignment, "WORKSPACE_CONFLICT", str(error))
            return
        if self.state_dir is not None:
            boundary_state = self.orchestrator.state(run_id)
            admitted = self._admission_for(boundary_state, assignment)
            try:
                execution_environment(self.state_dir, self._execution_workspace(run_id, assignment), admitted)
            except GitViewError as error:
                self._pause_assignment(run_id, assignment, "POLICY_DENIED", str(error))
                return
            packet_root = (self.state_dir / "packets" / run_id / assignment["task_id"]).resolve()
            output_root = packet_root / "worker-output"
            process_worker = boundary_state["agents"][assignment["agent_id"]]["adapter"]["kind"] == "process"
            for value in admitted.sandbox_policy.write_paths:
                writable = Path(value).resolve()
                protects_control = writable == packet_root or writable in packet_root.parents
                beneath_packets = packet_root in writable.parents
                if protects_control or (beneath_packets and (not process_worker or writable != output_root)):
                    self._pause_assignment(run_id, assignment, "POLICY_DENIED",
                        "legacy admission permits writes to trusted control artifacts; fresh human-approved migration is required")
                    return
        if assignment.get("status") == "verifying":
            state = self.orchestrator.state(run_id)
            task = state["tasks"][assignment["task_id"]]
            if self.capacity is not None:
                try:
                    self.capacity.renew_assignment(run_id, assignment, verification_reconciled=True)
                except CapacityError as error:
                    self._pause_assignment(run_id, assignment, "CAPACITY_EXHAUSTED", str(error))
                    return
            if parse_timestamp(effective_expiry(state, task), "effective lease expiry") <= self.orchestrator.clock():
                try:
                    if self.workspaces is None:
                        raise StateTransitionError("verification resumption needs isolated readiness proof")
                    controller = AdmissionController(state["runbook"], self.workspaces, clock=self.orchestrator.clock)
                    bundle, _ = await asyncio.wait_for(asyncio.to_thread(
                        controller.prepare, plan_digest=state["plan_digest"], task=task,
                        agent=state["agents"][assignment["agent_id"]], granted_by=state["approved_by"],
                        expected_evaluator_digest=self._admission_for(state, assignment).evaluator_digest,
                    ), timeout=max(1.0, min(60.0, self.orchestrator.lease_ttl_seconds)))
                    # Only a captured immutable candidate can use this scope;
                    # it cannot revive an expired worker turn or change fences.
                    self.orchestrator.refresh_active_lease(run_id, assignment, bundle, verification_resume=True)
                except (StateTransitionError, AdmissionError, WorkspaceError, SandboxError, asyncio.TimeoutError) as error:
                    self._pause_assignment(run_id, assignment, "READINESS_STALE", "verification readiness reproof failed: {}".format(error))
                    return
        operation = asyncio.create_task(
            self._resume_verification(run_id, assignment)
            if assignment.get("status") == "verifying"
            else self._execute_assignment(run_id, assignment)
        )
        try:
            while True:
                interval = max(0.05, min(15.0, self.orchestrator.lease_ttl_seconds / 3))
                active_state = self.orchestrator.state(run_id)
                if capacity_enabled(active_state):
                    interval = min(interval, active_state["runbook"]["run"]["capacity_policy"]["reservation_ttl_seconds"] / 3)
                done, _ = await asyncio.wait({operation}, timeout=interval)
                if done:
                    await operation
                    return
                state = self.orchestrator.state(run_id)
                task = state["tasks"][assignment["task_id"]]
                if task["status"] not in {"running", "verifying"}:
                    continue
                if self.capacity is not None:
                    self.capacity.renew_assignment(run_id, assignment)
                self.orchestrator.heartbeat(run_id, assignment)
                remaining = (
                    parse_timestamp(effective_expiry(state, task), "effective lease expiry")
                    - self.orchestrator.clock()
                ).total_seconds()
                if remaining > self.orchestrator.lease_ttl_seconds * 2 / 3:
                    continue
                if self.workspaces is None:
                    raise StateTransitionError("continued lease proof needs an isolated state directory")
                controller = AdmissionController(state["runbook"], self.workspaces, clock=self.orchestrator.clock)
                bundle, _ = await asyncio.wait_for(
                    asyncio.to_thread(
                        controller.prepare, plan_digest=state["plan_digest"], task=task,
                        agent=state["agents"][assignment["agent_id"]], granted_by=state["approved_by"],
                        expected_evaluator_digest=self._admission_for(state, assignment).evaluator_digest,
                    ), timeout=max(0.01, remaining * 0.8),
                )
                # Finishing during a probe is normal; a completed lease must
                # never be resurrected by the delayed observation.
                if operation.done():
                    await operation
                    return
                self.orchestrator.refresh_active_lease(run_id, assignment, bundle)
        except asyncio.CancelledError:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except (StateTransitionError, AdmissionError, WorkspaceError, SandboxError, CapacityError, asyncio.TimeoutError) as error:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            self._pause_assignment(run_id, assignment, "CAPACITY_EXHAUSTED" if isinstance(error, CapacityError) else "READINESS_STALE", "continued lease proof failed: {}".format(error))
        except Exception as error:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            self._pause_assignment(run_id, assignment, "OPERATOR_ATTENTION", "runtime failure: {}: {}".format(type(error).__name__, error))

    async def run_until_terminal(
        self,
        run_id: str,
        *,
        should_drain: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        state = self.orchestrator.state(run_id)
        if state["status"] in {"completed", "blocked", "awaiting_acceptance"}:
            return state
        if self.workspaces is not None:
            self._ensure_evaluator(run_id)
        self.orchestrator.start(run_id)
        self._paused_tasks = set()
        running = {}
        try:
            while True:
                state = self.orchestrator.state(run_id)
                if state["status"] in {"completed", "blocked", "awaiting_acceptance"}:
                    return state
                if self.orchestrator.maybe_finish(run_id):
                    return self.orchestrator.state(run_id)
                for assignment in self._active_assignments(run_id):
                    if assignment["task_id"] not in running:
                        running[assignment["task_id"]] = asyncio.create_task(self._run_assignment(run_id, assignment))
                draining = should_drain is not None and should_drain()
                if not draining:
                    try:
                        self._ensure_admissions(run_id)
                    except (AdmissionError, WorkspaceError, SandboxError, StateTransitionError, CapacityError) as error:
                        if running:
                            raise
                        self.orchestrator.block_run(run_id, "admission_failure", {"detail": str(error)})
                        return self.orchestrator.state(run_id)
                    self._ensure_source_binding(run_id)
                    for assignment in self.orchestrator.lease_ready_tasks(run_id):
                        running[assignment["task_id"]] = asyncio.create_task(self._run_assignment(run_id, assignment))
                if not running:
                    state = self.orchestrator.state(run_id)
                    if draining:
                        return state
                    if any(task.get("gate_wait") for task in state["tasks"].values()):
                        # Deliberate human review is a visible wait, not a
                        # scheduler failure or a reason to spend a new attempt.
                        return state
                    blocked = [task["id"] for task in state["tasks"].values() if task["status"] == "blocked"]
                    if blocked:
                        self.orchestrator.block_run(run_id, "tasks_blocked", {"task_ids": blocked})
                    elif not any(task["status"] == "waiting" for task in state["tasks"].values()):
                        self.orchestrator.block_run(run_id, "scheduler_deadlock", self._deadlock_details(run_id))
                    return self.orchestrator.state(run_id)
                done, _ = await asyncio.wait(set(running.values()), return_when=asyncio.FIRST_COMPLETED)
                for task_id, pending in list(running.items()):
                    if pending in done:
                        del running[task_id]
                        await pending
                if self.capacity is not None:
                    self.capacity.release_finished(run_id, processes_stopped=True)
        finally:
            for pending in running.values():
                pending.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)
            if self.capacity is not None:
                self.capacity.release_finished(run_id, processes_stopped=True)


def summary(state: Dict[str, Any]) -> Dict[str, Any]:
    run_config = state["runbook"]["run"]
    return {
        "run_id": state["run_id"],
        "status": state["status"],
        "max_concurrency": run_config.get("max_concurrency", run_config.get("max_agents")),
        "registered_workers": len(state["agents"]),
        "plan_digest": state["plan_digest"],
        "approved_by": state["approved_by"],
        "total_tokens": state["total_tokens"],
        "agents": {
            agent_id: {
                "role": agent["role"],
                "status": agent["status"],
                "task_id": agent["task_id"],
                "stats": agent["stats"],
            }
            for agent_id, agent in state["agents"].items()
        },
        "tasks": {
            task_id: {
                "status": task["status"],
                "agent_id": task["agent_id"],
                "attempts": task["attempts"],
                "turn_count": task["turn_count"],
                "completed_step_ids": task["completed_step_ids"],
                "blocker": task["blocker"],
                **({"runtime_wait": task["runtime_wait"]} if task.get("runtime_wait") else {}),
            }
            for task_id, task in state["tasks"].items()
        },
        "terminal": state.get("terminal"),
    }
