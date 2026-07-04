"""API schemas — Pydantic models for request/response."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=65_536)
    stream: bool = Field(False, description="Stream response as SSE")
    agentic: bool | None = Field(None, description="Override agentic mode (None = use system config from models.json)")
    history: list[dict] | None = Field(None, description="Conversation history")
    session_id: str | None = Field(None, description="Session ID for conversation continuity")
    client_cwd: str | None = Field(None, max_length=1024, description="Client terminal current working directory")
    client_system: dict[str, Any] | None = Field(
        None,
        description="Read-only client host snapshot captured by the CLI alias",
    )
    client_files: list[dict[str, Any]] | None = Field(
        None,
        description="Read-only metadata/previews for local files referenced in the CLI prompt",
    )


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=100, description="ID of the original request")
    rating: int = Field(..., ge=1, le=5, description="Rating 1-5")
    feedback: str = Field("", max_length=500, description="Optional text feedback")


class ClassifyResponse(BaseModel):
    intent: str
    complexity: str


class QueryResponse(BaseModel):
    response: str
    model_used: str
    intent: str
    complexity: str
    sources_used: list[str]
    context_tokens: int
    latency_ms: float
    session_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    agentic_deliberation: dict[str, Any] | None = None


class BackendHealthSchema(BaseModel):
    """Per-backend health detail — v0.7."""

    name: str
    status: str  # healthy | unavailable | disabled | unknown
    url: str
    models_configured: list[str] = []
    models_detected: list[str] = []
    latency_ms: float | None = None
    last_error: str | None = None
    privacy_level: str = "local"
    priority: int = 10


class HealthResponse(BaseModel):
    status: str
    ollama: bool  # backward compat: True if any local backend healthy
    rag: bool
    providers: dict[str, bool]
    backends: list[BackendHealthSchema] = []  # v0.7 per-backend detail
    config_health: dict[str, Any] | None = None
    # GPU/resource metrics (Phase 6 observability enrichment)
    gpu_available: bool | None = None
    gpu_vram_used_mb: int | None = None
    gpu_vram_free_mb: int | None = None
    gpu_vram_total_mb: int | None = None
    gpu_utilization_pct: float | None = None
    swap_used_mb: int | None = None
    models_loaded: int | None = None


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: dict


class ToolListResponse(BaseModel):
    tools: list[ToolSchema]


# --- OpenAI-compatible schemas ---


class OpenAIChatMessage(BaseModel):
    role: str = Field(..., description="Role: system, user, assistant")
    content: str = Field(..., description="Message content")


class OpenAIChatRequest(BaseModel):
    model: str = Field(..., description="Model ID")
    messages: list[OpenAIChatMessage] = Field(..., min_length=1)
    stream: bool = Field(False)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


class OpenAIChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str = ""


class OpenAIChatChoice(BaseModel):
    index: int = 0
    message: OpenAIChatChoiceMessage
    finish_reason: str | None = "stop"


class OpenAIChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChatChoice]
    usage: OpenAIChatUsage = OpenAIChatUsage()


class OpenAIModelEntry(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "ai-local-symbiont"
