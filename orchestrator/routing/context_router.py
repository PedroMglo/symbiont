"""Context source routing — decides which providers to query per intent."""

from __future__ import annotations

import logging

from orchestrator.capabilities.context_routing import (
    context_sources_for_intent,
    required_context_sources_for_intent,
)
from orchestrator.types import Complexity, Intent

log = logging.getLogger(__name__)


class ConfigContextRouter:
    """Routes intent to context provider names."""

    def route(self, intent: Intent, complexity: Complexity) -> list[str]:
        sources = list(context_sources_for_intent(intent))
        log.debug("ContextRouter: %s×%s → %s", intent.value, complexity.value, sources)
        return sources


def get_required_tools_for_intent(intent: Intent) -> list[str]:
    """Return the primary (non-auxiliary) tools expected for a given intent.

    Used by the agentic coverage validator to detect premature final answers;
    the required subset is declarative manifest metadata.
    """
    return list(required_context_sources_for_intent(intent))
