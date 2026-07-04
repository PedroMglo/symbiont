"""Small HTTP client for the Resource Governor internal API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from orchestrator.resource_governor.constants import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
)
from orchestrator.resource_governor.schemas import (
    DecisionType,
    LeaseDecision,
    LeaseDecisionKind,
    LeaseRequest,
    UserImpact,
)


class ResourceGovernorClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 10.0,
        fallback_enabled: bool = False,
    ) -> None:
        self.base_url = (base_url or os.environ.get("AI_RESOURCE_GOVERNOR_URL") or "").rstrip("/")
        self.token = token or _configured_token()
        self.timeout = timeout
        self.fallback_enabled = fallback_enabled

    def request_lease(self, request: LeaseRequest) -> LeaseDecision:
        if not self.base_url:
            if self.fallback_enabled:
                return self._fallback_decision()
            raise RuntimeError("AI_RESOURCE_GOVERNOR_URL is not configured")
        response = httpx.post(
            f"{self.base_url}/resources/leases",
            headers=self._headers(),
            json=request.model_dump(mode="json"),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return LeaseDecision.model_validate(response.json())

    def heartbeat(self, lease_id: str, *, owner: str | None = None, request_id: str | None = None) -> dict[str, Any]:
        if not self.base_url:
            if self.fallback_enabled:
                return {"status": "ok", "lease_id": lease_id}
            raise RuntimeError("AI_RESOURCE_GOVERNOR_URL is not configured")
        response = httpx.post(
            f"{self.base_url}/resources/leases/{lease_id}/heartbeat",
            headers=self._headers(),
            json={"lease_id": lease_id, "owner": owner, "request_id": request_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return dict(response.json())

    def release(self, lease_id: str) -> dict[str, Any]:
        if not self.base_url:
            if self.fallback_enabled:
                return {"status": "released", "lease_id": lease_id}
            raise RuntimeError("AI_RESOURCE_GOVERNOR_URL is not configured")
        response = httpx.delete(
            f"{self.base_url}/resources/leases/{lease_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return dict(response.json())

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Internal-API-Key"] = self.token
        return headers

    @staticmethod
    def _fallback_decision() -> LeaseDecision:
        return LeaseDecision(
            decision=LeaseDecisionKind.GRANTED_WITH_LIMITS,
            decision_type=DecisionType.SOFT_ADVICE,
            ttl_seconds=DEFAULT_LEASE_TTL_SECONDS,
            heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            limits={"fallback": True},
            reason="Resource Governor URL not configured; local fallback lease granted",
            expected_user_impact=UserImpact.NONE,
        )


def _configured_token() -> str:
    token = os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN", "").strip()
    if token:
        return token
    for env_name in ("AI_RESOURCE_GOVERNOR_TOKEN_FILE", "INTERNAL_API_KEY_FILE", "ORC_INTERNAL_API_KEY_FILE"):
        file_path = os.environ.get(env_name)
        if not file_path:
            continue
        try:
            token = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
    return os.environ.get("INTERNAL_API_KEY", "").strip() or os.environ.get("ORC_INTERNAL_API_KEY", "").strip()
