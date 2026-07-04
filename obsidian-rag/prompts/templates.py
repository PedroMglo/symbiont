"""Prompt templates for the local AI assistant.

All prompts are centralized in the RAG model registry.
This module loads them via the registry and exposes them as module-level
constants for the prompt consumers.
"""

from __future__ import annotations

from registry import get_rag_prompt, get_rag_system_prompt

# =============================================================================
# All prompts loaded from centralized models.json
# =============================================================================

SYSTEM_GENERAL = get_rag_prompt("system_general")
ROUTER_SYSTEM = get_rag_system_prompt("router")
ROUTER_USER_TEMPLATE = get_rag_prompt("router_user_template")
RAG_CONTEXT_INSTRUCTION = get_rag_prompt("rag_context_instruction")
GRAPH_CONTEXT_INSTRUCTION = get_rag_prompt("graph_context_instruction")
COMBINED_CONTEXT_INSTRUCTION = get_rag_prompt("combined_context_instruction")
FALLBACK_WEAK_CONTEXT = get_rag_prompt("fallback_weak_context")
CAG_CONTEXT_INSTRUCTION = get_rag_prompt("cag_context_instruction")


def get_context_instruction(sources_used: str) -> str:
    """Return the appropriate context instruction based on sources used."""
    parts: list[str] = []

    if "cag" in sources_used:
        parts.append(CAG_CONTEXT_INSTRUCTION)

    if "rag" in sources_used and "graph" in sources_used:
        parts.append(COMBINED_CONTEXT_INSTRUCTION)
    elif "graph" in sources_used:
        parts.append(GRAPH_CONTEXT_INSTRUCTION)
    elif "rag" in sources_used:
        parts.append(RAG_CONTEXT_INSTRUCTION)

    return "\n\n".join(parts)
