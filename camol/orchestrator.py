"""Authoritative state transitions for a run over an N-worker pool."""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from .admission import AdmissionBundle
from .artifacts import ArtifactRef
from .evidence import EvidenceRecord
from .events import EVIDENCE_KINDS, new_event
from .hillclimb import agent_efficiency_vector, compare_vectors
from .readiness import CapacityReservation, LeaseFence, ReadinessDecision, WaitingReason
from .probes import Redactor
from .runbook import runbook_digest, validate_runbook
from .schema import parse_timestamp
from .state import project
from .store import ConcurrentAppendError, SQLiteEventStore


class StateTransitionError(RuntimeError):
    pass


class Orchestrator:
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
        return project(self.store.read(run_id))

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
            new_event(run_id, event_type, actor_id, payload)
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

    def approve_plan(self, run_id: str, approved_by: str, expected_digest: str) -> None:
        state = self._require_status(run_id, "draft")
        if state["plan_digest"] != expected_digest:
            raise StateTransitionError("the plan changed after review; re-open planning")
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        self._emit(
            run_id,
            "PLAN_APPROVED",
            {"approved_by": approved_by, "plan_digest": expected_digest},
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
        if require_fresh and not LeaseFence.from_dict(task["lease_fence"]).is_fresh(self._now()):
            raise StateTransitionError("the active lease fence is expired")
        return state, task

    def _active_bundle(self, state: Dict[str, Any], task: Dict[str, Any]) -> AdmissionBundle:
        bundle = self._bundle_for(state, task["id"], task["agent_id"])
        if bundle is None or bundle.digest() != task.get("admission_digest"):
            raise StateTransitionError("active lease admission bundle is missing or changed")
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
        if not fence.is_fresh(self._now()):
            raise StateTransitionError("cannot heartbeat an expired lease fence")
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
                compact_evidence[evidence["kind"]] = {
                    "evidence_id": evidence_id,
                    "kind": evidence["kind"],
                    "epistemic_status": evidence.get("epistemic_status", "UNVERIFIED"),
                    "data": evidence["data"],
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
        return {
            "protocol": "camol-agent-turn/v1",
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
            "token_budget": {
                "turn": runbook["run"]["token_policy"]["max_tokens_per_turn"],
                "checkpoint_reserve": runbook["run"]["token_policy"]["checkpoint_reserve"],
                "run_remaining": max(
                    0,
                    runbook["run"]["token_policy"]["max_total_tokens"] - state["total_tokens"],
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
        if not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("input_tokens must be a non-negative integer")
        if not isinstance(output_tokens, int) or output_tokens < 0:
            raise ValueError("output_tokens must be a non-negative integer")
        previous_steps = set(task["completed_step_ids"])
        newly_completed = len(set(completed_step_ids) - previous_steps)
        used_tokens = input_tokens + output_tokens
        policy = state["runbook"]["run"]["token_policy"]
        violations = []
        if used_tokens > policy["max_tokens_per_turn"]:
            violations.append("turn_token_budget_exceeded")
        if state["total_tokens"] + used_tokens > policy["max_total_tokens"]:
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
                debug_case["status"] == "open" for debug_case in state["debug_cases"].values()
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
        state = self._require_status(run_id, "running")
        if case_id in state["debug_cases"]:
            raise StateTransitionError("debug case already exists: {}".format(case_id))
        if not observed_behavior.strip() or not target_behavior.strip():
            raise ValueError("observed and target behavior are required")
        if not reproduction:
            raise ValueError("debug reproduction must not be empty")
        if any(not isinstance(step, str) or not step.strip() for step in reproduction):
            raise ValueError("debug reproduction steps must be non-empty strings")
        if any(
            not isinstance(kind, str) or not kind.strip() for kind in required_evidence
        ):
            raise ValueError("debug evidence kinds must be non-empty strings")
        unknown = sorted(set(required_evidence) - EVIDENCE_KINDS)
        if not required_evidence or unknown:
            raise ValueError("invalid debug evidence contract: {}".format(", ".join(unknown)))
        self._emit(
            run_id,
            "DEBUG_CASE_OPENED",
            {
                "case_id": case_id,
                "observed_behavior": observed_behavior,
                "target_behavior": target_behavior,
                "reproduction": reproduction,
                "required_evidence": required_evidence,
            },
        )

    def record_debug_evidence(
        self,
        run_id: str,
        case_id: str,
        kind: str,
        data: Dict[str, Any],
        actor_id: str,
    ) -> str:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "open":
            raise StateTransitionError("debug case is not open: {}".format(case_id))
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unknown evidence kind: {}".format(kind))
        if not isinstance(data, dict):
            raise ValueError("debug evidence data must be an object")
        evidence_id = str(uuid4())
        record = EvidenceRecord(
            evidence_id=evidence_id,
            run_id=run_id,
            task_id=None,
            debug_case_id=case_id,
            agent_id=None,
            lease_id=None,
            fence_digest=None,
            kind=kind,
            epistemic_status="HUMAN_REPORTED",
            producer=actor_id,
            observed_at=self._now(),
            data=self.redactor.value(data),
            artifact_refs=(),
        )
        self._emit(
            run_id,
            "EVIDENCE_RECORDED",
            record.to_dict(),
            actor_id=actor_id,
            expected_seq=state["last_seq"],
        )
        return evidence_id

    def verify_debug_case(self, run_id: str, case_id: str, verdict: str) -> None:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "open":
            raise StateTransitionError("debug case is not open: {}".format(case_id))
        present = {
            state["evidence"][evidence_id]["kind"]
            for evidence_id in debug_case["evidence_ids"]
        }
        missing = sorted(set(debug_case["required_evidence"]) - present)
        if missing:
            raise StateTransitionError(
                "debug case is missing required evidence: {}".format(", ".join(missing))
            )
        if not verdict.strip():
            raise ValueError("verification verdict is required")
        self._emit(
            run_id,
            "DEBUG_CASE_VERIFIED",
            {"case_id": case_id, "verdict": verdict},
        )

    def promote_eval(
        self,
        run_id: str,
        case_id: str,
        eval_id: str,
        definition: Dict[str, Any],
    ) -> None:
        state = self._require_status(run_id, "running")
        debug_case = state["debug_cases"].get(case_id)
        if not debug_case or debug_case["status"] != "verified":
            raise StateTransitionError("debug case must be verified before eval promotion")
        if eval_id in state["evals"]:
            raise StateTransitionError("eval already exists: {}".format(eval_id))
        if not definition.get("fixture") or not definition.get("oracle"):
            raise ValueError("eval definition requires fixture and oracle")
        self._emit(
            run_id,
            "EVAL_PROMOTED",
            {"case_id": case_id, "eval_id": eval_id, "definition": definition},
        )
