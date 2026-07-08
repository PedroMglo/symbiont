"""HTTPS client for an external telemetry authority endpoint."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from orchestrator.resource_governor.telemetry.schemas import TelemetrySnapshot


class TelemetryAuthorityClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, *, timeout: float = 2.0) -> None:
        self.base_url = (base_url or os.environ.get("AI_TELEMETRY_AUTHORITY_URL") or "").rstrip("/")
        self.token = token or _configured_token()
        self.timeout = timeout
        self.verify: str | bool = _configured_verify()

    def snapshot(self) -> TelemetrySnapshot:
        if not self.base_url:
            raise RuntimeError("AI_TELEMETRY_AUTHORITY_URL is not configured")
        response = httpx.get(
            f"{self.base_url}/telemetry/snapshot",
            headers=self._headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        response.raise_for_status()
        return TelemetrySnapshot.model_validate(response.json())

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"X-Internal-API-Key": self.token}


def _configured_token() -> str:
    for env_name in ("AI_TELEMETRY_AUTHORITY_TOKEN", "AI_RESOURCE_GOVERNOR_TOKEN", "INTERNAL_API_KEY"):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    for env_name in ("AI_TELEMETRY_AUTHORITY_TOKEN_FILE", "AI_RESOURCE_GOVERNOR_TOKEN_FILE", "INTERNAL_API_KEY_FILE"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        try:
            token = Path(raw).read_text(encoding="utf-8").strip()
        except OSError:
            token = None
        if token:
            return token
    return ""


def _configured_verify() -> str | bool:
    for env_name in (
        "AI_TELEMETRY_AUTHORITY_CA_BUNDLE_PATH",
        "AI_TELEMETRY_AUTHORITY_CA_BUNDLE_FILE",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        raw = os.environ.get(env_name, "").strip()
        if raw and Path(raw).is_file():
            return raw
    return True
