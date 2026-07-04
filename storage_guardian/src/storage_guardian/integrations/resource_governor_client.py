"""Resource Governor helpers for storage_guardian storage cycles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sharedai.system.resource_governor import ResourceGovernorClient
from sharedai.system.resource_governor.constants import DEFAULT_HEARTBEAT_INTERVAL_SECONDS
from sharedai.system.resource_governor.schemas import DecisionType, LeaseDecision, LeaseDecisionKind, LeaseRequest, UserImpact


@dataclass
class StorageGuardianLease:
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


def request_storage_lease(
    *,
    component: str,
    lease_scope: str,
    request_id: str | None = None,
    estimated_duration_seconds: int | None = None,
    estimated_io_mb: int | None = None,
    suffix: str | None = None,
) -> StorageGuardianLease:
    req_id = request_id or f"storage_guardian_{uuid4().hex[:16]}"
    request = LeaseRequest(
        idempotency_key=f"storage_guardian:{component}:{lease_scope}:{req_id}:{suffix or uuid4().hex}",
        requester="storage_guardian",
        component=component,
        lane="storage",
        lease_scope=lease_scope,
        resource_class="io_write",
        capability="storage_archive",
        estimated_duration_seconds=estimated_duration_seconds,
        estimated_io_mb=estimated_io_mb,
        preemptible=True,
        quality_policy="preserve",
        estimated_quality_impact="none",
        request_id=req_id,
        session_id=None,
    )
    token = _resource_governor_token()
    if not os.environ.get("AI_RESOURCE_GOVERNOR_URL") and not token:
        return StorageGuardianLease(
            client=ResourceGovernorClient(fallback_enabled=True),
            request=request,
            decision=LeaseDecision(
                decision=LeaseDecisionKind.GRANTED_WITH_LIMITS,
                decision_type=DecisionType.SOFT_ADVICE,
                lease_id=f"local_lease_{uuid4().hex}",
                ttl_seconds=60,
                heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                limits={"workers": 1, "checkpoint_required": True},
                reason="Resource Governor not configured; storage_guardian using standalone local resource guard",
                effective_quality_policy="preserve",
                expected_user_impact=UserImpact.NONE,
            ),
        )
    client = ResourceGovernorClient(
        base_url=os.environ.get("AI_RESOURCE_GOVERNOR_URL"),
        token=token,
    )
    return StorageGuardianLease(client=client, request=request, decision=client.request_lease(request))


def _resource_governor_token() -> str:
    token = os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN", "").strip()
    if token:
        return token
    for env_name in (
        "AI_RESOURCE_GOVERNOR_TOKEN_FILE",
        "STORAGE_GUARDIAN_INTERNAL_TOKEN_FILE",
        "INTERNAL_API_KEY_FILE",
    ):
        file_path = os.environ.get(env_name)
        if not file_path:
            continue
        try:
            token = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
    return os.environ.get("INTERNAL_API_KEY", "").strip()
