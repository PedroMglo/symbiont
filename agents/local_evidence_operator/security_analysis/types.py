"""Pydantic contracts for the security_analysis feature."""

from __future__ import annotations

from typing import Any, Literal

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


SecurityAnalysisMode = Literal["cache_correlation", "redaction_review", "tenant_isolation"]


class SecurityAnalysisRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    mode: SecurityAnalysisMode = "cache_correlation"
    budget_tokens: int = 2000
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class SecurityAnalysisResponse(BaseModel):
    content: str = ""
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "security_analysis"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "cache_correlation",
            "tenant_isolation",
            "redaction",
            "local_security_evidence",
        ]
    )
    modes: list[SecurityAnalysisMode] = Field(
        default_factory=lambda: ["cache_correlation", "redaction_review", "tenant_isolation"]
    )
    description: str = (
        "Read-only security evidence analysis with redaction for cache namespace, "
        "tenant/account isolation and local trace/log correlation."
    )
    policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "mutates_inputs": False,
            "writes_final_outputs": False,
            "redacts_sensitive_values": True,
            "public_breach_claims": False,
        }
    )
