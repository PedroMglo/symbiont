"""Pydantic contracts for the data_analysis feature."""

from __future__ import annotations

from typing import Any, Literal

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


DataAnalysisMode = Literal["profile", "drift", "sqlite_reconcile", "metric_reconcile"]


class DataAnalysisRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    mode: DataAnalysisMode = "profile"
    budget_tokens: int = 2000
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class DataAnalysisResponse(BaseModel):
    content: str = ""
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "data_analysis"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "data_profile",
            "data_quality",
            "schema_drift",
            "sqlite_reconcile",
            "metric_reconcile",
            "workspace_sandbox_validation_plan",
        ]
    )
    modes: list[DataAnalysisMode] = Field(
        default_factory=lambda: ["profile", "drift", "sqlite_reconcile", "metric_reconcile"]
    )
    description: str = (
        "Read-only local data analysis for tabular files, JSONL/NDJSON streams, "
        "compressed datasets, SQLite inspection, and generic metric reconciliation."
    )
    policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "writes_final_outputs": False,
            "mutates_inputs": False,
            "decompresses_to_disk": False,
            "sqlite_open_mode": "read_only",
        }
    )
