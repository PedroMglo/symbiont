"""Pattern Store — extracts successful routing patterns and injects them as few-shot examples.

Bridges the RoutingDecisionLog with the MetaSymbiont's system prompt,
providing recent successful patterns as context for improved routing decisions.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.routing.decision_log import RoutingDecisionLog

log = logging.getLogger(__name__)


class PatternStore:
    """Extracts and formats routing patterns for few-shot prompt injection.

    Connects to RoutingDecisionLog and formats successful patterns into
    a prompt section the MetaSymbiont can use.
    """

    def __init__(
        self,
        routing_log: RoutingDecisionLog,
        *,
        max_examples: int = 5,
        min_rating: int = 4,
        lookback_days: int = 30,
    ) -> None:
        self._log = routing_log
        self._max_examples = max_examples
        self._min_rating = min_rating
        self._lookback_days = lookback_days
        # Cache: refreshed on demand
        self._cached_prompt: str = ""
        self._cache_age: float = 0.0

    def get_few_shot_prompt(self) -> str:
        """Generate few-shot examples section for the MetaSymbiont prompt.

        Returns a formatted string to append to the routing system prompt,
        or empty string if no patterns are available.
        """
        import time

        # Cache for 5 minutes
        now = time.time()
        if self._cached_prompt and (now - self._cache_age) < 300:
            return self._cached_prompt

        patterns = self._log.successful_patterns(
            min_rating=self._min_rating,
            limit=self._max_examples,
            days=self._lookback_days,
        )

        if not patterns:
            self._cached_prompt = ""
            self._cache_age = now
            return ""

        self._cached_prompt = self._format_patterns(patterns)
        self._cache_age = now
        return self._cached_prompt

    def _format_patterns(self, patterns: list[dict[str, Any]]) -> str:
        """Format patterns into a few-shot prompt section."""
        lines = [
            "",
            "## Exemplos de routing bem-sucedido (padrões anteriores)",
            "",
        ]

        for i, p in enumerate(patterns, 1):
            agents = ", ".join(p.get("agents", [])) or "direct"
            lines.append(f"### Exemplo {i}")
            lines.append(f"Pergunta: {p['query'][:100]}")
            lines.append(f"Intenção: {p.get('intent', '?')} | Complexidade: {p.get('complexity', '?')}")
            lines.append(f"Decisão: {p['action']} → [{agents}]")
            if p.get("reasoning"):
                lines.append(f"Razão: {p['reasoning'][:80]}")
            lines.append("")

        return "\n".join(lines)

    def invalidate_cache(self) -> None:
        """Force refresh on next access."""
        self._cache_age = 0.0
        self._cached_prompt = ""
