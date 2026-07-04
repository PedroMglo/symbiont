"""FastAPI application for the Code Analysis feature."""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI
from sharedai.evidence.contracts import evidence_metadata
from sharedai.servicekit.auth import service_token_dependency

from code_analysis import __version__
from code_analysis.config import get_settings
from code_analysis.graph import get_graph_context
from code_analysis.git_regression import (
    build_git_regression_report,
    format_git_regression_report,
    resolve_git_workspace,
)
from code_analysis.repo import get_file_context, get_repo_context, get_repo_status
from code_analysis.sandbox_plan import build_git_regression_sandbox_plan
from code_analysis.types import (
    AnalyzeRequest,
    AnalyzeResponse,
    CapabilitiesResponse,
    GitRegressionRequest,
    GitRegressionResponse,
    HealthResponse,
    RepoStatusResponse,
)

app = FastAPI(title="Code Analysis Feature", version=__version__)
require_service_token = service_token_dependency(
    "Code Analysis",
    lambda: get_settings().security.api_key,
)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/code/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/code/analyze", dependencies=[Depends(require_service_token)])
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Analyze code based on a query."""
    graph_ctx = ""
    repo_ctx = ""
    file_ctx = get_file_context(request.query, request.budget_tokens)

    if request.include_graph:
        graph_ctx = get_graph_context(request.query, request.budget_tokens)

    if request.include_repo:
        repo_ctx = get_repo_context(request.query)

    return AnalyzeResponse(
        graph_context=graph_ctx,
        repo_context=repo_ctx,
        file_context=file_ctx,
        status="ok",
    )


def _workspace_input(request: GitRegressionRequest) -> str:
    if request.workspace_path:
        return request.workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


@app.post("/v1/code/git-regression", dependencies=[Depends(require_service_token)])
def git_regression(request: GitRegressionRequest) -> GitRegressionResponse:
    """Run read-only Git regression/code-forensics analysis."""
    workspace = resolve_git_workspace(
        _workspace_input(request),
        host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
    )
    if workspace is None:
        return GitRegressionResponse(success=False, error="git_regression_workspace_not_found")
    report = build_git_regression_report(workspace, request.query)
    sandbox_plan = build_git_regression_sandbox_plan(report)
    content = format_git_regression_report(report)
    return GitRegressionResponse(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4),
        metadata={
            **evidence_metadata(
                provider="code_analysis",
                mode="git_regression",
                workspace=workspace,
                report={**report, "analysis_mode": "read_only_git_regression"},
                extra={"likely_regression": report.get("likely_regression")},
            ),
            "operation": "git_regression",
            "likely_regression": report.get("likely_regression"),
            "sandbox_validation_plan": sandbox_plan,
        },
    )


@app.get("/v1/code/repo-status", dependencies=[Depends(require_service_token)])
def repo_status() -> RepoStatusResponse:
    """Get current repository status."""
    return get_repo_status()
