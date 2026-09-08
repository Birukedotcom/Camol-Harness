"""Authoritative state transitions for a run over an N-worker pool."""

from datetime import datetime, timedelta, timezone
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from .admission import AdmissionBundle
from .evidence import EvidenceRecord
from .effects import EffectRequest, outcome_payload
from .evaluation import CandidateRecord, CounterexampleRecord, IntegrationReceipt
from .events import EVIDENCE_KINDS, new_event
from .hillclimb import agent_efficiency_vector, compare_vectors
from .readiness import CapacityReservation, LeaseFence, ReadinessDecision, WaitingReason
from .probes import Redactor
from .runbook import runbook_digest, validate_runbook
from .schema import canonical_digest, parse_timestamp
from .state import apply_event, empty_state, project
from .store import ConcurrentAppendError, SQLiteEventStore
from .workspace import SalvageReceipt
from .usage import UsageRecord, accounted_tokens, _trusted_receipts
from .leases import effective_expiry, validate_authorization
from .gate_runtime import GateOrchestratorMixin, acceptance_digest, require_integration_gate
from .revisions import RevisionOrchestratorMixin, prior_effect_reuse
from .capacity import CapacityError
from .capacity_runtime import capacity_for_task
from .projection_copy import clone_projection


class StateTransitionError(RuntimeError):
    pass


