"""Direct respond node — fast-path for trivial queries."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestrator.cache.semantic_cache import get_response_cache
from orchestrator.pipeline.nodes.conversation_shortcuts import (
    local_conversation_response,
    memory_read_response,
)
from orchestrator.pipeline.state import SymbiontState

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text

log = logging.getLogger(__name__)


def create_direct_respond_node(llm_adapter: Any):
    """Factory that creates the direct respond node with injected LLM."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    _SYSTEM = (
        _prompt("system.md")
    )

    def direct_respond_node(state: SymbiontState) -> dict:
        """Respond directly without context or agent invocation."""
        query = state["query"]
        original_query = state.get("original_query", query)
        language_context = state.get("language_context", {}) or {}
        response_language = str(language_context.get("response_language") or "same_as_user")
        language_instruction = (
            f"Original user message: {original_query}\n"
            f"User-facing response language: {response_language}\n"
            "The current query may be English-normalized for model work. "
            "Treat the original user message as the source of truth when wording matters."
        )
        system_prompt = f"{_SYSTEM}\n\n{language_instruction}"
        history = state.get("history", [])
        model = state.get("model_used", "")
        stream_mode = state.get("stream_mode", False)

        memory_response = memory_read_response(query, history)
        if memory_response is not None:
            return {
                "response": memory_response,
                "tokens_used": 0,
                "selected_agents": [],
                "context_sources": [],
                "execution_trace": ["direct_respond:memory_read_shortcut"],
            }

        local_response = local_conversation_response(query)
        if local_response is not None:
            return {
                "response": local_response,
                "tokens_used": 0,
                "selected_agents": [],
                "context_sources": [],
                "execution_trace": ["direct_respond:local_shortcut"],
            }

        # Check response cache (skip for streaming and when history exists —
        # follow-up queries depend on context that changes per session)
        if not stream_mode and not history:
            cache = get_response_cache()
            cached = cache.get(query)
            if cached is not None:
                log.debug("direct_respond: cache hit for %r", query[:60])
                return cached

        messages_dicts = [{"role": "system", "content": system_prompt}]

        for msg in (history or [])[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            messages_dicts.append({"role": role, "content": content})

        messages_dicts.append({"role": "user", "content": query})

        if stream_mode:
            return {
                "response": "",
                "stream_messages": messages_dicts,
                "tokens_used": 0,
                "selected_agents": [],
                "context_sources": [],
                "execution_trace": ["direct_respond:stream"],
            }

        messages = [SystemMessage(content=system_prompt)]

        for msg in (history or [])[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

        messages.append(HumanMessage(content=query))

        try:
            result = llm_adapter.invoke(messages, model=model)
            log.debug("direct_respond: model=%s", model)
            response = {
                "response": result.content,
                "tokens_used": 0,
                "selected_agents": [],
                "context_sources": [],
                "execution_trace": ["direct_respond:ok"],
            }
            # Cache the response for future similar queries
            if not stream_mode:
                cache = get_response_cache()
                cache.put(query, response)
            return response
        except Exception as e:
            log.warning("direct_respond: LLM failed: %s", e)
            return {
                "response": "Desculpa, nao consegui processar o pedido.",
                "tokens_used": 0,
                "selected_agents": [],
                "context_sources": [],
                "execution_trace": [f"direct_respond:error({e})"],
            }

    return direct_respond_node
