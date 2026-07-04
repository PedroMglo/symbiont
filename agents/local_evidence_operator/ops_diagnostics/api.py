"""FastAPI application for the read-only ops_diagnostics feature."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from sharedai.evidence.contracts import evidence_metadata
from sharedai.servicekit.auth import service_token_dependency

from ops_diagnostics import __version__
from ops_diagnostics.compose_analysis import (
    build_compose_analysis_report,
    format_compose_analysis_report,
    resolve_compose_workspace,
)
from ops_diagnostics.config import get_settings
from ops_diagnostics.incident_timeline import (
    build_incident_timeline_report,
    format_incident_timeline_report,
    resolve_incident_workspace,
)
from ops_diagnostics.log_performance import (
    build_log_performance_report,
    format_log_performance_report,
    resolve_log_workspace,
)
from ops_diagnostics.types import CapabilitiesResponse, HealthResponse, OpsDiagnosticsRequest, OpsDiagnosticsResponse

app = FastAPI(title="Ops Diagnostics Feature", version=__version__)
require_service_token = service_token_dependency(
    "Ops Diagnostics",
    lambda: get_settings().security.api_key,
)


def _workspace_input(request: OpsDiagnosticsRequest) -> str:
    if request.workspace_path:
        return request.workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _response(content: str, report: dict[str, Any], *, mode: str, workspace: Path) -> OpsDiagnosticsResponse:
    return OpsDiagnosticsResponse(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4),
        metadata=evidence_metadata(provider="ops_diagnostics", mode=mode, workspace=workspace, report=report),
    )


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/ops/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/ops/log-performance", dependencies=[Depends(require_service_token)])
def log_performance(request: OpsDiagnosticsRequest) -> OpsDiagnosticsResponse:
    request.mode = "log_performance"
    workspace = resolve_log_workspace(_workspace_input(request), host_home_prefix=os.environ.get("HOST_HOME_PREFIX"))
    if workspace is None:
        return OpsDiagnosticsResponse(success=False, error="log_workspace_not_found")
    limits = request.limits or {}
    report = build_log_performance_report(
        workspace,
        previous_start=limits.get("previous_start"),
        previous_end=limits.get("previous_end"),
        current_start=limits.get("current_start"),
        current_end=limits.get("current_end"),
        min_requests=int(limits.get("min_requests", 1000)),
        top_n=int(limits.get("top_n", 100)),
    )
    return _response(format_log_performance_report(report), report, mode=request.mode, workspace=workspace)


@app.post("/v1/ops/incident-timeline", dependencies=[Depends(require_service_token)])
def incident_timeline(request: OpsDiagnosticsRequest) -> OpsDiagnosticsResponse:
    request.mode = "incident_timeline"
    workspace = resolve_incident_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return OpsDiagnosticsResponse(success=False, error="incident_workspace_not_found")
    report = build_incident_timeline_report(workspace, request.query)
    return _response(format_incident_timeline_report(report), report, mode=request.mode, workspace=workspace)


@app.post("/v1/ops/compose-diagnostics", dependencies=[Depends(require_service_token)])
def compose_diagnostics(request: OpsDiagnosticsRequest) -> OpsDiagnosticsResponse:
    request.mode = "compose_diagnostics"
    workspace = resolve_compose_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return OpsDiagnosticsResponse(success=False, error="compose_workspace_not_found")
    report = build_compose_analysis_report(workspace, request.query)
    return _response(format_compose_analysis_report(report), report, mode=request.mode, workspace=workspace)


@app.post("/v1/ops/diagnose", dependencies=[Depends(require_service_token)])
def diagnose(request: OpsDiagnosticsRequest) -> OpsDiagnosticsResponse:
    mode = request.mode
    if mode == "log_performance":
        return log_performance(request)
    if mode == "incident_timeline":
        return incident_timeline(request)
    if mode == "compose_diagnostics":
        return compose_diagnostics(request)
    query = request.query.lower()
    if "compose" in query or "docker-compose" in query or "healthcheck" in query:
        request.mode = "compose_diagnostics"
        return compose_diagnostics(request)
    if "p95" in query or "latency" in query or "access log" in query:
        request.mode = "log_performance"
        return log_performance(request)
    request.mode = "incident_timeline"
    return incident_timeline(request)
