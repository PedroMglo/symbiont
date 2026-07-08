"""Internal FastAPI routes for scheduler lanes and admission."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestrator.resource_governor.app_routes import require_internal_token
from orchestrator.scheduler.leases import RoutePlan, SchedulerLeaseRequest
from orchestrator.scheduler.service import get_scheduler_service

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.post("/leases", dependencies=[Depends(require_internal_token)])
def request_scheduler_lease(payload: SchedulerLeaseRequest):
    return get_scheduler_service().request_lease(payload)


@router.post("/admission", dependencies=[Depends(require_internal_token)])
def admit_route(payload: RoutePlan):
    return get_scheduler_service().admit_route(payload)


@router.get("/queue", dependencies=[Depends(require_internal_token)])
def scheduler_queue(limit: int = 50):
    return {"items": get_scheduler_service().queue.recent(limit=limit)}