class Orchestrator(GateOrchestratorMixin, RevisionOrchestratorMixin):
    def __init__(
        self,
        store: SQLiteEventStore,
        actor_id: str = "orchestrator",
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lease_ttl_seconds: int = 90,
        redactor: Optional[Redactor] = None,
    ):
        self.store = store
        self.actor_id = actor_id
        self.clock = clock
        self.lease_ttl_seconds = lease_ttl_seconds
        self.redactor = redactor or Redactor()
        self._projections: Dict[str, Dict[str, Any]] = {}

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise StateTransitionError("orchestrator clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _gate_evidence(payload: Dict[str, Any]) -> bool:
        return (
            payload.get("schema") == EvidenceRecord.SCHEMA
            and EvidenceRecord.from_dict(payload).satisfies_gate()
        )

    def state(self, run_id: str) -> Dict[str, Any]:
        previous = self._projections.get(run_id, empty_state())
        events = self.store.read(run_id, after_seq=previous["last_seq"])
        current = previous
        for event in events:
            current = apply_event(current, event)
        self._projections[run_id] = current
        # Callers may assemble packets from the projection. They cannot mutate
        # durable truth by retaining and editing a previously returned mapping.
        return clone_projection(current)

    def _emit(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        actor_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        expected_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.store.append(
            new_event(
                run_id,
                event_type,
                actor_id or self.actor_id,
                payload,
                causation_id=causation_id,
                correlation_id=correlation_id,
                occurred_at=self._now(),
            ),
            expected_seq=expected_seq,
        )

    def _emit_many(
        self,
        run_id: str,
        records: List[Any],
        *,
        expected_seq: int,
    ) -> List[Dict[str, Any]]:
        events = [
            new_event(run_id, event_type, actor_id, payload, occurred_at=self._now())
            for event_type, actor_id, payload in records
        ]
        return self.store.append_many(events, expected_seq=expected_seq)

    def _require_status(self, run_id: str, *statuses: str) -> Dict[str, Any]:
        state = self.state(run_id)
        if state["status"] not in statuses:
            raise StateTransitionError(
                "run {} must be in {}, not {}".format(run_id, "/".join(statuses), state["status"])
            )
        return state

    def initialize(self, raw_runbook: Dict[str, Any]) -> Dict[str, Any]:
        runbook = validate_runbook(raw_runbook)
        run_id = runbook["run"]["id"]
        digest = runbook_digest(runbook)
        if self.store.has_run(run_id):
            state = self.state(run_id)
            if state["plan_digest"] != digest:
                raise StateTransitionError(
                    "run {} already exists with a different frozen plan".format(run_id)
                )
            return state
        self._emit(run_id, "RUN_CREATED", {"runbook": runbook, "plan_digest": digest})
        return self.state(run_id)

    def bind_source(self, run_id: str, source: Dict[str, Any]) -> None:
        from .source_binding import make_binding, apply_source_binding
        state = self.state(run_id)
        binding = make_binding(source, run_id, state["plan_digest"])
        if state.get("source_binding") == binding:
            return
        event = {"payload": binding, "run_id": run_id, "actor_id": self.actor_id}
        apply_source_binding(state, event)
        self._emit(run_id, "SOURCE_BASELINE_BOUND", binding, expected_seq=state["last_seq"])

    def approve_plan(self, run_id: str, approved_by: str, expected_digest: str) -> None:
        state = self._require_status(run_id, "draft")
        if state["plan_digest"] != expected_digest:
            raise StateTransitionError("the plan changed after review; re-open planning")
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        if state.get("source_binding") and (approved_by in state["agents"] or self.actor_id in state["agents"]):
            raise StateTransitionError("source-bound approval requires a human owner, not a registered worker")
        self._emit(
            run_id,
            "PLAN_APPROVED",
            {"approved_by": approved_by, "plan_digest": expected_digest,
             **({"source_binding_digest": canonical_digest(state["source_binding"])} if state.get("source_binding") else {})},
        )

    def start(self, run_id: str) -> None:
        state = self._require_status(run_id, "ready", "running")
        if state["status"] == "ready":
            self._emit(run_id, "RUN_STARTED", {})

    def ready_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        state = self._require_status(run_id, "running")
        ready = []
        for task in state["tasks"].values():
            if task["status"] != "pending":
                continue
            if all(state["tasks"][dependency]["status"] == "succeeded" for dependency in task["depends_on"]):
                ready.append(task)
        return ready

    def _dependencies_green(self, state: Dict[str, Any], task: Dict[str, Any]) -> bool:
        return all(state["tasks"][dependency]["status"] == "succeeded" for dependency in task["depends_on"])

    def _wait_task(self, run_id: str, task_id: str, reason: WaitingReason) -> None:
        state = self.state(run_id)
        task = state["tasks"][task_id]
        if task["status"] == "waiting":
            if task.get("waiting") == reason.to_dict():
                return
            self._emit(run_id, "TASK_WAITING", {"task_id": task_id, "reason": reason.to_dict()})
            return
        if task["status"] != "pending":
            raise StateTransitionError("only a pending task can enter a typed wait")
        self._emit(run_id, "TASK_WAITING", {"task_id": task_id, "reason": reason.to_dict()})

    def wait_task(self, run_id: str, task_id: str, reason: WaitingReason) -> None:
        """Expose a typed, restartable wait to admission and supervision code."""
        if reason.task_id != task_id:
            raise StateTransitionError("waiting reason does not match its task")
        self._wait_task(run_id, task_id, reason)

    def release_reservation(self, run_id: str, reservation_id: str, reason: str) -> None:
        state = self.state(run_id)
        if reservation_id in state["released_reservation_ids"]:
            return
        if reservation_id not in state["reservations"]:
            raise StateTransitionError("unknown reservation {}".format(reservation_id))
        self._emit(run_id, "RESERVATION_RELEASED", {"reservation_id": reservation_id, "reason": reason})

    def _active_reservations(self, state: Dict[str, Any], now: str) -> List[CapacityReservation]:
        active = []
        for reservation_id, payload in state["reservations"].items():
            reservation = CapacityReservation.from_dict(payload)
            if reservation_id not in state["released_reservation_ids"] and reservation.is_active(now):
                active.append(reservation)
        return active

    def active_reservation_ids(self, run_id: str) -> List[str]:
        state = self.state(run_id)
        return [item.reservation_id for item in self._active_reservations(state, self._now())]

    def record_admission(self, run_id: str, bundle: AdmissionBundle) -> ReadinessDecision:
        """Persist a complete candidate bundle; red bundles become typed waits."""
        state = self._require_status(run_id, "running")
        from .source_binding import require_source_admission
        require_source_admission(state, bundle)
        subject = bundle.binding
        if subject.run_id != run_id or subject.plan_digest != state["plan_digest"]:
            raise StateTransitionError("admission bundle belongs to another run or plan")
        task = state["tasks"].get(subject.task_id)
        agent = state["agents"].get(subject.worker_id)
        if task is None or agent is None or subject.box_id != agent["id"]:
            raise StateTransitionError("admission bundle names an unknown task/worker/box subject")
        if not set(task["capabilities"]).issubset(set(agent["capabilities"])):
            raise StateTransitionError("admission worker does not cover task capabilities")
        if task["status"] not in {"pending", "waiting"}:
            raise StateTransitionError("admission can only be recorded for a pending or waiting task")
        key = "{}:{}".format(subject.task_id, subject.worker_id)
        previous_payload = state["admissions"].get(key)
        previous = AdmissionBundle.from_dict(previous_payload) if previous_payload is not None else None
        now = self._now()
        dependencies_green = self._dependencies_green(state, task)
        decision = bundle.decision(
            now=now,
            plan_digest=state["plan_digest"],
            plan_frozen=state["approved_by"] is not None,
            dependencies_green=dependencies_green,
        )
        if previous_payload is not None:
            if previous.digest() == bundle.digest():
                if previous.reservation.reservation_id in state["released_reservation_ids"]:
                    return ReadinessDecision(
                        ready=False,
                        reasons=(
                            WaitingReason(
                                code="CAPACITY_EXHAUSTED",
                                detail="the persisted admission reservation was already released",
                                wake_condition="a new capacity reservation and readiness receipt are recorded",
                                task_id=task["id"],
                                box_id=agent["id"],
                            ),
                        ),
                    )
                records = []
                if decision.ready and task["status"] == "waiting":
                    records.append(("TASK_WAIT_CLEARED", self.actor_id, {"task_id": task["id"]}))
                elif not decision.ready:
                    records.append(
                        (
                            "RESERVATION_RELEASED",
                            self.actor_id,
                            {"reservation_id": bundle.reservation.reservation_id, "reason": "admission_not_ready"},
                        )
                    )
                    if task.get("waiting") != decision.reasons[0].to_dict():
                        records.append(
                            ("TASK_WAITING", self.actor_id, {"task_id": task["id"], "reason": decision.reasons[0].to_dict()})
                        )
                if records:
                    self._emit_many(run_id, records, expected_seq=state["last_seq"])
                return decision
        if bundle.reservation.reservation_id in state["reservations"]:
            raise StateTransitionError("capacity reservation id is already recorded")
        replaced_reservation_id = previous.reservation.reservation_id if previous is not None else None
        active_slots = sum(
            item.concurrency_slots
            for item in self._active_reservations(state, now)
            if item.reservation_id != replaced_reservation_id
        )
        maximum = state["runbook"]["run"].get("max_concurrency", state["runbook"]["run"].get("max_agents"))
        if active_slots + bundle.reservation.concurrency_slots > maximum:
            raise StateTransitionError("capacity reservation ceiling is exhausted")
        existing_receipt = state["readiness_receipts"].get(bundle.receipt.receipt_id)
        if existing_receipt is not None and existing_receipt != bundle.receipt.to_dict():
            raise StateTransitionError("readiness receipt id is already bound to different evidence")
        records = []
        if (
            previous is not None
            and previous.reservation.reservation_id not in state["released_reservation_ids"]
        ):
            records.append(
                (
                    "RESERVATION_RELEASED",
                    self.actor_id,
                    {"reservation_id": previous.reservation.reservation_id, "reason": "admission_superseded"},
                )
            )
        if existing_receipt is None:
            records.append(("READINESS_RECORDED", self.actor_id, {"receipt": bundle.receipt.to_dict()}))
        records.append(
            (
                "ADMISSION_RECORDED",
                self.actor_id,
                {"bundle": bundle.to_dict(), "bundle_digest": bundle.digest()},
            )
        )
        if decision.ready and task["status"] == "waiting":
            records.append(("TASK_WAIT_CLEARED", self.actor_id, {"task_id": task["id"]}))
        elif not decision.ready:
            records.append(
                (
                    "RESERVATION_RELEASED",
                    self.actor_id,
                    {"reservation_id": bundle.reservation.reservation_id, "reason": "admission_not_ready"},
                )
            )
            if task.get("waiting") != decision.reasons[0].to_dict():
                records.append(
                    ("TASK_WAITING", self.actor_id, {"task_id": task["id"], "reason": decision.reasons[0].to_dict()})
                )
        try:
            self._emit_many(run_id, records, expected_seq=state["last_seq"])
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during admission; retry") from error
        return decision

    def _bundle_for(self, state: Dict[str, Any], task_id: str, agent_id: str) -> Optional[AdmissionBundle]:
        payload = state["admissions"].get("{}:{}".format(task_id, agent_id))
        return AdmissionBundle.from_dict(payload) if payload is not None else None

    @staticmethod
    def _agent_order(agent: Dict[str, Any], task: Dict[str, Any]) -> Any:
        efficiency = agent_efficiency_vector(agent)
        capability_surplus = len(set(agent["capabilities"]) - set(task["capabilities"]))
        return (
            capability_surplus,
            -efficiency["successful_tasks"],
            efficiency["failed_attempts"],
            -efficiency["verified_steps_per_1k_tokens"],
            efficiency["total_tokens"],
            agent["id"],
        )

    def lease_ready_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        state = self._require_status(run_id, "running")
        run_config = state["runbook"]["run"]
        max_concurrency = run_config.get("max_concurrency", run_config.get("max_agents"))
        active_count = sum(agent["status"] != "idle" for agent in state["agents"].values())
        available_capacity = max(0, max_concurrency - active_count)
        if available_capacity == 0:
            return []
        idle_agents = [agent for agent in state["agents"].values() if agent["status"] == "idle"]
        assignments = []
        for task in list(state["tasks"].values()):
            if task["status"] != "pending":
                continue
            if not self._dependencies_green(state, task):
                self._wait_task(
                    run_id,
                    task["id"],
                    WaitingReason(
                        code="WAITING_DEPENDENCY",
                        detail="task dependencies are not all green",
                        wake_condition="all depends_on tasks succeeded",
                        task_id=task["id"],
                    ),
                )
                continue
            eligible = [
                agent
                for agent in idle_agents
                if set(task["capabilities"]).issubset(set(agent["capabilities"]))
            ]
            if not eligible:
                self._wait_task(
                    run_id,
                    task["id"],
                    WaitingReason(
                        code="CAPACITY_EXHAUSTED",
                        detail="no idle registered worker covers the task capabilities",
                        wake_condition="an eligible worker becomes idle or is registered",
                        task_id=task["id"],
                    ),
                )
                continue
            candidates = []
            rejected_reasons = []
            for agent in eligible:
                bundle = self._bundle_for(state, task["id"], agent["id"])
                if bundle is None:
                    rejected_reasons.append(
                        WaitingReason(
                            code="READINESS_STALE",
                            detail="no complete admission bundle exists for task {} and box {}".format(task["id"], agent["id"]),
                            wake_condition="workspace prepared, capacity reserved, authority granted, and readiness re-probed",
                            task_id=task["id"],
                            box_id=agent["id"],
                        )
                    )
                    continue
                if bundle.reservation.reservation_id in state["released_reservation_ids"]:
                    rejected_reasons.append(
                        WaitingReason(
                            code="CAPACITY_EXHAUSTED",
                            detail="the candidate reservation has been released",
                            wake_condition="a new capacity reservation and bound readiness receipt are recorded",
                            task_id=task["id"],
                            box_id=agent["id"],
                        )
                    )
                    continue
                decision = bundle.decision(
                    now=self._now(),
                    plan_digest=state["plan_digest"],
                    plan_frozen=state["approved_by"] is not None,
                    dependencies_green=True,
                )
                if decision.ready:
                    candidates.append((agent, bundle))
                else:
                    rejected_reasons.extend(decision.reasons)
            if not candidates:
                self._wait_task(run_id, task["id"], rejected_reasons[0])
                continue
            agent, bundle = sorted(candidates, key=lambda item: self._agent_order(item[0], task))[0]
            # Re-read immediately before issuing the fence. The compare-and-
            # append below makes this decision atomic across daemon processes.
            latest = self.state(run_id)
            latest_task = latest["tasks"][task["id"]]
            latest_agent = latest["agents"][agent["id"]]
            latest_bundle = self._bundle_for(latest, task["id"], agent["id"])
            latest_active = sum(item["status"] != "idle" for item in latest["agents"].values())
            if (
                latest_task["status"] != "pending"
                or latest_agent["status"] != "idle"
                or latest_active >= max_concurrency
                or latest_bundle is None
                or latest_bundle.digest() != bundle.digest()
                or latest_bundle.reservation.reservation_id in latest["released_reservation_ids"]
            ):
                continue
            bundle = latest_bundle
            lease_now = self._now()
            try:
                shared_capacity = capacity_for_task(latest, task["id"], agent["id"], now=lease_now, local_reservation_id=bundle.reservation.reservation_id)
            except CapacityError as error:
                self._wait_task(run_id, task["id"], WaitingReason(code="CAPACITY_EXHAUSTED", detail=str(error), wake_condition="obtain fresh shared capacity", task_id=task["id"], box_id=agent["id"]))
                continue
            latest_decision = bundle.decision(
                now=lease_now,
                plan_digest=latest["plan_digest"],
                plan_frozen=latest["approved_by"] is not None,
                dependencies_green=self._dependencies_green(latest, latest_task),
            )
            if not latest_decision.ready:
                self._wait_task(run_id, task["id"], latest_decision.reasons[0])
                continue
            lease_id = str(uuid4())
            now = parse_timestamp(lease_now, "lease now")
            maximum_expiry = now + timedelta(seconds=self.lease_ttl_seconds)
            expires = min(
                maximum_expiry,
                parse_timestamp(bundle.receipt.expires_at, "receipt expires_at"),
                parse_timestamp(bundle.grant.expires_at, "grant expires_at"),
                parse_timestamp(bundle.reservation.expires_at, "reservation expires_at"),
            )
            if shared_capacity is not None:
                expires = min(expires, parse_timestamp(shared_capacity["expires_at"], "global capacity expiry"))
            epoch = latest["lease_epochs"].get(task["id"], 0) + 1
            fence = LeaseFence(
                lease_id=lease_id,
                **bundle.binding.subject(),
                epoch=epoch,
                plan_digest=latest["plan_digest"],
                box_binding_digest=bundle.binding.digest(),
                evaluator_digest=bundle.evaluator_digest,
                workspace_digest=bundle.workspace.digest(),
                authority_digest=bundle.authority_policy.digest(),
                probe_policy_digest=bundle.probe_policy.digest(),
                readiness_digest=bundle.receipt.digest(),
                grant_digest=bundle.grant.digest(),
                reservation_digest=bundle.reservation.digest(),
                issued_at=now.isoformat(timespec="microseconds"),
                expires_at=expires.isoformat(timespec="microseconds"),
            )
            try:
                event = self._emit(
                    run_id,
                    "TASK_LEASED",
                    {
                        "task_id": task["id"],
                        "agent_id": agent["id"],
                        "box_id": bundle.binding.box_id,
                        "lease_id": lease_id,
                        "fence": fence.to_dict(),
                        "fence_digest": fence.digest(),
                        "admission_digest": bundle.digest(),
                    },
                    expected_seq=latest["last_seq"],
                )
            except ConcurrentAppendError:
                # Another scheduler changed the run. The next scheduling pass
                # will recompute from the new projection; no stale lease lands.
                continue
            assignments.append(event["payload"])
            idle_agents = [candidate for candidate in idle_agents if candidate["id"] != agent["id"]]
            if len(assignments) == available_capacity:
                break
        return assignments

    def _require_lease(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        lease_id: str,
        *task_statuses: str,
        fence_digest: Optional[str] = None,
        require_fresh: bool = True,
    ) -> Any:
        state = self._require_status(run_id, "running")
        task = state["tasks"].get(task_id)
        if (
            task is None
            or task["status"] not in task_statuses
            or task["agent_id"] != agent_id
            or task["lease_id"] != lease_id
            or fence_digest is None
            or task.get("fence_digest") != fence_digest
        ):
            raise StateTransitionError(
                "agent {} does not hold the active lease for task {}".format(agent_id, task_id)
            )
        fence = LeaseFence.from_dict(task["lease_fence"])
        now = parse_timestamp(self._now(), "lease now")
        if require_fresh and not (
            parse_timestamp(fence.issued_at, "lease issued") <= now
            < parse_timestamp(effective_expiry(state, task), "effective lease expiry")
        ):
            raise StateTransitionError("the active lease fence is expired")
        if require_fresh:
            try:
                capacity_for_task(state, task_id, agent_id, now=self._now())
            except CapacityError as error:
                raise StateTransitionError(str(error)) from error
        return state, task

    def _active_bundle(self, state: Dict[str, Any], task: Dict[str, Any]) -> AdmissionBundle:
        bundle = self._bundle_for(state, task["id"], task["agent_id"])
        if bundle is None or bundle.digest() != task.get("admission_digest"):
            raise StateTransitionError("active lease admission bundle is missing or changed")
        authorization = state.get("lease_authorizations", {}).get(task["lease_id"])
        if authorization is not None and authorization["fence_digest"] == task["fence_digest"]:
            return AdmissionBundle.from_dict(authorization["bundle"])
        return bundle

    def start_task(self, run_id: str, assignment: Dict[str, str]) -> bool:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            fence_digest=assignment.get("fence_digest"),
            require_fresh=False,
        )
        bundle = self._active_bundle(state, task)
        fence = LeaseFence.from_dict(task["lease_fence"])
        now = self._now()
        decision = bundle.decision(
            now=now,
            plan_digest=state["plan_digest"],
            plan_frozen=state["approved_by"] is not None,
            dependencies_green=self._dependencies_green(state, task),
        )
        reason = None
        if not fence.is_fresh(now):
            reason = WaitingReason(
                code="READINESS_STALE",
                detail="lease fence expired before process launch",
                wake_condition="capacity re-reserved and readiness re-probed",
                task_id=task["id"],
                box_id=task["agent_id"],
            )
        elif not decision.ready:
            reason = decision.reasons[0]
        elif fence.bound_digests() != {
            "plan_digest": state["plan_digest"],
            "box_binding_digest": bundle.binding.digest(),
            "evaluator_digest": bundle.evaluator_digest,
            "workspace_digest": bundle.workspace.digest(),
            "authority_digest": bundle.authority_policy.digest(),
            "probe_policy_digest": bundle.probe_policy.digest(),
            "readiness_digest": bundle.receipt.digest(),
            "grant_digest": bundle.grant.digest(),
            "reservation_digest": bundle.reservation.digest(),
        }:
            reason = WaitingReason(
                code="POLICY_DENIED",
                detail="lease fence does not bind the current admission records",
                wake_condition="a new fenced lease is issued",
                task_id=task["id"],
                box_id=task["agent_id"],
            )
        if reason is not None:
            try:
                self._emit_many(
                    run_id,
                    [
                        (
                            "TASK_LEASE_REJECTED",
                            self.actor_id,
                            {"task_id": task["id"], "lease_id": task["lease_id"], "reason": reason.to_dict()},
                        ),
                        (
                            "RESERVATION_RELEASED",
                            self.actor_id,
                            {"reservation_id": bundle.reservation.reservation_id, "reason": "launch_gate_rejected"},
                        ),
                    ],
                    expected_seq=state["last_seq"],
                )
            except ConcurrentAppendError as error:
                raise StateTransitionError("run state changed during launch rejection; retry") from error
            return False
        try:
            self._emit(
                run_id,
                "TASK_STARTED",
                dict(assignment),
                actor_id=assignment["agent_id"],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed before task start; retry") from error
        return True

    def reject_launch(
        self,
        run_id: str,
        assignment: Dict[str, str],
        reason: WaitingReason,
    ) -> None:
        """Reject a leased process before exec when a launch-time observation changed."""
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            fence_digest=assignment.get("fence_digest"),
            require_fresh=False,
        )
        if reason.task_id != task["id"] or reason.box_id not in (None, task["agent_id"]):
            raise StateTransitionError("launch rejection reason does not match the active lease")
        bundle = self._active_bundle(state, task)
        try:
            self._emit_many(
                run_id,
                [
                    (
                        "TASK_LEASE_REJECTED",
                        self.actor_id,
                        {"task_id": task["id"], "lease_id": task["lease_id"], "reason": reason.to_dict()},
                    ),
                    (
                        "RESERVATION_RELEASED",
                        self.actor_id,
                        {"reservation_id": bundle.reservation.reservation_id, "reason": "launch_gate_rejected"},
                    ),
                ],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during launch rejection; retry") from error

    def heartbeat(self, run_id: str, assignment: Dict[str, str]) -> None:
        """Record liveness only for the currently fenced lease."""
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        fence = LeaseFence.from_dict(task["lease_fence"])
        try:
            self._emit(
                run_id,
                "LEASE_HEARTBEAT",
                {
                    "task_id": task["id"],
                    "lease_id": fence.lease_id,
                    "fence_digest": fence.digest(),
                    "observed_at": self._now(),
                },
                actor_id=assignment["agent_id"],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during heartbeat; retry") from error

    def refresh_active_lease(
        self, run_id: str, assignment: Dict[str, str], bundle: AdmissionBundle, *, verification_resume: bool = False
    ) -> Dict[str, Any]:
        """Re-prove continued authority without invalidating an in-flight packet."""
        state, task = self._require_lease(
            run_id, assignment["task_id"], assignment["agent_id"], assignment["lease_id"],
            "running", "verifying", fence_digest=assignment.get("fence_digest"),
            require_fresh=not verification_resume,
        )
        now = self._now()
        expires = min(
            parse_timestamp(now, "refresh now") + timedelta(seconds=self.lease_ttl_seconds),
            parse_timestamp(bundle.receipt.expires_at, "receipt expiry"),
            parse_timestamp(bundle.grant.expires_at, "grant expiry"),
            parse_timestamp(bundle.reservation.expires_at, "reservation expiry"),
        )
        shared_capacity = capacity_for_task(state, task["id"], task["agent_id"], now=now)
        if shared_capacity is not None:
            expires = min(expires, parse_timestamp(shared_capacity["expires_at"], "shared capacity expiry"))
        payload = {
            "task_id": task["id"], "lease_id": task["lease_id"],
            "fence_digest": task["fence_digest"], "bundle": bundle.to_dict(),
            "bundle_digest": bundle.digest(), "refreshed_at": now,
            "expires_at": expires.isoformat(timespec="microseconds"),
            "scope": "verification_resume" if verification_resume else "active",
        }
        try:
            from .source_binding import require_source_admission
            require_source_admission(state, bundle)
            validate_authorization(state, payload)
            self._emit(run_id, "LEASE_AUTHORIZATION_REFRESHED", payload, expected_seq=state["last_seq"])
        except (ValueError, ConcurrentAppendError) as error:
            raise StateTransitionError("lease authorization refresh failed: {}".format(error)) from error
        return payload

    def renew_lease(self, run_id: str, assignment: Dict[str, str]) -> Dict[str, Any]:
        """Renew the same epoch only while every bound admission input remains fresh."""
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        bundle = self._active_bundle(state, task)
        decision = bundle.decision(
            now=self._now(),
            plan_digest=state["plan_digest"],
            plan_frozen=state["approved_by"] is not None,
            dependencies_green=self._dependencies_green(state, task),
        )
        if not decision.ready:
            raise StateTransitionError("lease cannot renew: {}".format(decision.reasons[0].detail))
        old = LeaseFence.from_dict(task["lease_fence"])
        now = parse_timestamp(self._now(), "lease renewal now")
        expires = min(
            now + timedelta(seconds=self.lease_ttl_seconds),
            parse_timestamp(bundle.receipt.expires_at, "receipt expires_at"),
            parse_timestamp(bundle.grant.expires_at, "grant expires_at"),
            parse_timestamp(bundle.reservation.expires_at, "reservation expires_at"),
        )
        if expires <= now:
            raise StateTransitionError("lease cannot renew beyond expired admission evidence")
        renewed = LeaseFence(
            lease_id=old.lease_id,
            run_id=old.run_id,
            task_id=old.task_id,
            box_id=old.box_id,
            worker_id=old.worker_id,
            target_id=old.target_id,
            epoch=old.epoch,
            plan_digest=old.plan_digest,
            box_binding_digest=old.box_binding_digest,
            evaluator_digest=old.evaluator_digest,
            workspace_digest=old.workspace_digest,
            authority_digest=old.authority_digest,
            probe_policy_digest=old.probe_policy_digest,
            readiness_digest=old.readiness_digest,
            grant_digest=old.grant_digest,
            reservation_digest=old.reservation_digest,
            issued_at=now.isoformat(timespec="microseconds"),
            expires_at=expires.isoformat(timespec="microseconds"),
        )
        try:
            self._emit(
                run_id,
                "LEASE_RENEWED",
                {
                    "task_id": task["id"],
                    "lease_id": old.lease_id,
                    "fence": renewed.to_dict(),
                    "fence_digest": renewed.digest(),
                },
                actor_id=assignment["agent_id"],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during lease renewal; retry") from error
        return dict(assignment, fence=renewed.to_dict(), fence_digest=renewed.digest())

    def revoke_lease(self, run_id: str, assignment: Dict[str, str], reason: WaitingReason) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
            require_fresh=False,
        )
        bundle = self._active_bundle(state, task)
        try:
            self._emit_many(
                run_id,
                [
                    (
                        "LEASE_REVOKED",
                        self.actor_id,
                        {"task_id": task["id"], "lease_id": task["lease_id"], "reason": reason.to_dict()},
                    ),
                    (
                        "RESERVATION_RELEASED",
                        self.actor_id,
                        {"reservation_id": bundle.reservation.reservation_id, "reason": "lease_revoked"},
                    ),
                ],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during lease revocation; retry") from error

    def cancel_lease(self, run_id: str, assignment: Dict[str, str], requested_by: str) -> None:
        """Cancel active work safely; accepted state remains and the task is restartable."""
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise ValueError("requested_by is required")
        self.revoke_lease(
            run_id,
            assignment,
            WaitingReason(
                code="OPERATOR_ATTENTION",
                detail="active lease was cancelled by {}".format(requested_by),
                wake_condition="operator approves a new admission and lease",
                task_id=assignment["task_id"],
                box_id=assignment["agent_id"],
            ),
        )

    def begin_effect(
        self,
        run_id: str,
        assignment: Dict[str, str],
        *,
        idempotency_key: str,
        provider: str,
        operation: str,
        target: str,
        request: Dict[str, Any],
        approval_digest: str,
        requested_by: str,
    ) -> tuple:
        """Persist remote-mutation intent before execution.

        Returns ``(record, True)`` only for a newly committed intent. Replaying
        the same key returns ``False`` and therefore never authorizes a blind
        duplicate call.
        """
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            fence_digest=assignment.get("fence_digest"),
        )
        # The request body is never persisted, but its identity must be hashed
        # before redaction: two different secret-bearing mutations must not
        # collapse to the same idempotency binding.
        request_digest = canonical_digest(request)
        prior = prior_effect_reuse(state, task["id"], provider=provider, operation=operation, target=self.redactor.text(target),
                                   request_digest=request_digest, idempotency_key=idempotency_key)
        if prior is not None:
            self._emit(run_id, "REVISION_EFFECT_REUSED", {"effect_id": prior["effect_id"], "task_id": task["id"],
                                                         "lease_id": assignment["lease_id"], "fence_digest": assignment["fence_digest"],
                                                         "request_digest": request_digest}, expected_seq=state["last_seq"])
            record_data = {key: prior[key] for key in EffectRequest.FIELDS if key in prior}
            record_data["state"] = "EFFECT_REQUESTED"
            return EffectRequest.from_dict(record_data), False
        for existing in state.get("effects", {}).values():
            if existing["idempotency_key"] != idempotency_key:
                continue
            same = all(existing.get(name) == value for name, value in {
                "run_id": run_id,
                "task_id": task["id"],
                "lease_id": assignment["lease_id"],
                "fence_digest": assignment["fence_digest"],
                "provider": provider,
                "operation": operation,
                "target": target,
                "request_digest": request_digest,
                "approval_digest": approval_digest,
            }.items())
            if not same:
                raise StateTransitionError("idempotency key is already bound to a different effect")
            record_data = {key: existing[key] for key in EffectRequest.FIELDS if key in existing}
            record_data["state"] = "EFFECT_REQUESTED"
            return EffectRequest.from_dict(record_data), False
        record = EffectRequest(
            effect_id="effect-" + uuid4().hex,
            run_id=run_id,
            task_id=task["id"],
            lease_id=assignment["lease_id"],
            fence_digest=assignment["fence_digest"],
            idempotency_key=idempotency_key,
            provider=provider,
            operation=operation,
            target=self.redactor.text(target),
            request_digest=request_digest,
            approval_digest=approval_digest,
            requested_by=requested_by,
            requested_at=self._now(),
        )
        try:
            self._emit(
                run_id,
                "EFFECT_REQUESTED",
                record.to_dict(),
                actor_id=requested_by,
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed while requesting an external effect; retry") from error
        return record, True

    def resolve_effect(
        self,
        run_id: str,
        effect_id: str,
        state_name: str,
        *,
        outcome: Optional[Dict[str, Any]] = None,
        readback: Optional[Dict[str, Any]] = None,
        reconciled: bool = False,
    ) -> None:
        """Record an effect result; UNKNOWN can only resolve through readback."""
        state = self._require_status(run_id, "running")
        effect = state.get("effects", {}).get(effect_id)
        if effect is None:
            raise StateTransitionError("unknown external effect: {}".format(effect_id))
        if effect["state"] in {"EFFECT_CONFIRMED", "EFFECT_REJECTED"}:
            raise StateTransitionError("external effect already has a terminal outcome")
        if effect["state"] == "EFFECT_UNKNOWN" and state_name in {"EFFECT_CONFIRMED", "EFFECT_REJECTED"}:
            if not reconciled or readback is None:
                raise StateTransitionError("EFFECT_UNKNOWN requires provider readback before resolution")
        payload = outcome_payload(
            effect_id,
            state_name,
            resolved_at=self._now(),
            outcome=self.redactor.value(outcome) if outcome is not None else None,
            readback=self.redactor.value(readback) if readback is not None else None,
            reconciled=reconciled,
        )
        self._emit(run_id, state_name, payload, expected_seq=state["last_seq"])

    def mark_interrupted_effects_unknown(self, run_id: str) -> List[str]:
        """On daemon recovery, refuse to infer that an in-flight call failed."""
        changed = []
        while True:
            state = self._require_status(run_id, "running")
            pending = next(
                (effect for effect in state.get("effects", {}).values() if effect["state"] == "EFFECT_REQUESTED"),
                None,
            )
            if pending is None:
                return changed
            self._emit(
                run_id,
                "EFFECT_UNKNOWN",
                outcome_payload(pending["effect_id"], "EFFECT_UNKNOWN", resolved_at=self._now()),
                expected_seq=state["last_seq"],
            )
            changed.append(pending["effect_id"])

    def record_salvage(
        self,
        run_id: str,
        assignment: Dict[str, str],
        salvage: SalvageReceipt,
        *,
        reason: str,
    ) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased", "running", "verifying",
            fence_digest=assignment.get("fence_digest"),
            require_fresh=False,
        )
        if not isinstance(salvage, SalvageReceipt):
            raise ValueError("salvage must be a SalvageReceipt")
        self._emit(
            run_id,
            "WORKSPACE_SALVAGED",
            {
                "run_id": run_id,
                "task_id": task["id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "fence_digest": assignment["fence_digest"],
                "reason": self.redactor.text(reason),
                "salvage": salvage.to_dict(),
                "salvage_digest": salvage.digest(),
            },
            expected_seq=state["last_seq"],
        )

    def record_candidate(self, run_id: str, assignment: Dict[str, str], record: CandidateRecord) -> None:
        state, _ = self._require_lease(
            run_id, assignment["task_id"], assignment["agent_id"], assignment["lease_id"],
            "verifying", fence_digest=assignment.get("fence_digest"),
        )
        if not isinstance(record, CandidateRecord):
            raise ValueError("record must be a CandidateRecord")
        self._emit(
            run_id, "CANDIDATE_CAPTURED",
            {"candidate": record.to_dict(), "candidate_digest": record.digest()},
            expected_seq=state["last_seq"],
        )

    def record_counterexample(self, run_id: str, record: CounterexampleRecord) -> None:
        state = self._require_status(run_id, "running")
        if not isinstance(record, CounterexampleRecord):
            raise ValueError("record must be a CounterexampleRecord")
        self._emit(run_id, "COUNTEREXAMPLE_RECORDED", record.to_dict(), expected_seq=state["last_seq"])

    def accept_integration(
        self, run_id: str, assignment: Dict[str, str], receipt: IntegrationReceipt
    ) -> None:
        state, _ = self._require_lease(
            run_id, assignment["task_id"], assignment["agent_id"], assignment["lease_id"],
            "verifying", fence_digest=assignment.get("fence_digest"),
        )
        if not isinstance(receipt, IntegrationReceipt):
            raise ValueError("receipt must be an IntegrationReceipt")
        require_integration_gate(state, receipt)
        self._emit(
            run_id, "INTEGRATION_ACCEPTED",
            {"receipt": receipt.to_dict(), "receipt_digest": receipt.digest()},
            expected_seq=state["last_seq"],
        )

    def context_packet(self, run_id: str, assignment: Dict[str, str]) -> Dict[str, Any]:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "leased",
            "running",
            fence_digest=assignment.get("fence_digest"),
        )
        runbook = state["runbook"]
        completed = set(task["completed_step_ids"])
        dependency_receipts = []
        for dependency_id in task["depends_on"]:
            dependency = state["tasks"][dependency_id]
            compact_evidence = {}
            for evidence_id in dependency["evidence_ids"]:
                evidence = state["evidence"][evidence_id]
                if evidence["kind"] not in {"artifact", "claim", "test_result"}:
                    continue
                if evidence.get("schema") == EvidenceRecord.SCHEMA and not self._gate_evidence(evidence):
                    continue
                data = evidence["data"]
                if evidence["kind"] == "test_result" and isinstance(data, dict):
                    data = {
                        "passed": data.get("passed"),
                        "checks": [
                            {
                                key: check.get(key)
                                for key in ("purpose", "passed", "phase", "evaluated_task_id", "error")
                                if key in check
                            }
                            for check in data.get("checks", [])[-20:]
                            if isinstance(check, dict)
                        ],
                    }
                compact_evidence[evidence["kind"]] = {
                    "evidence_id": evidence_id,
                    "kind": evidence["kind"],
                    "epistemic_status": evidence.get("epistemic_status", "UNVERIFIED"),
                    "data": data,
                    "artifact_digests": [
                        reference["digest"] for reference in evidence.get("artifact_refs", [])
                    ],
                }
            dependency_receipts.append(
                {
                    "task_id": dependency_id,
                    "submission": dependency.get("submission"),
                    "evidence": [compact_evidence[kind] for kind in sorted(compact_evidence)],
                }
            )
        last_verification = task["verification_history"][-1] if task["verification_history"] else None
        if last_verification is not None:
            last_verification = {
                "passed": last_verification["passed"],
                "checks": [
                    {
                        key: check.get(key)
                        for key in ("purpose", "passed", "phase", "evaluated_task_id", "error")
                        if key in check
                    }
                    for check in last_verification["checks"][-20:]
                ],
            }
        last_counterexample = next(
            (
                item for item in reversed(state.get("counterexamples", []))
                if item["task_id"] == task["id"]
            ),
            None,
        )
        return {
            "protocol": "camol-agent-turn/v1",
            **({"revision_context": state["revision"]} if state.get("revision") else {}),
            **({"state_model": runbook["state_model"]} if runbook["schema_version"] >= 5 else {}),
            "run": {
                "id": run_id,
                "objective": runbook["run"]["objective"],
                "plan_digest": state["plan_digest"],
                "completion": runbook["run"]["completion"],
            },
            "lease": {
                "task_id": assignment["task_id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "fence_digest": task["fence_digest"],
                "fence": task["lease_fence"],
            },
            "rules": runbook["rules"],
            "task": {
                "id": task["id"],
                "goal": task["goal"],
                "acceptance": task["acceptance"],
                "required_evidence": task["required_evidence"],
                "attempt": task["attempts"] + (1 if task["status"] == "leased" else 0),
                "remaining_steps": [step for step in task["steps"] if step["id"] not in completed],
            },
            "dependency_receipts": dependency_receipts,
            "routed_messages": [
                message for message in state["messages"] if message["to_task_id"] == task["id"]
            ][-10:],
            "last_checkpoint": task["checkpoints"][-1] if task["checkpoints"] else None,
            "last_verification": last_verification,
            "last_counterexample": last_counterexample,
            "token_budget": {
                "turn": runbook["run"]["token_policy"]["max_tokens_per_turn"],
                "checkpoint_reserve": runbook["run"]["token_policy"]["checkpoint_reserve"],
                "run_remaining": max(
                    0,
                    runbook["run"]["token_policy"]["max_total_tokens"] - accounted_tokens(state),
                ),
            },
            "return_contract": {
                "status": ["continue", "complete", "blocked"],
                "required": [
                    "status",
                    "checkpoint",
                    "completed_step_ids",
                    "input_tokens",
                    "output_tokens",
                    "evidence",
                ],
            },
        }

    def record_turn(
        self,
        run_id: str,
        assignment: Dict[str, str],
        result: Dict[str, Any],
        *,
        usage_invocation_id: Optional[str] = None,
    ) -> List[str]:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            fence_digest=assignment.get("fence_digest"),
        )
        completed_step_ids = result.get("completed_step_ids")
        if not isinstance(completed_step_ids, list) or any(
            not isinstance(step_id, str) for step_id in completed_step_ids
        ):
            raise ValueError("completed_step_ids must be an array of strings")
        valid_step_ids = {step["id"] for step in task["steps"]}
        if not set(completed_step_ids).issubset(valid_step_ids):
            raise ValueError("turn claimed an unknown task step")
        checkpoint = result.get("checkpoint")
        if not isinstance(checkpoint, str) or not checkpoint.strip():
            raise ValueError("every turn must leave a non-empty checkpoint")
        checkpoint = self.redactor.text(checkpoint)
        input_tokens = result.get("input_tokens")
        output_tokens = result.get("output_tokens")
        if type(input_tokens) is not int or input_tokens < 0:
            raise ValueError("input_tokens must be a non-negative integer")
        if type(output_tokens) is not int or output_tokens < 0:
            raise ValueError("output_tokens must be a non-negative integer")
        previous_steps = set(task["completed_step_ids"])
        newly_completed = len(set(completed_step_ids) - previous_steps)
        used_tokens = input_tokens + output_tokens
        current_charge = 0
        if usage_invocation_id is not None:
            receipts = _trusted_receipts(state["evidence"].values())
            receipt = receipts.get(usage_invocation_id)
            if (receipt is None or usage_invocation_id in state.get("accounted_usage_invocations", ())
                    or (receipt.run_id, receipt.task_id, receipt.agent_id, receipt.lease_id,
                        receipt.turn_number, receipt.input_tokens, receipt.output_tokens) !=
                    (run_id, task["id"], assignment["agent_id"], assignment["lease_id"],
                     task["turn_count"] + 1, input_tokens, output_tokens)):
                raise StateTransitionError("turn usage must bind one unconsumed provider receipt")
            current_charge = receipt.token_charge
        policy = state["runbook"]["run"]["token_policy"]
        violations = []
        if used_tokens > policy["max_tokens_per_turn"]:
            violations.append("turn_token_budget_exceeded")
        if accounted_tokens(state) - current_charge + used_tokens > policy["max_total_tokens"]:
            violations.append("run_token_budget_exceeded")
        if task["turn_count"] + 1 > policy["max_turns_per_task"]:
            violations.append("task_turn_budget_exceeded")
        checkpoint_tokens_estimate = (len(checkpoint) + 3) // 4
        if checkpoint_tokens_estimate > policy["checkpoint_reserve"]:
            violations.append("checkpoint_reserve_exceeded")

        self._emit(
            run_id,
            "AGENT_TURN_RECORDED",
            {
                "task_id": task["id"],
                "agent_id": assignment["agent_id"],
                "lease_id": assignment["lease_id"],
                "status": result.get("status"),
                "checkpoint": checkpoint,
                "completed_step_ids": completed_step_ids,
                "newly_completed_steps": newly_completed,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "checkpoint_tokens_estimate": checkpoint_tokens_estimate,
                "budget_violations": violations,
                **({"usage_invocation_id": usage_invocation_id} if usage_invocation_id is not None else {}),
            },
            actor_id=assignment["agent_id"],
            expected_seq=state["last_seq"],
        )
        return violations

    def route_message(
        self,
        run_id: str,
        assignment: Dict[str, str],
        to_task_id: str,
        kind: str,
        body: str,
    ) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            fence_digest=assignment.get("fence_digest"),
        )
        if to_task_id not in state["tasks"]:
            raise ValueError("message target is not a declared task")
        if kind not in {"information", "question", "proposal", "warning"}:
            raise ValueError("unsupported routed message kind")
        if not isinstance(body, str) or not body.strip() or len(body) > 2000:
            raise ValueError("routed message body must contain 1-2000 characters")
        self._emit(
            run_id,
            "MESSAGE_ROUTED",
            {
                "message_id": str(uuid4()),
                "from_task_id": task["id"],
                "to_task_id": to_task_id,
                "kind": kind,
                "body": self.redactor.text(body),
            },
            actor_id=assignment["agent_id"],
            expected_seq=state["last_seq"],
        )

    def record_evidence(
        self,
        run_id: str,
        assignment: Dict[str, str],
        evidence: Dict[str, Any],
    ) -> str:
        """Ingest worker-reported evidence as unverified data."""
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be an object")
        unknown = sorted(set(evidence) - {"evidence_id", "kind", "data"})
        if unknown:
            raise ValueError("worker evidence has unknown fields: {}".format(", ".join(unknown)))
        return self._record_task_evidence(
            run_id,
            assignment,
            evidence_id=evidence.get("evidence_id"),
            kind=evidence.get("kind"),
            data=evidence.get("data"),
            epistemic_status="UNVERIFIED",
            producer="worker",
            artifact_refs=(),
        )

    def record_observed_evidence(
        self,
        run_id: str,
        assignment: Dict[str, str],
        *,
        kind: str,
        data: Dict[str, Any],
        epistemic_status: str,
        producer: str,
        artifact_refs: Any = (),
        evidence_id: Optional[str] = None,
    ) -> str:
        """Record evidence produced by a Camol-controlled observer boundary."""
        return self._record_task_evidence(
            run_id,
            assignment,
            evidence_id=evidence_id,
            kind=kind,
            data=data,
            epistemic_status=epistemic_status,
            producer=producer,
            artifact_refs=artifact_refs,
        )

    def _record_task_evidence(
        self,
        run_id: str,
        assignment: Dict[str, str],
        *,
        evidence_id: Optional[str],
        kind: str,
        data: Dict[str, Any],
        epistemic_status: str,
        producer: str,
        artifact_refs: Any,
    ) -> str:
        state, _ = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
            # Observed costs and retained outputs remain facts after expiry;
            # their exact lease must still be current. They cannot advance it.
            require_fresh=producer == "worker",
        )
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unknown evidence kind: {}".format(kind))
        if not isinstance(data, dict):
            raise ValueError("evidence data must be an object")
        evidence_id = evidence_id or str(uuid4())
        if evidence_id in state["evidence"]:
            raise ValueError("evidence id already exists: {}".format(evidence_id))
        record = EvidenceRecord.task(
            evidence_id=evidence_id,
            run_id=run_id,
            task_id=assignment["task_id"],
            agent_id=assignment["agent_id"],
            lease_id=assignment["lease_id"],
            fence_digest=assignment["fence_digest"],
            kind=kind,
            epistemic_status=epistemic_status,
            producer=producer,
            observed_at=self._now(),
            data=data,
            artifact_refs=tuple(artifact_refs),
            redactor=self.redactor,
        )
        self._emit(
            run_id,
            "EVIDENCE_RECORDED",
            record.to_dict(),
            actor_id=assignment["agent_id"],
            expected_seq=state["last_seq"],
        )
        return evidence_id

    def submit_task(
        self,
        run_id: str,
        assignment: Dict[str, str],
        summary: str,
    ) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            fence_digest=assignment.get("fence_digest"),
        )
        required_steps = {step["id"] for step in task["steps"]}
        if not required_steps.issubset(set(task["completed_step_ids"])):
            missing = sorted(required_steps - set(task["completed_step_ids"]))
            raise StateTransitionError("task is missing completed steps: {}".format(", ".join(missing)))
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("submission summary is required")
        self._emit(
            run_id,
            "TASK_SUBMITTED",
            {
                "task_id": task["id"],
                "lease_id": assignment["lease_id"],
                "summary": self.redactor.text(summary),
            },
            actor_id=assignment["agent_id"],
            expected_seq=state["last_seq"],
        )

    def record_verification(
        self,
        run_id: str,
        assignment: Dict[str, str],
        checks: List[Dict[str, Any]],
    ) -> bool:
        state, _ = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        passed = bool(checks) and all(check.get("passed") is True for check in checks)
        self._emit(
            run_id,
            "TASK_VERIFICATION_RECORDED",
            {
                "task_id": assignment["task_id"],
                "lease_id": assignment["lease_id"],
                "fence_digest": assignment["fence_digest"],
                "passed": passed,
                "checks": self.redactor.value(checks),
            },
            expected_seq=state["last_seq"],
        )
        return passed

    def succeed_task(self, run_id: str, assignment: Dict[str, str]) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        if not task["verification_history"] or task["verification_history"][-1]["passed"] is not True:
            raise StateTransitionError("the latest verification is not green")
        latest_verification = task["verification_history"][-1]
        if (
            latest_verification.get("lease_id") != assignment["lease_id"]
            or latest_verification.get("fence_digest") != assignment["fence_digest"]
        ):
            raise StateTransitionError("the latest verification belongs to another lease")
        task_candidates = {
            candidate_id for candidate_id, candidate in state.get("candidates", {}).items()
            if candidate["task_id"] == task["id"] and candidate["lease_id"] == task["lease_id"]
        }
        if task_candidates and not any(
            receipt["task_id"] == task["id"] and receipt["candidate_id"] in task_candidates
            for receipt in state.get("integrations", [])
        ):
            raise StateTransitionError("the candidate has no accepted integration receipt")
        present_kinds = {
            state["evidence"][evidence_id]["kind"]
            for evidence_id in task["evidence_ids"]
            if self._gate_evidence(state["evidence"][evidence_id])
        }
        missing = sorted(set(task["required_evidence"]) - present_kinds)
        if missing:
            raise StateTransitionError("task is missing required evidence: {}".format(", ".join(missing)))
        bundle = self._active_bundle(state, task)
        try:
            self._emit_many(
                run_id,
                [
                    ("TASK_SUCCEEDED", self.actor_id, {"task_id": task["id"]}),
                    (
                        "RESERVATION_RELEASED",
                        self.actor_id,
                        {"reservation_id": bundle.reservation.reservation_id, "reason": "task_succeeded"},
                    ),
                ],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed during task completion; retry") from error

    def retry_or_block(self, run_id: str, assignment: Dict[str, str], reason: str) -> str:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        bundle = self._active_bundle(state, task)
        if task["attempts"] >= task["max_attempts"]:
            records = [
                (
                    "TASK_BLOCKED",
                    self.actor_id,
                    {
                        "task_id": task["id"],
                        "blocker": {
                            "kind": "attempts_exhausted",
                            "detail": self.redactor.text(reason),
                        },
                    },
                ),
                (
                    "RESERVATION_RELEASED",
                    self.actor_id,
                    {"reservation_id": bundle.reservation.reservation_id, "reason": "task_blocked"},
                ),
            ]
            try:
                self._emit_many(run_id, records, expected_seq=state["last_seq"])
            except ConcurrentAppendError as error:
                raise StateTransitionError("run state changed while blocking task; retry") from error
            return "blocked"
        try:
            self._emit_many(
                run_id,
                [
                    (
                        "TASK_RETRY_SCHEDULED",
                        self.actor_id,
                        {"task_id": task["id"], "reason": self.redactor.text(reason)},
                    ),
                    (
                        "RESERVATION_RELEASED",
                        self.actor_id,
                        {"reservation_id": bundle.reservation.reservation_id, "reason": "task_retry"},
                    ),
                ],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed while scheduling retry; retry") from error
        return "retry"

    def block_task(
        self,
        run_id: str,
        assignment: Dict[str, str],
        blocker: Dict[str, Any],
    ) -> None:
        state, task = self._require_lease(
            run_id,
            assignment["task_id"],
            assignment["agent_id"],
            assignment["lease_id"],
            "running",
            "verifying",
            fence_digest=assignment.get("fence_digest"),
        )
        bundle = self._active_bundle(state, task)
        try:
            self._emit_many(
                run_id,
                [
                    (
                        "TASK_BLOCKED",
                        self.actor_id,
                        {"task_id": task["id"], "blocker": self.redactor.value(blocker)},
                    ),
                    (
                        "RESERVATION_RELEASED",
                        self.actor_id,
                        {"reservation_id": bundle.reservation.reservation_id, "reason": "task_blocked"},
                    ),
                ],
                expected_seq=state["last_seq"],
            )
        except ConcurrentAppendError as error:
            raise StateTransitionError("run state changed while blocking task; retry") from error

    def completion_report(self, run_id: str) -> Dict[str, Any]:
        state = self.state(run_id)
        tasks = list(state["tasks"].values())
        blockers = [task["id"] for task in tasks if task["status"] == "blocked"]
        incomplete = [task["id"] for task in tasks if task["status"] != "succeeded"]
        missing_evidence = {}
        failed_verification = []
        for task in tasks:
            present = {
                state["evidence"][item]["kind"]
                for item in task["evidence_ids"]
                if self._gate_evidence(state["evidence"][item])
            }
            missing = sorted(set(task["required_evidence"]) - present)
            if missing:
                missing_evidence[task["id"]] = missing
            if not task["verification_history"] or task["verification_history"][-1]["passed"] is not True:
                failed_verification.append(task["id"])
        conditions = {
            "all_tasks_succeeded": not incomplete,
            "all_required_evidence_present": not missing_evidence,
            "all_verifications_green": not failed_verification,
            "no_open_blockers": not blockers,
            "no_open_debug_cases": not any(
                debug_case["status"] not in {"verified", "eval_promoted"}
                for debug_case in state["debug_cases"].values()
            ),
        }
        required = state["runbook"]["run"]["completion"] if state["runbook"] else []
        return {
            "complete": bool(required) and all(conditions[name] for name in required),
            "conditions": conditions,
            "incomplete_tasks": incomplete,
            "blocked_tasks": blockers,
            "missing_evidence": missing_evidence,
            "failed_verification": failed_verification,
        }

    def maybe_finish(self, run_id: str) -> bool:
        state = self._require_status(run_id, "running")
        report = self.completion_report(run_id)
        if report["complete"]:
            if state["runbook"]["schema_version"] >= 5:
                for item in state["integrations"]:
                    require_integration_gate(state, IntegrationReceipt.from_dict(item))
                self._emit(run_id, "RUN_AWAITING_ACCEPTANCE", {"outcome_digest": acceptance_digest(state)}, expected_seq=state["last_seq"])
                return True
            self._emit(
                run_id,
                "RUN_COMPLETED",
                {
                    "verdict": "all declared completion conditions are satisfied",
                    "plan_digest": state["plan_digest"],
                    "total_tokens": state["total_tokens"],
                },
            )
            return True
        return False

    def block_run(self, run_id: str, reason: str, details: Dict[str, Any]) -> None:
        self._require_status(run_id, "running")
        self._emit(run_id, "RUN_BLOCKED", {"reason": reason, "details": details})

    def record_hillclimb(
        self,
        run_id: str,
        hillclimb_id: str,
        baseline: Dict[str, float],
        candidate: Dict[str, float],
        dimensions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self._require_status(run_id, "running")
        verdict = compare_vectors(baseline, candidate, dimensions)
        self._emit(
            run_id,
            "HILLCLIMB_RECORDED",
            {
                "hillclimb_id": hillclimb_id,
                "baseline": baseline,
                "candidate": candidate,
                "dimensions": dimensions,
                "verdict": verdict,
            },
        )
        return verdict

    def open_debug_case(
        self,
        run_id: str,
        case_id: str,
        observed_behavior: str,
        target_behavior: str,
        reproduction: List[str],
        required_evidence: List[str],
    ) -> None:
        from .debugger import Debugger
        state = self._require_status(run_id, "running")
        Debugger(self, run_id).open(
            case_id, observed_behavior, target_behavior, reproduction, required_evidence,
            target_authority="plan {} approved by {}".format(state["plan_digest"], state["approved_by"]),
        )
        return

    def record_debug_evidence(
        self,
        run_id: str,
        case_id: str,
        kind: str,
        data: Dict[str, Any],
        actor_id: str,
    ) -> str:
        from .debugger import Debugger
        return Debugger(self, run_id).observe(case_id, kind, data, producer=actor_id)

    def verify_debug_case(self, run_id: str, case_id: str, verdict: str) -> None:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "open":
            raise StateTransitionError("debug case is not open: {}".format(case_id))
        present = {
            state["evidence"][evidence_id]["kind"]
            for evidence_id in debug_case["evidence_ids"]
            if self._gate_evidence(state["evidence"][evidence_id])
        }
        missing = sorted(set(debug_case["required_evidence"]) - present)
        if missing:
            raise StateTransitionError(
                "debug case is missing required evidence: {}".format(", ".join(missing))
            )
        raise StateTransitionError(
            "use Debugger.verify after reproduction, localization and a bounded experiment"
        )

    def promote_eval(
        self,
        run_id: str,
        case_id: str,
        eval_id: str,
        definition: Dict[str, Any],
    ) -> None:
        from .debugger import Debugger
        Debugger(self, run_id).promote(case_id, eval_id, definition)
