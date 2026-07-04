"""Structured observability events — typed, immutable, serialisable.

Each event corresponds to a lifecycle point in the symbiont.
Events carry all contextual fields needed for analytics/tracing.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventName(str, Enum):
    # Application lifecycle
    APP_STARTED = "app_started"
    APP_SHUTDOWN = "app_shutdown"

    # Request lifecycle
    REQUEST_STARTED = "request_started"
    REQUEST_COMPLETED = "request_completed"
    REQUEST_ERROR = "request_error"

    # Router
    ROUTER_DECISION = "router_decision"
    MODEL_SELECTED = "model_selected"
    FALLBACK_USED = "fallback_used"

    # Backend health
    BACKEND_HEALTH_CHECK = "backend_health_check"
    BACKEND_HEALTH_CHANGED = "backend_health_changed"

    # LLM calls
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    LLM_CALL_ERROR = "llm_call_error"
    LLM_STREAM_STARTED = "llm_stream_started"
    LLM_STREAM_COMPLETED = "llm_stream_completed"

    # Model warmup
    MODEL_WARMUP_STARTED = "model_warmup_started"
    MODEL_WARMUP_COMPLETED = "model_warmup_completed"
    MODEL_COLD_START_DETECTED = "model_cold_start_detected"

    # Context building
    CONTEXT_BUILD_STARTED = "context_build_started"
    CONTEXT_BUILD_COMPLETED = "context_build_completed"

    # RAG
    RAG_RETRIEVAL_STARTED = "rag_retrieval_started"
    RAG_RETRIEVAL_COMPLETED = "rag_retrieval_completed"

    # Graph
    GRAPH_CONTEXT_STARTED = "graph_context_started"
    GRAPH_CONTEXT_COMPLETED = "graph_context_completed"

    # LangGraph tracing
    GRAPH_RUN_STARTED = "graph_run_started"
    GRAPH_RUN_COMPLETED = "graph_run_completed"
    GRAPH_NODE_STARTED = "graph_node_started"
    GRAPH_NODE_COMPLETED = "graph_node_completed"
    GRAPH_NODE_ERROR = "graph_node_error"

    # Tools
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"

    # Dynamic multi-agent routing (Sprint 2)
    DYNAMIC_ROUTE_DECISION = "dynamic_route_decision"
    DYNAMIC_ROUTE_FALLBACK = "dynamic_route_fallback"
    AGENT_INVOKED = "agent_invoked"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    SYNTHESIS_COMPLETED = "synthesis_completed"

    # Dashboard
    DASHBOARD_REQUEST = "dashboard_request"
    DASHBOARD_REQUEST_ERROR = "dashboard_request_error"

    # Resources
    RESOURCE_SNAPSHOT = "resource_snapshot"

    # Security (v1.3)
    INJECTION_DETECTED = "injection_detected"
    INJECTION_BLOCKED = "injection_blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    SECRET_FOUND = "secret_found"
    SANDBOX_VIOLATION = "sandbox_violation"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    AUDIT_LLM_CALL = "audit_llm_call"

    # Model-Agnostic Intelligence (v1.4)
    MODEL_ESCALATED = "model_escalated"
    PROMPT_COMPRESSED = "prompt_compressed"
    PROMPT_ADAPTED = "prompt_adapted"
    CAPABILITY_DETECTED = "capability_detected"

    # Gemilyni — Gemini Execution Layer (v2.1)
    GEMILYNI_ROUTING_DECISION = "gemilyni.routing_decision"
    GEMILYNI_EXTERNAL_CONTEXT_POLICY = "gemilyni.external_context_policy"
    GEMILYNI_BUNDLE_CREATED = "gemilyni.bundle_created"
    GEMILYNI_BUNDLE_FILE = "gemilyni.bundle_file"
    GEMILYNI_CONTEXT_BLOCK = "gemilyni.context_block"
    GEMILYNI_CONTAINER_CREATED = "gemilyni.container_created"
    GEMILYNI_CONTAINER_STARTED = "gemilyni.container_started"
    GEMILYNI_CONTAINER_STATS = "gemilyni.container_stats"
    GEMILYNI_GEMINI_INVOCATION_STARTED = "gemilyni.gemini_invocation_started"
    GEMILYNI_GEMINI_INVOCATION_FINISHED = "gemilyni.gemini_invocation_finished"
    GEMILYNI_WORKER_OUTPUT = "gemilyni.worker_output"
    GEMILYNI_EXECUTION_FINISHED = "gemilyni.execution_finished"
    GEMILYNI_POLICY_VIOLATION = "gemilyni.policy_violation"
    GEMILYNI_ERROR = "gemilyni.error"


@dataclass
class ObservabilityEvent:
    """Universal structured event for all observability sinks."""

    # === Identity & Temporal ===
    event: EventName
    level: EventLevel = EventLevel.INFO
    timestamp: float = field(default_factory=time.time)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    # === Correlation ===
    session_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    # === Entrypoint ===
    entrypoint: str | None = None  # cli | api | dashboard | alias | background

    # === Model / Backend / Routing ===
    model: str | None = None
    backend: str | None = None
    backend_type: str | None = None
    profile: str | None = None
    intent: str | None = None
    complexity: str | None = None

    selected_model: str | None = None
    selected_backend: str | None = None
    requested_model: str | None = None
    requested_profile: str | None = None

    fallback_used: bool = False
    fallback_reason: str | None = None

    # === Tokens ===
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    context_tokens: int | None = None
    usage_source: str | None = None  # backend | estimated | missing

    # === Latency (ms) ===
    total_latency_ms: float | None = None
    first_token_latency_ms: float | None = None
    model_load_latency_ms: float | None = None
    prompt_eval_latency_ms: float | None = None
    generation_latency_ms: float | None = None
    context_build_latency_ms: float | None = None
    rag_latency_ms: float | None = None
    graph_latency_ms: float | None = None
    llm_latency_ms: float | None = None
    router_latency_ms: float | None = None

    # === Performance ===
    tokens_per_second: float | None = None
    prompt_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None

    # === Features ===
    rag_used: bool = False
    graph_used: bool = False
    tools_used: list[str] = field(default_factory=list)
    agentic: bool = False
    iterations: int = 0

    # === Resources (attached by resource monitor) ===
    cpu_percent: float | None = None
    ram_used_mb: int | None = None
    gpu_util_percent: float | None = None
    vram_used_mb: int | None = None
    vram_peak_mb: int | None = None

    # === Cold start ===
    cold_start: bool = False

    # === Result ===
    success: bool = True
    error_type: str | None = None
    error_message_safe: str | None = None

    # === Streaming ===
    stream: bool = False
    chunks_count: int = 0

    # === Query info (privacy-aware) ===
    query_length: int | None = None
    response_length: int | None = None
    query_hash: str | None = None

    # === Extensible metadata ===
    metadata_json: str | None = None

    # === Ollama native timing (nanoseconds) ===
    ollama_total_duration: int | None = None
    ollama_load_duration: int | None = None
    ollama_prompt_eval_count: int | None = None
    ollama_prompt_eval_duration: int | None = None
    ollama_eval_count: int | None = None
    ollama_eval_duration: int | None = None

    def to_dict(self, *, exclude_none: bool = True) -> dict[str, Any]:
        """Serialise to dict, optionally excluding None values."""
        d = asdict(self)
        # Convert enums to string values
        d["event"] = self.event.value
        d["level"] = self.level.value
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None and v != [] and v != 0}
            # Always keep event, level, timestamp, request_id, success
            for key in ("event", "level", "timestamp", "request_id", "success"):
                if key not in d:
                    d[key] = getattr(self, key) if not isinstance(getattr(self, key), Enum) else getattr(self, key).value
        return d

    @staticmethod
    def hash_query(query: str) -> str:
        """SHA256 hash of query text (first 16 chars)."""
        return hashlib.sha256(query.encode()).hexdigest()[:16]


def new_request_id() -> str:
    """Generate a new request ID."""
    return uuid.uuid4().hex[:16]
