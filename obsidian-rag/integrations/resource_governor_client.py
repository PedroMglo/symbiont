"""Resource Governor client helpers for RAG background work."""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

from sharedai.system.resource_governor import ResourceGovernorClient
from sharedai.system.resource_governor.schemas import (
    Capability,
    Lane,
    LeaseDecision,
    LeaseRequest,
    LeaseScope,
    QualityImpact,
    QualityPolicy,
    ResourceClass,
)


@dataclass
class LeaseHandle:
    client: ResourceGovernorClient
    request: LeaseRequest
    decision: LeaseDecision

    @property
    def granted(self) -> bool:
        return self.decision.granted

    def heartbeat(self) -> None:
        if self.decision.lease_id:
            self.client.heartbeat(
                self.decision.lease_id,
                owner=self.request.requester,
                request_id=self.request.request_id,
            )

    def release(self) -> None:
        if self.decision.lease_id:
            self.client.release(self.decision.lease_id)


def request_lease(
    *,
    component: str,
    lane: Lane | str,
    lease_scope: LeaseScope | str,
    resource_class: ResourceClass | str,
    capability: Capability | str,
    request_id: str | None = None,
    session_id: str | None = None,
    estimated_duration_seconds: int | None = None,
    estimated_ram_mb: int | None = None,
    estimated_vram_mb: int | None = None,
    estimated_io_mb: int | None = None,
    preemptible: bool = True,
    quality_policy: QualityPolicy | str = QualityPolicy.PRESERVE,
    estimated_quality_impact: QualityImpact | str = QualityImpact.LOW,
    idempotency_suffix: str | None = None,
) -> LeaseHandle:
    req_id = request_id or f"rag_{uuid4().hex[:16]}"
    suffix = idempotency_suffix or uuid4().hex
    request = LeaseRequest(
        idempotency_key=f"obsidian-rag:{component}:{capability}:{req_id}:{suffix}",
        requester="obsidian-rag",
        component=component,
        lane=lane,
        lease_scope=lease_scope,
        resource_class=resource_class,
        capability=capability,
        estimated_duration_seconds=estimated_duration_seconds,
        estimated_ram_mb=estimated_ram_mb,
        estimated_vram_mb=estimated_vram_mb,
        estimated_io_mb=estimated_io_mb,
        preemptible=preemptible,
        quality_policy=quality_policy,
        estimated_quality_impact=estimated_quality_impact,
        request_id=req_id,
        session_id=session_id,
    )
    client = ResourceGovernorClient(
        base_url=os.environ.get("AI_RESOURCE_GOVERNOR_URL"),
        token=os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN"),
    )
    return LeaseHandle(client=client, request=request, decision=client.request_lease(request))
