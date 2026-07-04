"""LLM fallback routing node — invoked when deterministic routing has low confidence."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from orchestrator.config import get_settings
from orchestrator.pipeline.language_context import language_context_from_state, routing_prompt_query
from orchestrator.pipeline.state import SymbiontState
from orchestrator.types import Complexity, Intent

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

# Agents the symbiont graph can actually dispatch. Invocation endpoints live
# in [dispatch.agent_endpoints]; the LLM router must only choose from these.
_VALID_AGENTS: frozenset[str] = frozenset({
    "reasoning_and_response",
    "audio_transcribe",
})

_SYSTEM_PROMPT = _prompt("system_2.md")


def _history_block(history: list[dict] | None, window: int) -> str:
    """Render the last `window` turns as a compact transcript for the router prompt."""
    if not history:
        return ""
    lines: list[str] = []
    for msg in history[-window:]:
        role = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {content[:400]}")
    return "\n".join(lines)


def create_llm_fallback_node(llm_adapter: Any):
    """Factory that creates the LLM fallback node with injected LLM adapter."""
    from langchain_core.messages import HumanMessage, SystemMessage

    def _invoke_router(messages: list[Any], *, model: str | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "temperature": 0.0,
            "max_tokens": 192,
            "timeout": 30,
        }
        if model:
            kwargs["model"] = model
        try:
            return llm_adapter.invoke(messages, **kwargs)
        except TypeError:
            # Some lightweight test adapters and older local wrappers only
            # accept (messages, model). Keep the production token cap above.
            if model:
                return llm_adapter.invoke(messages, model=model)
            return llm_adapter.invoke(messages)

    def llm_fallback_node(state: SymbiontState) -> dict:
        """Use LLM to decide routing when deterministic confidence is low."""
        query = state["query"]
        intent = state.get("intent", Intent.GENERAL)
        history = state.get("history")
        language_context = language_context_from_state(state)
        routing_query = routing_prompt_query(query, language_context)
        window = get_settings().classify.history_window
        t0 = time.perf_counter()

        try:
            transcript = _history_block(history, window)
            if transcript:
                human = (
                    f"Recent conversation:\n{transcript}\n\n"
                    f"Latest user query:\n{routing_query}"
                )
            else:
                human = f"Query:\n{routing_query}"

            messages = [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=human),
            ]
            # Use routing_model from config; fall back to default if "fast" unavailable
            routing_model = get_settings().dynamic_routing.routing_model or "fast"
            try:
                result = _invoke_router(messages, model=routing_model)
            except Exception:
                # "fast" backend might not be running — use default model
                result = _invoke_router(messages)
            raw = result.content

            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

            parsed = json.loads(text)
            agents = parsed.get("agents", [])

            selected = [a for a in agents if a in _VALID_AGENTS]

            if not selected:
                selected = _intent_default_agents(intent, state.get("complexity"))

            latency = (time.perf_counter() - t0) * 1000
            log.info("llm_fallback: selected=%s latency=%.0fms", selected, latency)

            return {
                "selected_agents": selected,
                "fallback_used": True,
                "execution_trace": [
                    f"llm_fallback->{selected}",
                    "i18n:routing_assisted" if routing_query != query else "i18n:routing_original",
                ],
            }

        except Exception as e:
            log.warning("llm_fallback failed: %s - using intent defaults", e)
            selected = _intent_default_agents(intent, state.get("complexity"))
            return {
                "selected_agents": selected,
                "fallback_used": True,
                "execution_trace": [f"llm_fallback:error->{selected}"],
            }

    return llm_fallback_node


def _intent_default_agents(intent: Intent, complexity: Complexity | str | None = None) -> list[str]:
    """Hard fallback: map intent to graph agents when even LLM routing fails.

    Mirrors route._INTENT_AGENTS so a fallback path never produces an agent name
    the dispatcher cannot resolve.
    """
    complex_general = (
        intent == Intent.GENERAL
        and complexity is not None
        and str(getattr(complexity, "value", complexity)).lower() in {"complex", "deep"}
    )
    if complex_general:
        return ["reasoning_and_response"]

    mapping = {
        Intent.GENERAL: [],  # General knowledge -> direct LLM, no external agents needed
        Intent.LOCAL: ["reasoning_and_response"],
        Intent.RESEARCH: ["reasoning_and_response"],
        Intent.PERSONAL_CONTEXT: ["reasoning_and_response"],
        Intent.CODE: ["reasoning_and_response"],
        Intent.SYSTEM: ["reasoning_and_response"],
        Intent.GRAPH: ["reasoning_and_response"],
        Intent.AUDIO: ["audio_transcribe"],
        Intent.LOCAL_AND_GRAPH: ["reasoning_and_response"],
        Intent.SYSTEM_AND_LOCAL: ["reasoning_and_response"],
        Intent.CLARIFY: [],
    }
    return list(mapping.get(intent, ["reasoning_and_response"]))
