"""OpenTelemetry semantic attributes for ai-local runtime telemetry.

This module is the canonical place for cross-cutting ``ai.local.*`` attribute
names used by orchestrator telemetry. Owners outside the orchestrator may mirror
the documented names in their own code, but must not import this module at
runtime.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

OWNER_ORCHESTRATOR = "orchestrator"

ATTR_OWNER = "ai.local.owner"
ATTR_COMPONENT = "ai.local.component"
ATTR_TRACE_KIND = "ai.local.trace_kind"
ATTR_REQUEST_ID = "ai.local.request_id"
ATTR_SESSION_ID = "ai.local.session_id"
ATTR_TASK_ID = "ai.local.task_id"
ATTR_RUN_ID = "ai.local.run_id"
ATTR_CAPABILITY_ID = "ai.local.capability_id"
ATTR_POLICY_ACTION = "ai.local.policy_action"
ATTR_RISK_LEVEL = "ai.local.risk_level"
ATTR_RESOURCE_LEASE_ID = "ai.local.resource_lease_id"
ATTR_ENTRYPOINT = "ai.local.entrypoint"

ATTR_EVENT_NAME = "ai.local.event.name"
ATTR_EVENT_LEVEL = "ai.local.event.level"
ATTR_SUCCESS = "ai.local.success"
ATTR_ERROR_TYPE = "ai.local.error.type"

ATTR_MODEL_NAME = "ai.local.model.name"
ATTR_MODEL_BACKEND = "ai.local.model.backend"
ATTR_MODEL_BACKEND_TYPE = "ai.local.model.backend_type"
ATTR_MODEL_PROFILE = "ai.local.model.profile"
ATTR_REQUESTED_MODEL = "ai.local.model.requested"
ATTR_SELECTED_MODEL = "ai.local.model.selected"
ATTR_SELECTED_BACKEND = "ai.local.model.selected_backend"

ATTR_ROUTE_INTENT = "ai.local.route.intent"
ATTR_ROUTE_COMPLEXITY = "ai.local.route.complexity"
ATTR_FALLBACK_USED = "ai.local.route.fallback_used"
ATTR_FALLBACK_REASON = "ai.local.route.fallback_reason"

ATTR_RAG_USED = "ai.local.rag.used"
ATTR_GRAPH_USED = "ai.local.graph.used"
ATTR_TOOLS_USED = "ai.local.tools.used"

ATTR_GRAPH_RUN_ID = "ai.local.graph.run_id"
ATTR_GRAPH_NODE_NAME = "ai.local.graph.node_name"
ATTR_GRAPH_NODE_TYPE = "ai.local.graph.node_type"

_EVENT_FIELD_MAP = {
    ATTR_REQUEST_ID: "request_id",
    ATTR_SESSION_ID: "session_id",
    ATTR_ENTRYPOINT: "entrypoint",
    ATTR_MODEL_NAME: "model",
    ATTR_MODEL_BACKEND: "backend",
    ATTR_MODEL_BACKEND_TYPE: "backend_type",
    ATTR_MODEL_PROFILE: "profile",
    ATTR_REQUESTED_MODEL: "requested_model",
    ATTR_SELECTED_MODEL: "selected_model",
    ATTR_SELECTED_BACKEND: "selected_backend",
    ATTR_ROUTE_INTENT: "intent",
    ATTR_ROUTE_COMPLEXITY: "complexity",
    ATTR_FALLBACK_USED: "fallback_used",
    ATTR_FALLBACK_REASON: "fallback_reason",
    ATTR_RAG_USED: "rag_used",
    ATTR_GRAPH_USED: "graph_used",
    ATTR_TOOLS_USED: "tools_used",
    ATTR_SUCCESS: "success",
    ATTR_ERROR_TYPE: "error_type",
}


def _otel_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return value


def compact_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Return OTel-safe attributes without empty optional values."""
    compact: dict[str, Any] = {}
    for key, value in attributes.items():
        value = _otel_value(value)
        if value is None or value == "" or value == () or value == [] or value == {}:
            continue
        compact[key] = value
    return compact


def base_attributes(
    *,
    owner: str = OWNER_ORCHESTRATOR,
    component: str | None = None,
    trace_kind: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    capability_id: str | None = None,
    policy_action: str | None = None,
    risk_level: str | None = None,
    resource_lease_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build canonical ai-local attributes for spans and metrics."""
    return compact_attributes(
        {
            ATTR_OWNER: owner,
            ATTR_COMPONENT: component,
            ATTR_TRACE_KIND: trace_kind,
            ATTR_REQUEST_ID: request_id,
            ATTR_SESSION_ID: session_id,
            ATTR_RUN_ID: run_id,
            ATTR_TASK_ID: task_id,
            ATTR_CAPABILITY_ID: capability_id,
            ATTR_POLICY_ACTION: policy_action,
            ATTR_RISK_LEVEL: risk_level,
            ATTR_RESOURCE_LEASE_ID: resource_lease_id,
            **extra,
        }
    )


def event_attributes(event: Any, *, owner: str = OWNER_ORCHESTRATOR) -> dict[str, Any]:
    """Map an ObservabilityEvent-like object to canonical OTel attributes."""
    attrs: dict[str, Any] = {
        ATTR_OWNER: owner,
        ATTR_EVENT_NAME: getattr(getattr(event, "event", None), "value", getattr(event, "event", None)),
        ATTR_EVENT_LEVEL: getattr(getattr(event, "level", None), "value", getattr(event, "level", None)),
    }
    for attr_name, field_name in _EVENT_FIELD_MAP.items():
        if hasattr(event, field_name):
            attrs[attr_name] = getattr(event, field_name)
    return compact_attributes(attrs)
