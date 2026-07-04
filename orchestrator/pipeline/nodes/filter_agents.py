"""Filter agents node — dead path elimination (v1.6).

Removes agents whose required context sources returned empty,
avoiding wasted computation on agents that cannot produce quality output.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.pipeline.state import SymbiontState

if TYPE_CHECKING:
    from orchestrator.config import IntelligentPipelineConfig

log = logging.getLogger(__name__)

_AGENT_CONTEXT_DEPS: dict[str, list[str]] = {
    "research": ["rag", "cag"],
    "code": ["repo", "graph"],
    "system": ["system", "config", "logs"],
    "personal": ["calendar", "email", "rss"],
}


def create_filter_agents_node(config: "IntelligentPipelineConfig"):
    """Factory that creates the dead-path elimination filter node."""

    def filter_agents_node(state: SymbiontState) -> dict:
        """Drop agents whose context dependencies are all empty."""
        if not config.dead_path_elimination_enabled:
            return {}

        selected = state.get("selected_agents", [])
        context_blocks = state.get("context_blocks", [])

        sources_with_content = {
            block.source for block in context_blocks if block.content.strip()
        }

        surviving: list[str] = []
        eliminated: list[str] = []

        for agent in selected:
            deps = _AGENT_CONTEXT_DEPS.get(agent, [])
            if not deps:
                surviving.append(agent)
                continue

            has_any = any(dep in sources_with_content for dep in deps)
            if has_any:
                surviving.append(agent)
            else:
                eliminated.append(agent)

        if not surviving and selected:
            surviving = [selected[0]]
            if selected[0] in eliminated:
                eliminated.remove(selected[0])

        if eliminated:
            log.info(
                "Dead path elimination: dropped %s (no context), keeping %s",
                eliminated, surviving,
            )

        return {
            "selected_agents": surviving,
            "eliminated_agents": eliminated,
            "execution_trace": [
                f"filter_agents:kept={len(surviving)},dropped={len(eliminated)}"
            ] if eliminated else [],
        }

    return filter_agents_node
