"""FastAPI application for the read-only data_analysis feature."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from sharedai.evidence.contracts import evidence_metadata
from sharedai.servicekit.auth import service_token_dependency

from data_analysis import __version__
from data_analysis.config import get_settings
from data_analysis.data_quality_drift import (
    build_data_quality_drift_report,
    format_data_quality_drift_report,
    resolve_data_workspace,
)
from data_analysis.sql_reconcile import (
    build_sql_reconcile_report,
    format_sql_reconcile_report,
    resolve_sql_workspace,
)
from data_analysis.sandbox_plan import build_data_sandbox_plan
from data_analysis.types import CapabilitiesResponse, DataAnalysisRequest, DataAnalysisResponse, HealthResponse

app = FastAPI(title="Data Analysis Feature", version=__version__)
require_service_token = service_token_dependency(
    "Data Analysis",
    lambda: get_settings().security.api_key,
)


def _workspace_input(request: DataAnalysisRequest) -> str:
    if request.workspace_path:
        return request.workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _response(content: str, report: dict[str, Any], *, mode: str, workspace: Path) -> DataAnalysisResponse:
    metadata = evidence_metadata(provider="data_analysis", mode=mode, workspace=workspace, report=report)
    metadata["sandbox_validation_plan"] = build_data_sandbox_plan(report, mode=mode, workspace=workspace)
    return DataAnalysisResponse(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4),
        metadata=metadata,
    )


def _data_report(request: DataAnalysisRequest) -> DataAnalysisResponse:
    workspace = resolve_data_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return DataAnalysisResponse(success=False, error="data_workspace_not_found")
    report = build_data_quality_drift_report(workspace, request.query)
    content = format_data_quality_drift_report(report)
    return _response(content, report, mode=request.mode, workspace=workspace)


def _sqlite_report(request: DataAnalysisRequest) -> DataAnalysisResponse:
    workspace = resolve_sql_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return DataAnalysisResponse(success=False, error="sql_workspace_not_found")
    report = build_sql_reconcile_report(workspace, request.query)
    content = format_sql_reconcile_report(report)
    return _response(content, report, mode=request.mode, workspace=workspace)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/data/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/data/profile", dependencies=[Depends(require_service_token)])
def profile(request: DataAnalysisRequest) -> DataAnalysisResponse:
    request.mode = "profile"
    return _data_report(request)


@app.post("/v1/data/drift", dependencies=[Depends(require_service_token)])
def drift(request: DataAnalysisRequest) -> DataAnalysisResponse:
    request.mode = "drift"
    return _data_report(request)


@app.post("/v1/data/sqlite/reconcile", dependencies=[Depends(require_service_token)])
def sqlite_reconcile(request: DataAnalysisRequest) -> DataAnalysisResponse:
    request.mode = "sqlite_reconcile"
    return _sqlite_report(request)


@app.post("/v1/data/metric/reconcile", dependencies=[Depends(require_service_token)])
def metric_reconcile(request: DataAnalysisRequest) -> DataAnalysisResponse:
    request.mode = "metric_reconcile"
    return _sqlite_report(request)
