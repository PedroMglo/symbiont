"""Scheduler service backed by the Resource Governor lease authority."""

from __future__ import annotations

from uuid import uuid4

from orchestrator.resource_governor.schemas import LeaseRequest
from orchestrator.resource_governor.service import get_resource_governor_service
from orchestrator.scheduler.lanes import lane_definition
from orchestrator.scheduler.leases import RoutePlan, SchedulerLeaseRequest, SchedulerLeaseResponse
from orchestrator.scheduler.policy import scheduler_decision_from_lease
from orchestrator.scheduler.queue import SchedulerQueue


class SchedulerService:
    def __init__(self) -> None:
        self.queue = SchedulerQueue()

    def request_lease(self, request: SchedulerLeaseRequest) -> SchedulerLeaseResponse:
        lane = lane_definition(request.lane)
        governor = get_resource_governor_service()
        snapshot = governor.snapshot()
        if request.lane == "system_status_fast":
            return SchedulerLeaseResponse(
                decision="admit",
                reason="system_status_fast is deterministic and does not require LLM",
                pressure_level=str(snapshot.pressure_level),
                pressure_reasons=list(snapshot.pressure_reasons),
            )
        lease_request = LeaseRequest(
            idempotency_key=request.idempotency_key
            or f"scheduler:{request.owner}:{request.lane}:{request.session_id or uuid4().hex}",
            requester=request.owner,
            component=request.route_plan.route if request.route_plan else request.owner,
            lane=lane.governor_lane,
            lease_scope="batch" if lane.preemptible else "request",
            resource_class=lane.resource_class,
            capability=lane.capability,
            estimated_duration_seconds=request.resources.estimated_duration_s,
            requested_ttl_seconds=request.resources.estimated_duration_s,
            estimated_ram_mb=request.resources.estimated_ram_mb,
            estimated_vram_mb=request.resources.estimated_vram_mb,
            estimated_io_mb=request.resources.estimated_io_mb,
            preemptible=request.preemptible,
            quality_policy="degrade_allowed" if (request.route_plan and request.route_plan.can_degrade) else "preserve",
            estimated_quality_impact="medium" if request.preemptible else "low",
            session_id=request.session_id,
        )
        decision = governor.request_lease(lease_request)
        background_lane = request.lane in {"graphify_background", "embedding_batch", "prewarm_gpu", "io_write"}
        scheduler_decision = scheduler_decision_from_lease(decision, background_lane=background_lane)
        if scheduler_decision == "queue_background":
            self.queue.add(
                owner=request.owner,
                lane=request.lane,
                reason=decision.reason,
                retry_after_s=decision.retry_after_seconds,
            )
        return SchedulerLeaseResponse(
            decision=scheduler_decision,  # type: ignore[arg-type]
            lease_id=decision.lease_id,
            ttl_s=decision.ttl_seconds,
            retry_after_s=decision.retry_after_seconds,
            reason=decision.reason,
            pressure_level=str(snapshot.pressure_level),
            pressure_reasons=list(snapshot.pressure_reasons),
            limits=dict(decision.limits),
        )

    def admit_route(self, route_plan: RoutePlan) -> SchedulerLeaseResponse:
        resources = {
            "gpu": route_plan.requires_gpu,
            "estimated_duration_s": route_plan.max_latency_s,
        }
        if route_plan.requires_gpu:
            resources["estimated_vram_mb"] = 1024
        return self.request_lease(
            SchedulerLeaseRequest(
                owner=route_plan.owner,
                lane=route_plan.lane,
                resources=resources,
                preemptible=route_plan.lane not in {"interactive_chat", "audio_gpu"},
                session_id=route_plan.session_id,
                route_plan=route_plan,
            )
        )


_SERVICE: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SchedulerService()
    return _SERVICE
