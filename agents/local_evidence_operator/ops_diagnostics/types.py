"""Pydantic contracts for the ops_diagnostics feature."""

from __future__ import annotations

from typing import Any, Literal

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


OpsDiagnosticsMode = Literal["log_performance", "incident_timeline", "compose_diagnostics", "diagnose"]


class OpsDiagnosticsRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    mode: OpsDiagnosticsMode = "diagnose"
    budget_tokens: int = 2000
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class OpsDiagnosticsResponse(BaseModel):
    content: str = ""
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "ops_diagnostics"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "log_performance",
            "incident_timeline",
            "compose_diagnostics",
            "read_only_ops_evidence",
        ]
    )
    modes: list[OpsDiagnosticsMode] = Field(
        default_factory=lambda: ["log_performance", "incident_timeline", "compose_diagnostics", "diagnose"]
    )
    description: str = (
        "Read-only operational diagnostics for compressed logs, incident timelines, "
        "Docker Compose topology, env drift and readiness risk."
    )
    policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "executes_scripts": False,
            "mutates_inputs": False,
            "writes_final_outputs": False,
            "decompresses_to_disk": False,
        }
    )
