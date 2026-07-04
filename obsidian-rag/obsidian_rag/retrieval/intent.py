"""Detecção de intenção da query para selecção de fontes de contexto.

Adapter that converts router decisions to ``QueryIntent`` for the RAG pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from obsidian_rag.retrieval.router import ContextMode, RoutingDecision, route_query


@dataclass(frozen=True)
class QueryIntent:
    """Resultado da detecção de intenção."""
    use_notes: bool
    use_code: bool
    use_graph: bool


# Mapping from ContextMode → QueryIntent flags
_MODE_TO_INTENT = {
    ContextMode.NO_CONTEXT:    QueryIntent(use_notes=False, use_code=False, use_graph=False),
    ContextMode.RAG_ONLY:      QueryIntent(use_notes=True,  use_code=True,  use_graph=False),
    ContextMode.GRAPH_ONLY:    QueryIntent(use_notes=False, use_code=False, use_graph=True),
    ContextMode.RAG_AND_GRAPH: QueryIntent(use_notes=True,  use_code=True,  use_graph=True),
    ContextMode.CLARIFY:       QueryIntent(use_notes=True,  use_code=True,  use_graph=False),
}


def detect_intent(query: str, context_mode: str) -> QueryIntent:
    """Detecta que fontes de contexto beneficiam a query.

    Delegates to the LLM-based router (with keyword fallback).

    Args:
        query: texto da pergunta do utilizador
        context_mode: modo configurado — "auto"|"rag_only"|"graph_only"|"both"|"none"

    Returns:
        QueryIntent com flags use_notes, use_code, use_graph
    """
    decision = route_query(query, context_mode=context_mode)
    return _MODE_TO_INTENT.get(decision.mode, _MODE_TO_INTENT[ContextMode.RAG_ONLY])


def detect_intent_full(query: str, context_mode: str, *, history: list[dict] | None = None) -> tuple[QueryIntent, RoutingDecision]:
    """Like detect_intent but also returns the full RoutingDecision for observability."""
    decision = route_query(query, context_mode=context_mode, history=history)
    intent = _MODE_TO_INTENT.get(decision.mode, _MODE_TO_INTENT[ContextMode.RAG_ONLY])
    return intent, decision
