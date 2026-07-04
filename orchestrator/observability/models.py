"""Observability event dataclasses — the data model for all instrumentation."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class LLMUsage:
    """Token usage from an LLM call."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_source: str = "missing"  # "backend" | "estimated" | "missing"

    @classmethod
    def from_backend(cls, data: dict) -> LLMUsage:
        """Parse usage from an OpenAI-compatible response JSON."""
        if not data:
            return cls()
        return cls(
            prompt_tokens=data.get("prompt_tokens"),
            completion_tokens=data.get("completion_tokens"),
            total_tokens=data.get("total_tokens"),
            usage_source="backend",
        )

    @classmethod
    def estimated(cls, prompt_text: str, response_text: str) -> LLMUsage:
        """Estimate token counts from text lengths (chars ÷ 4)."""
        pt = len(prompt_text) // 4
        ct = len(response_text) // 4
        return cls(
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            usage_source="estimated",
        )


@dataclass(frozen=True)
class OllamaTiming:
    """Native timing data from Ollama /api/chat response."""

    total_duration: int | None = None  # nanoseconds
    load_duration: int | None = None  # nanoseconds
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None  # nanoseconds
    eval_count: int | None = None
    eval_duration: int | None = None  # nanoseconds

    @classmethod
    def from_response(cls, data: dict) -> "OllamaTiming":
        """Parse from Ollama native API response JSON."""
        if not data:
            return cls()
        return cls(
            total_duration=data.get("total_duration"),
            load_duration=data.get("load_duration"),
            prompt_eval_count=data.get("prompt_eval_count"),
            prompt_eval_duration=data.get("prompt_eval_duration"),
            eval_count=data.get("eval_count"),
            eval_duration=data.get("eval_duration"),
        )

    @property
    def load_duration_ms(self) -> float | None:
        return (self.load_duration / 1_000_000) if self.load_duration else None

    @property
    def prompt_eval_duration_ms(self) -> float | None:
        return (self.prompt_eval_duration / 1_000_000) if self.prompt_eval_duration else None

    @property
    def eval_duration_ms(self) -> float | None:
        return (self.eval_duration / 1_000_000) if self.eval_duration else None

    @property
    def prompt_tokens_per_second(self) -> float | None:
        if self.prompt_eval_count and self.prompt_eval_duration and self.prompt_eval_duration > 0:
            return self.prompt_eval_count / (self.prompt_eval_duration / 1_000_000_000)
        return None

    @property
    def generation_tokens_per_second(self) -> float | None:
        if self.eval_count and self.eval_duration and self.eval_duration > 0:
            return self.eval_count / (self.eval_duration / 1_000_000_000)
        return None

    @property
    def total_tokens_per_second(self) -> float | None:
        total_count = (self.prompt_eval_count or 0) + (self.eval_count or 0)
        if total_count and self.total_duration and self.total_duration > 0:
            return total_count / (self.total_duration / 1_000_000_000)
        return None


@dataclass
class LLMChatResult:
    """Complete result from an LLM chat call (text + metadata)."""

    text: str
    model: str
    backend: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: float = 0.0
    first_token_latency_ms: float | None = None
    finish_reason: str | None = None
    chunks_count: int = 0
    router_decision: "RouterDecision | None" = None
    # Ollama native timing (when available)
    ollama_timing: OllamaTiming = field(default_factory=OllamaTiming)
    cold_start: bool = False


@dataclass(frozen=True)
class RouterDecision:
    """Captures why a particular backend/model was selected."""

    requested_model: str = ""
    resolved_model: str = ""
    backend: str = ""
    backend_type: str = "openai_compatible"
    intent: str | None = None
    complexity: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    blocked_backends: tuple[str, ...] = ()
    privacy_mode: bool = False
    decision_reason: str = "direct_match"
    # Performance routing metadata
    latency_reason: str | None = None  # e.g. "prefer_warm", "p95_threshold"
    profile_key: str | None = None  # "fast", "default", "code", "deep"


@dataclass
class MetricsEvent:
    """A single observability event — one per LLM call (sync or stream)."""

    # Identity
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    session_id: str | None = None

    # Temporal (pre-computed for fast GROUP BY)
    date: str = ""
    hour: int = 0
    weekday: int = 0

    # Entrypoint
    entrypoint: str = "api"  # "api" | "cli" | "alias" | "stream"

    # Model / Backend
    model: str = ""
    backend: str = ""
    router_decision: RouterDecision = field(default_factory=RouterDecision)

    # Token usage
    usage: LLMUsage = field(default_factory=LLMUsage)

    # Streaming
    stream: bool = False
    chunks_count: int = 0

    # Timing — granular breakdown
    latency_ms: float = 0.0
    first_token_latency_ms: float | None = None
    context_latency_ms: float | None = None
    llm_latency_ms: float = 0.0
    router_latency_ms: float | None = None
    context_build_latency_ms: float | None = None
    model_load_latency_ms: float | None = None
    prompt_eval_latency_ms: float | None = None
    generation_latency_ms: float | None = None
    total_latency_ms: float | None = None

    # Performance metrics
    cold_start: bool = False
    prompt_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None
    total_tokens_per_second: float | None = None

    # Ollama native timing (nanoseconds, raw from Ollama)
    ollama_total_duration: int | None = None
    ollama_load_duration: int | None = None
    ollama_prompt_eval_count: int | None = None
    ollama_prompt_eval_duration: int | None = None
    ollama_eval_count: int | None = None
    ollama_eval_duration: int | None = None

    # Profile used
    profile_key: str | None = None

    # Content metadata (privacy-safe)
    query_length: int = 0
    response_length: int = 0
    query_hash: str = ""

    # Context
    rag_used: bool = False
    graph_used: bool = False
    tools_used: tuple[str, ...] = ()
    agentic: bool = False
    iterations: int = 0

    # Result
    success: bool = True
    error_type: str | None = None
    error_message: str | None = None

    # Optional previews (only if configured)
    prompt_preview: str | None = None
    response_preview: str | None = None

    def __post_init__(self):
        """Fill temporal fields from timestamp."""
        if not self.date:
            dt = datetime.fromtimestamp(self.timestamp, tz=timezone.utc)
            self.date = dt.strftime("%Y-%m-%d")
            self.hour = dt.hour
            self.weekday = dt.weekday()

    @staticmethod
    def hash_query(query: str) -> str:
        """SHA-256 truncated to 16 chars — privacy-safe identifier."""
        return hashlib.sha256(query.encode()).hexdigest()[:16]
