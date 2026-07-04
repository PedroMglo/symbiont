"""Dispatch layer — HTTP client infrastructure for external agent and feature services."""

from orchestrator.dispatch.agent_client import AgentClient
from orchestrator.dispatch.client import HTTPServiceClient
from orchestrator.dispatch.feature_client import FeatureClient
from orchestrator.dispatch.service_registry import ServiceRegistry
from orchestrator.dispatch.types import (
    AgentInvokeRequest,
    AgentInvokeResponse,
    FeatureQueryRequest,
    FeatureQueryResponse,
    ServiceEndpoint,
    ServiceHealth,
)

__all__ = [
    "AgentClient",
    "FeatureClient",
    "HTTPServiceClient",
    "ServiceRegistry",
    "ServiceEndpoint",
    "ServiceHealth",
    "AgentInvokeRequest",
    "AgentInvokeResponse",
    "FeatureQueryRequest",
    "FeatureQueryResponse",
]
