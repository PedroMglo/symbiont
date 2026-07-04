"""Data types for the reasoning_and_response agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sharedai.llm.contracts import LLMConfigOverride
from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse


class SourceResult(BaseModel):
    """A single owner output to be synthesized."""

    agent_name: str
    output: str
    confidence: float = 1.0


class ChatMessage(BaseModel):
    """Conversation message for direct response generation."""

    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str


class RespondRequest(BaseModel):
    """Request to produce a direct response from query, history, and context."""

    query: str
    history: list[ChatMessage] = Field(default_factory=list)
    context: str = ""
    budget_tokens: int | None = None
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class RespondResponse(BaseModel):
    """Direct response result."""

    response: str
    model_used: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent_decision: dict[str, Any] | None = None


class SubTask(BaseModel):
    """A decomposed subtask."""

    id: str
    objective: str
    assigned_agents: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    budget_tokens: int = 1000
    parallel_group: int = 0


class DecomposeRequest(BaseModel):
    """Request to decompose a complex query into subtasks."""

    query: str
    available_agents: list[str] = Field(default_factory=list)
    max_subtasks: int | None = None
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class DecomposeResponse(BaseModel):
    """Task decomposition result."""

    subtasks: list[SubTask]
    reasoning: str = ""
    output: str = ""


class SynthesizeRequest(BaseModel):
    """Request to synthesize multiple sources into one response."""

    query: str
    sources: list[SourceResult]
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class SynthesizeResponse(BaseModel):
    """Synthesized response result."""

    response: str
    sources_used: list[str] = Field(default_factory=list)


class PolishRequest(BaseModel):
    """Request to refine a draft response using critic feedback."""

    query: str
    draft: str
    issues: list[str] = Field(default_factory=list)
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class PolishResponse(BaseModel):
    """Polished response result."""

    response: str
    refinement_applied: bool = True


class CritiqueRequest(BaseModel):
    """Request to critique an owner output."""

    output: str
    original_query: str
    agent_name: str = "symbiont"
    risk_level: str | None = None
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class CritiqueResponse(BaseModel):
    """Quality critique result."""

    acceptable: bool
    confidence_score: float
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    response: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClassifyRequest(BaseModel):
    """Request to classify intent and select downstream owners."""

    query: str
    available_agents: list[str] = Field(default_factory=list)
    language_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_config: LLMConfigOverride | None = None


class ClassifyResponse(BaseModel):
    """Intent classification result."""

    agents: list[str] = Field(default_factory=list)
    reasoning: str = ""
    response: str = ""


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "reasoning_and_response"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "agent.reasoning_and_response.respond",
            "agent.reasoning_and_response.decompose",
            "agent.reasoning_and_response.synthesize",
            "agent.reasoning_and_response.critique",
            "agent.reasoning_and_response.classify",
            "direct_response",
            "planning",
            "decomposition",
            "synthesis",
            "multi_source_combination",
            "critique",
            "evaluation",
            "classification",
            "refinement",
        ]
    )
    description: str = (
        "Read-only reasoning and response provider family. "
        "Exposes direct response, decomposition, synthesis, critique, classification, "
        "and polish-compatible response contracts."
    )
