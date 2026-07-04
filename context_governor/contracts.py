"""Contracts for the internal shared Context Governor.

The governor owns prompt pressure accounting and context packaging. Raw history
can stay in ledgers/session storage, but LLM calls should receive an explicit
ContextPackage with budgets, pressure and included operational context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ContextGovernorMode = Literal["off", "observe", "enforce"]
ContextGovernorDecision = Literal["allow", "warn", "trim", "block"]


@dataclass(frozen=True)
class ContextGovernorPolicy:
    """Config-backed policy for prompt and response budgets."""

    enabled: bool = True
    mode: ContextGovernorMode = "observe"
    default_context_window_tokens: int = 8192
    prompt_budget_ratio: float = 0.75
    reserved_response_ratio: float = 0.15
    minimum_reserved_response_tokens: int = 256
    max_reserved_response_tokens: int = 2048
    warning_pressure_threshold: float = 0.75
    block_pressure_threshold: float = 1.0
    model_context_windows: dict[str, int] = field(default_factory=dict)
    phase_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextBudget:
    """Resolved budget for one LLM prompt-construction phase."""

    phase: str
    model: str
    context_window_tokens: int
    max_prompt_tokens: int
    reserved_response_tokens: int
    prompt_budget_ratio: float
    warning_pressure_threshold: float
    block_pressure_threshold: float


@dataclass(frozen=True)
class ContextItem:
    """Structured operational memory candidate for a ContextPackage."""

    source: str
    content: str
    kind: str = "context"
    priority: int = 50
    required: bool = False
    token_estimate: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextRequest:
    """Input contract for building a governed LLM context package."""

    phase: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    items: list[ContextItem] = field(default_factory=list)
    context_window_tokens: int | None = None
    reserved_response_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextPackage:
    """Governed context prepared for one LLM call."""

    phase: str
    model: str
    mode: ContextGovernorMode
    decision: ContextGovernorDecision
    budget: ContextBudget
    messages: list[dict[str, Any]]
    included_items: list[ContextItem] = field(default_factory=list)
    dropped_items: list[ContextItem] = field(default_factory=list)
    prompt_tokens_estimate: int = 0
    original_prompt_tokens_estimate: int = 0
    context_pressure: float = 0.0
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_event_fields(self) -> dict[str, Any]:
        """Return metadata-only telemetry fields for trace events."""

        return {
            "phase": self.phase,
            "model": self.model,
            "mode": self.mode,
            "decision": self.decision,
            "context_window_tokens": self.budget.context_window_tokens,
            "max_prompt_tokens": self.budget.max_prompt_tokens,
            "reserved_response_tokens": self.budget.reserved_response_tokens,
            "prompt_tokens_estimate": self.prompt_tokens_estimate,
            "original_prompt_tokens_estimate": self.original_prompt_tokens_estimate,
            "context_pressure": round(self.context_pressure, 4),
            "included_item_count": len(self.included_items),
            "dropped_item_count": len(self.dropped_items),
            "included_sources": sorted({item.source for item in self.included_items}),
            "dropped_sources": sorted({item.source for item in self.dropped_items}),
            "warnings": list(self.warnings),
        }
