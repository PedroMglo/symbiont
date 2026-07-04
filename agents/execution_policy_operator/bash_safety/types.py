"""Pydantic contracts for the bash_safety feature."""

from __future__ import annotations

from typing import Any, Literal

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


BashSafetyMode = Literal["static_safety", "dry_run_plan", "portable_review"]


class BashSafetyRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    mode: BashSafetyMode = "static_safety"
    budget_tokens: int = 2000
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class BashSafetyResponse(BaseModel):
    content: str = ""
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CommandRiskRequest(BaseModel):
    command: str
    context_profile: str = "project_context"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandRiskResponse(BaseModel):
    success: bool = True
    command: str = ""
    action: str = ""
    risk_level: str = "deny"
    decision_hint: str = "deny"
    reason: str = ""
    tokens: list[str] = Field(default_factory=list)
    denied_markers: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    dry_run_required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "bash_safety"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "shell_static_analysis",
            "bash_safety",
            "command_risk_classification",
            "destructive_command_detection",
            "dry_run_planning",
            "portable_shell_review",
        ]
    )
    modes: list[BashSafetyMode] = Field(
        default_factory=lambda: ["static_safety", "dry_run_plan", "portable_review"]
    )
    description: str = (
        "Read-only operational safety analysis for shell scripts and command plans. "
        "Detects destructive patterns, unsafe expansion, portability risks and missing dry-run gates."
    )
    policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "executes_scripts": False,
            "mutates_inputs": False,
            "writes_final_outputs": False,
            "dangerous_actions_require_dry_run": True,
        }
    )
