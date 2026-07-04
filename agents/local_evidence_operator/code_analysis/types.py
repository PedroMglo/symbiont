"""Data types for the Code Analysis feature."""

from __future__ import annotations

from typing import Any

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    query: str
    include_graph: bool = True
    include_repo: bool = True
    budget_tokens: int = 2000


class AnalyzeResponse(BaseModel):
    graph_context: str = ""
    repo_context: str = ""
    file_context: str = ""
    status: str = "ok"


class RepoStatusResponse(BaseModel):
    branch: str = ""
    modified_files: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    ahead: int = 0
    behind: int = 0


class GitRegressionRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    budget_tokens: int = 2000
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class GitRegressionResponse(BaseModel):
    content: str = ""
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "code_analysis"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "code_analysis",
            "dependency_graph",
            "repo_structure",
            "git_state",
            "git_regression",
            "code_forensics",
            "diff_reasoning",
            "test_planning",
            "workspace_sandbox_validation_plan",
        ]
    )
    description: str = (
        "Analyzes code architecture, dependency graphs, repository structure, "
        "and git state. Provides insights about code organization."
    )
