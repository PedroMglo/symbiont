"""Shared data types for the dispatch layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ServiceType(str, Enum):
    """Type of external service."""

    AGENT = "agent"        # LLM-using service (reasoning_and_response, audio_transcribe, etc.)
    FEATURE = "feature"    # Context provider (research, local_evidence, etc.)


class ServiceStatus(str, Enum):
    """Health status of a service."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ServiceEndpoint:
    """Configuration for a single external service."""

    name: str
    url: str                                  # Base URL (e.g. https://localhost:8081)
    service_type: ServiceType
    enabled: bool = True
    timeout_seconds: float = 10.0
    retries: int = 2
    capabilities: list[str] = field(default_factory=list)
    description: str = ""
    health_path: str = "/health"


@dataclass
class ServiceHealth:
    """Health status of a service at a point in time."""

    name: str
    status: ServiceStatus = ServiceStatus.UNKNOWN
    latency_ms: float = 0.0
    last_checked: float = 0.0
    error: str = ""
    version: str = ""


# ---------------------------------------------------------------------------
# Agent dispatch types
# ---------------------------------------------------------------------------

@dataclass
class AgentInvokeRequest:
    """Request to invoke an agent service."""

    query: str
    context: dict[str, Any] = field(default_factory=dict)
    budget_tokens: int | None = None
    timeout_seconds: float | None = None
    language_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AgentInvokeResponse:
    """Response from an agent service."""

    output: str
    success: bool = True
    confidence: float = 1.0
    tokens_used: int = 0
    latency_ms: float = 0.0
    agent_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_decision: dict[str, Any] | None = None
    error: str = ""


# ---------------------------------------------------------------------------
# Feature dispatch types
# ---------------------------------------------------------------------------

@dataclass
class FeatureQueryRequest:
    """Request to query a feature service (context provider)."""

    query: str
    budget_tokens: int | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureQueryResponse:
    """Response from a feature service."""

    content: str
    source: str = ""
    token_estimate: int = 0
    success: bool = True
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class FeatureInvokeResponse:
    """Raw structured response from a feature endpoint invoked through dispatch."""

    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    success: bool = True
    latency_ms: float = 0.0
    error: str = ""
