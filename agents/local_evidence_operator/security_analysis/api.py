"""FastAPI application for the read-only security_analysis feature."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from sharedai.evidence.contracts import evidence_metadata
from sharedai.servicekit.auth import service_token_dependency

from security_analysis import __version__
from security_analysis.cache_correlation import (
    build_security_cache_report,
    format_security_cache_report,
    resolve_security_workspace,
)
from security_analysis.config import get_settings
from security_analysis.types import (
    CapabilitiesResponse,
    HealthResponse,
    SecurityAnalysisRequest,
    SecurityAnalysisResponse,
)

app = FastAPI(title="Security Analysis Feature", version=__version__)
require_service_token = service_token_dependency(
    "Security Analysis",
    lambda: get_settings().security.api_key,
)


def _workspace_input(request: SecurityAnalysisRequest) -> str:
    if request.workspace_path:
        return request.workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _response(
    content: str,
    report: dict[str, Any],
    *,
    mode: str,
    workspace: Path,
) -> SecurityAnalysisResponse:
    policy = {
        "writes_performed": False,
        "redaction": True,
        "public_breach_claims": False,
    }
    return SecurityAnalysisResponse(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4),
        metadata={
            **evidence_metadata(
                provider="security_analysis",
                mode=mode,
                workspace=workspace,
                report={**report, "policy": policy},
                extra={"exposure_count": len(report.get("exposures", []))},
            ),
            "exposure_count": len(report.get("exposures", [])),
            "policy": policy,
        },
    )


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/security/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/security/cache-correlation", dependencies=[Depends(require_service_token)])
def cache_correlation(request: SecurityAnalysisRequest) -> SecurityAnalysisResponse:
    request.mode = "cache_correlation"
    workspace = resolve_security_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return SecurityAnalysisResponse(success=False, error="security_workspace_not_found")
    report = build_security_cache_report(workspace, request.query)
    return _response(format_security_cache_report(report), report, mode=request.mode, workspace=workspace)


@app.post("/v1/security/tenant-isolation", dependencies=[Depends(require_service_token)])
def tenant_isolation(request: SecurityAnalysisRequest) -> SecurityAnalysisResponse:
    request.mode = "tenant_isolation"
    return cache_correlation(request)


@app.post("/v1/security/redaction-review", dependencies=[Depends(require_service_token)])
def redaction_review(request: SecurityAnalysisRequest) -> SecurityAnalysisResponse:
    request.mode = "redaction_review"
    return cache_correlation(request)
