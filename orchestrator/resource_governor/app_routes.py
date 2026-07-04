"""Internal FastAPI routes for the Resource Governor."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from orchestrator.resource_governor.schemas import ActivityRequest, LeaseHeartbeat, LeaseRequest
from orchestrator.resource_governor.service import get_resource_governor_service

router = APIRouter(prefix="/resources", tags=["resources"])


def _configured_token() -> str:
    env_name = os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN_ENV", "AI_RESOURCE_GOVERNOR_TOKEN")
    token = os.environ.get(env_name) or os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN")
    if token:
        return token
    file_path = os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN_FILE")
    if file_path and Path(file_path).exists():
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    internal_file_path = os.environ.get("INTERNAL_API_KEY_FILE") or os.environ.get("ORC_INTERNAL_API_KEY_FILE")
    if internal_file_path and Path(internal_file_path).exists():
        try:
            return Path(internal_file_path).read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return os.environ.get("INTERNAL_API_KEY") or os.environ.get("ORC_INTERNAL_API_KEY", "")


async def require_internal_token(request: Request, authorization: str = Header(default="")) -> None:
    token = _configured_token()
    require = os.environ.get("AI_RESOURCE_GOVERNOR_REQUIRE_TOKEN", "true").lower() not in {"0", "false", "no", "off"}
    if not require and not token:
        return
    provided = request.headers.get("X-Internal-API-Key") or request.headers.get("X-API-Key") or ""
    if not provided and authorization.startswith("Bearer "):
        provided = authorization.removeprefix("Bearer ").strip()
    if not token or not provided or not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="Missing or invalid Resource Governor token")


@router.get("/snapshot", dependencies=[Depends(require_internal_token)])
def snapshot():
    return get_resource_governor_service().snapshot()


@router.post("/leases", dependencies=[Depends(require_internal_token)])
def request_lease(payload: LeaseRequest, request: Request):
    decision = get_resource_governor_service().request_lease(payload)
    _record_agentic_lease(request, payload=payload, decision=decision)
    return decision


@router.post("/leases/{lease_id}/heartbeat", dependencies=[Depends(require_internal_token)])
def heartbeat_lease(lease_id: str, payload: LeaseHeartbeat):
    if payload.lease_id != lease_id:
        raise HTTPException(status_code=400, detail="lease_id mismatch")
    if not get_resource_governor_service().heartbeat_lease(lease_id):
        raise HTTPException(status_code=404, detail="lease not found or expired")
    _renew_agentic_lease(lease_id)
    return {"status": "ok", "lease_id": lease_id}


@router.delete("/leases/{lease_id}", dependencies=[Depends(require_internal_token)])
def release_lease(lease_id: str):
    released = get_resource_governor_service().release_lease(lease_id)
    if released:
        _release_agentic_lease(lease_id)
    return {"status": "released" if released else "not_found", "lease_id": lease_id}


@router.post("/activity", dependencies=[Depends(require_internal_token)])
def register_activity(payload: ActivityRequest, request: Request):
    record = get_resource_governor_service().register_activity(payload)
    _record_agentic_activity(request, payload=payload, activity_id=record.activity_id)
    return record


@router.get("/effective-policy", dependencies=[Depends(require_internal_token)])
def effective_policy():
    return get_resource_governor_service().effective_policy()


@router.get("/metrics", dependencies=[Depends(require_internal_token)])
def governor_metrics():
    return get_resource_governor_service().metrics_snapshot()


@router.get("/decisions", dependencies=[Depends(require_internal_token)])
def governor_decisions(limit: int = 50):
    return {"items": get_resource_governor_service().recent_decisions(limit=limit)}


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "dict"):
        return model.dict()
    return {}


def _record_agentic_lease(request: Request, *, payload: LeaseRequest, decision: Any) -> None:
    task_id = request.headers.get("X-Task-ID") or None
    if not task_id:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        decision_data = _model_dump(decision)
        payload_data = _model_dump(payload)
        ttl = decision_data.get("ttl_seconds") or payload_data.get("requested_ttl_seconds")
        get_agentic_store().record_resource_lease(
            task_id=task_id,
            lease_id=decision_data.get("lease_id"),
            capability=str(payload_data.get("capability") or ""),
            decision=str(decision_data.get("decision") or ""),
            status="active" if decision_data.get("lease_id") else "not_granted",
            payload={"request": payload_data, "decision": decision_data},
            expires_at=time.time() + float(ttl) if ttl else None,
        )
    except Exception:
        pass


def _record_agentic_activity(request: Request, *, payload: ActivityRequest, activity_id: str) -> None:
    task_id = request.headers.get("X-Task-ID") or None
    if not task_id:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        payload_data = _model_dump(payload)
        get_agentic_store().record_resource_lease(
            task_id=task_id,
            lease_id=activity_id,
            capability=str(payload_data.get("capability") or ""),
            decision="GRANTED",
            status="active",
            payload={"activity": payload_data},
            expires_at=time.time() + float(payload_data.get("ttl_seconds") or 0),
        )
    except Exception:
        pass


def _renew_agentic_lease(lease_id: str) -> None:
    try:
        from orchestrator.agentic.store import get_agentic_store

        get_agentic_store().renew_resource_lease(lease_id)
    except Exception:
        pass


def _release_agentic_lease(lease_id: str) -> None:
    try:
        from orchestrator.agentic.store import get_agentic_store

        get_agentic_store().release_resource_lease(lease_id)
    except Exception:
        pass
