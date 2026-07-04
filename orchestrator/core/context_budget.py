"""Context budget system — limits context injection per task profile."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.config import ContextBudgetConfig, get_settings
from orchestrator.types import ContextBlock

if TYPE_CHECKING:
    from orchestrator.llm.capabilities import ModelCapabilities

log = logging.getLogger(__name__)

# Keywords that trigger system snapshot in "auto" mode
_SYSTEM_KEYWORDS = frozenset({
    "memory", "ram", "gpu", "vram", "cpu", "disk", "storage",
    "process", "temperature", "load", "system", "hardware",
    "memória", "disco", "processador", "sistema", "temperatura",
    "nvidia", "gpu", "swap", "uptime",
})


@dataclass
class ContextBudget:
    """Resolved context budget for a specific profile."""

    key: str  # "fast", "default", "code", "deep"
    max_context_tokens: int
    rag_top_k: int
    graph_enabled: bool
    system_snapshot_enabled: str  # "true", "false", "auto"

    @classmethod
    def from_config(cls, key: str, cfg: ContextBudgetConfig) -> "ContextBudget":
        return cls(
            key=key,
            max_context_tokens=cfg.max_context_tokens,
            rag_top_k=cfg.rag_top_k,
            graph_enabled=cfg.graph_enabled,
            system_snapshot_enabled=cfg.system_snapshot_enabled,
        )

    def should_use_system_snapshot(self, query: str) -> bool:
        """Determine if system snapshot should be injected based on mode and query."""
        if self.system_snapshot_enabled == "true":
            return True
        if self.system_snapshot_enabled == "false":
            return False
        # "auto" mode: check if query mentions system-related keywords
        words = set(query.lower().split())
        return bool(words & _SYSTEM_KEYWORDS)

    def should_use_graph(self) -> bool:
        """Whether graph context should be included."""
        return self.graph_enabled


def resolve_budget(key: str) -> ContextBudget:
    """Resolve a profile key to its context budget.

    Falls back to the configured 'default' profile if key not found.
    """
    cfg = get_settings()
    budgets = cfg.context_budgets

    if key in budgets:
        return ContextBudget.from_config(key, budgets[key])

    if "default" in budgets:
        return ContextBudget.from_config("default", budgets["default"])

    raise ValueError(
        f"Unknown context budget profile {key!r} and no 'default' profile configured. "
        "Add [context_budget.default] to config/orc/context.toml."
    )


def resolve_budget_with_caps(
    key: str, caps: "ModelCapabilities | None" = None
) -> ContextBudget:
    """Resolve budget, optionally scaling by model's actual context window.

    Scales token budget and RAG top_k proportionally to the model's context
    window. All scaling parameters come from [context_budget_scaling] config
    (reference window, max scale, and the minimum window required for graph).
    """
    base = resolve_budget(key)
    if caps is None:
        return base

    scaling = get_settings().context_budget_scaling
    scale = min(caps.context_window / scaling.reference_context_window, scaling.max_scale)
    return ContextBudget(
        key=base.key,
        max_context_tokens=int(base.max_context_tokens * scale),
        rag_top_k=max(1, int(base.rag_top_k * scale)),
        graph_enabled=base.graph_enabled and caps.context_window >= scaling.graph_min_context_window,
        system_snapshot_enabled=base.system_snapshot_enabled,
    )


def deduplicate_blocks(blocks: list[ContextBlock]) -> list[ContextBlock]:
    """Remove duplicate context blocks based on content hash.

    Preserves order (first occurrence kept).
    """
    seen_hashes: set[str] = set()
    unique: list[ContextBlock] = []

    for block in blocks:
        content_hash = hashlib.sha256(block.content.encode()).hexdigest()[:16]
        if content_hash not in seen_hashes:
            seen_hashes.add(content_hash)
            unique.append(block)
        else:
            log.debug("ContextBudget: deduplicating block from source=%s", block.source)

    return unique


def truncate_by_budget(
    blocks: list[ContextBlock], max_tokens: int
) -> list[ContextBlock]:
    """Truncate blocks to fit within token budget, preserving priority order."""
    result: list[ContextBlock] = []
    remaining = max_tokens

    for block in blocks:
        if block.token_estimate <= remaining:
            result.append(block)
            remaining -= block.token_estimate
        else:
            # Partial inclusion not supported — skip
            log.debug(
                "ContextBudget: skipping block source=%s (%d tokens, budget remaining=%d)",
                block.source,
                block.token_estimate,
                remaining,
            )
            break

    return result
