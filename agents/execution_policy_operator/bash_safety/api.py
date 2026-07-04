"""FastAPI application for the read-only bash_safety feature."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from sharedai.evidence.contracts import evidence_metadata
from sharedai.servicekit.auth import service_token_dependency

from bash_safety import __version__
from bash_safety.config import get_settings
from bash_safety.static_analysis import (
    build_bash_safety_report,
    classify_shell_command,
    format_bash_safety_report,
    resolve_bash_workspace,
)
from bash_safety.types import (
    BashSafetyRequest,
    BashSafetyResponse,
    CapabilitiesResponse,
    CommandRiskRequest,
    CommandRiskResponse,
    HealthResponse,
)

app = FastAPI(title="Bash Safety Feature", version=__version__)
require_service_token = service_token_dependency(
    "Bash Safety",
    lambda: get_settings().security.api_key,
)


def _workspace_input(request: BashSafetyRequest) -> str:
    if request.workspace_path:
        return request.workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _response(content: str, report: dict[str, Any], *, mode: str, workspace: Path) -> BashSafetyResponse:
    return BashSafetyResponse(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4),
        metadata=evidence_metadata(provider="bash_safety", mode=mode, workspace=workspace, report=report),
    )


def _bash_report(request: BashSafetyRequest) -> BashSafetyResponse:
    workspace = resolve_bash_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return BashSafetyResponse(success=False, error="bash_workspace_not_found")
    report = build_bash_safety_report(workspace, request.query)
    content = format_bash_safety_report(report)
    return _response(content, report, mode=request.mode, workspace=workspace)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/bash/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/bash/static-safety", dependencies=[Depends(require_service_token)])
def static_safety(request: BashSafetyRequest) -> BashSafetyResponse:
    request.mode = "static_safety"
    return _bash_report(request)


@app.post("/v1/bash/command-risk", dependencies=[Depends(require_service_token)])
def command_risk(request: CommandRiskRequest) -> CommandRiskResponse:
    classification = classify_shell_command(request.command, context_profile=request.context_profile)
    metadata = {
        **classification.get("metadata", {}),
        "request_metadata": request.metadata,
    }
    classification = {**classification, "metadata": metadata}
    return CommandRiskResponse(
        **classification,
    )


@app.post("/v1/bash/dry-run-plan", dependencies=[Depends(require_service_token)])
def dry_run_plan(request: BashSafetyRequest) -> BashSafetyResponse:
    request.mode = "dry_run_plan"
    return _bash_report(request)


@app.post("/v1/bash/portable-review", dependencies=[Depends(require_service_token)])
def portable_review(request: BashSafetyRequest) -> BashSafetyResponse:
    request.mode = "portable_review"
    return _bash_report(request)
