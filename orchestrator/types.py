"""Core shared types — enums, data containers, and protocols used across the symbiont.

This module centralizes types that were previously in symbiont.context.base.
All other modules should import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

from sharedai.llm.tokens import estimate_tokens  # noqa: F401 - re-exported

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Intent(str, Enum):
    """Classificação de intenção da query."""
    GENERAL = "general"
    LOCAL = "local"
    RESEARCH = "research"
    PERSONAL_CONTEXT = "personal_context"
    CODE = "code"
    SYSTEM = "system"
    GRAPH = "graph"
    AUDIO = "audio"
    LOCAL_AND_GRAPH = "local_and_graph"
    SYSTEM_AND_LOCAL = "system_and_local"
    CLARIFY = "clarify"


class Complexity(str, Enum):
    """Classificação de complexidade da query."""
    SIMPLE = "simple"
    NORMAL = "normal"
    COMPLEX = "complex"
    DEEP = "deep"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextBlock:
    """Bloco de contexto retornado por um provider (local ou remoto)."""
    source: str
    content: str
    token_estimate: int = 0
    metadata: dict = field(default_factory=dict)


def context_block_metadata(
    source: str,
    metadata: dict[str, Any] | None = None,
    *,
    read_only: bool | None = None,
    visibility: str | None = None,
    provider_status: str | None = None,
) -> dict[str, Any]:
    """Return contract metadata for a context block without overwriting provider data.

    Providers can keep their own metadata keys. The symbiont adds stable
    namespaced fields so downstream routing, fallback and observability can
    reason about context blocks consistently.
    """
    data: dict[str, Any] = dict(metadata or {})
    data.setdefault("contract_version", "context-block-v1")
    data.setdefault("context_source", source)
    if read_only is not None:
        data.setdefault("read_only", read_only)
    if visibility is not None:
        data.setdefault("visibility", visibility)
    if provider_status is not None:
        data.setdefault("provider_status", provider_status)
    return data


def make_context_block(
    *,
    source: str,
    content: str,
    token_estimate: int = 0,
    metadata: dict[str, Any] | None = None,
    read_only: bool | None = None,
    visibility: str | None = None,
    provider_status: str | None = None,
) -> ContextBlock:
    """Create a ContextBlock with the v1 metadata contract applied."""
    return ContextBlock(
        source=source,
        content=content,
        token_estimate=token_estimate,
        metadata=context_block_metadata(
            source,
            metadata,
            read_only=read_only,
            visibility=visibility,
            provider_status=provider_status,
        ),
    )


@dataclass(frozen=True)
class RoutingResult:
    """Resultado do intent + complexity classification."""
    intent: Intent
    complexity: Complexity
    method: str = "heuristic"


@dataclass(frozen=True)
class SymbiontResult:
    """Resultado final do Orquestrador."""
    response: str
    model_used: str
    intent: Intent
    complexity: Complexity
    sources_used: list[str] = field(default_factory=list)
    context_tokens: int = 0
    latency_ms: float = 0.0
    agentic: bool = False
    iterations: int = 0
    tools_invoked: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class IntentClassifier(Protocol):
    """Classifica a intenção da query."""
    def classify(self, query: str, *, history: list[dict] | None = None) -> Intent: ...


@runtime_checkable
class ComplexityClassifier(Protocol):
    """Classifica a complexidade da query."""
    def classify(self, query: str) -> Complexity: ...


@runtime_checkable
class ModelRouter(Protocol):
    """Seleciona o modelo ideal."""
    def select(self, intent: Intent, complexity: Complexity) -> str: ...


@runtime_checkable
class ContextRouter(Protocol):
    """Decide que fontes de contexto consultar."""
    def route(self, intent: Intent, complexity: Complexity) -> list[str]: ...


# ---------------------------------------------------------------------------
# Agent contract types
# ---------------------------------------------------------------------------

class AgentCapability(str, Enum):
    """Capabilities an agent can declare."""

    RESEARCH = "research"
    LOCAL_EVIDENCE = "local_evidence"
    SYSTEM_INFO = "system_info"
    PERSONAL = "personal"
    SYNTHESIS = "synthesis"
    CRITIQUE = "critique"
    PLANNING = "planning"


@dataclass(frozen=True)
class AgentDefinition:
    """Self-description of an agent for routing decisions."""

    name: str
    description: str
    capabilities: list[AgentCapability]
    when_to_use: str
    when_not_to_use: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    cost_estimate: str = "low"
    avg_latency_ms: float = 500.0

    def to_llm_description(self) -> str:
        caps = ", ".join(c.value for c in self.capabilities)
        return (
            f"**{self.name}**\n"
            f"  Description: {self.description}\n"
            f"  Capabilities: {caps}\n"
            f"  When to use: {self.when_to_use}\n"
            f"  When NOT to use: {self.when_not_to_use}\n"
            f"  Cost: {self.cost_estimate} | Avg latency: {self.avg_latency_ms:.0f}ms"
        )


@dataclass
class AgentTask:
    """A unit of work assigned to an agent."""

    task_id: str
    query: str
    context: dict[str, Any] = field(default_factory=dict)
    budget_tokens: int = 2000
    timeout_seconds: float = 10.0
    parent_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    peer_context: list["AgentResult"] = field(default_factory=list)


@dataclass
class AgentResult:
    """Result produced by an agent after executing a task."""

    task_id: str
    agent_name: str
    output: str
    success: bool = True
    confidence: float = 1.0
    tokens_used: int = 0
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    suggested_handoff: str | None = None

    @property
    def failed(self) -> bool:
        return not self.success
