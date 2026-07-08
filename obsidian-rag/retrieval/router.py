"""LLM-based context router with keyword heuristic fallback.

Decides whether a query needs local context (RAG/Graph) or can be
answered with general LLM knowledge. Domain-agnostic by design.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum

from prompts.templates import ROUTER_SYSTEM, ROUTER_USER_TEMPLATE
from rag_config import settings
from registry import get_rag_model

log = logging.getLogger(__name__)


class ContextMode(str, Enum):
    """Routing decision for a query."""
    NO_CONTEXT = "NO_CONTEXT"
    RAG_ONLY = "RAG_ONLY"
    GRAPH_ONLY = "GRAPH_ONLY"
    RAG_AND_GRAPH = "RAG_AND_GRAPH"
    CLARIFY = "CLARIFY"


@dataclass(frozen=True)
class RoutingDecision:
    """Result of the routing step."""
    mode: ContextMode
    confidence: float      # 0.0–1.0
    reason: str
    method: str            # "llm" | "heuristic" | "explicit"
    latency_ms: float = 0.0


# ── Keyword-based heuristic (fallback) ──────────────────────────────────────

# Signals that the user is referring to their OWN local content
_LOCAL_SIGNALS = frozenset({
    # Possessives / self-references (PT + EN)
    "meu", "minha", "meus", "minhas", "nosso", "nossa",
    "my", "our", "mine",
    # Explicit local references
    "obsidian", "vault", "notas", "notes",
    "repo", "repositório", "repository", "projeto", "project",
    "ficheiro", "ficheiros", "file", "files",
    "código", "code", "script", "scripts",
    "configuração", "config", "setup",
    "documentos", "documents", "docs",
    "indexado", "indexed", "local",
    # Pipeline / project-specific terms (PT + EN)
    "pipeline", "codebase", "workspace",
    "modelfile", "modelfiles",
    "instalado", "instalados", "installed",
    "configurado", "configurados", "configured",
    "alias", "aliases", "funções", "functions",
})

# Signals that the user wants relational/structural info
_GRAPH_SIGNALS = frozenset({
    "depende", "dependência", "dependências", "depends", "dependency",
    "chama", "chamada", "calls", "called",
    "importa", "imports", "importação",
    "fluxo", "flow", "pipeline", "cadeia", "chain",
    "arquitectura", "arquitetura", "architecture", "structure", "estrutura",
    "impacto", "impact", "afeta", "affects",
    "relação", "relações", "relation", "relations", "relationship",
    "componente", "componentes", "component", "components",
    "módulo", "módulos", "module", "modules",
    "vizinhos", "neighbors", "neighbour",
    "comunidade", "community",
    "grafo", "graph",
    "upstream", "downstream", "montante", "jusante",
})

# Multi-word patterns for graph queries
_GRAPH_PATTERNS = (
    # PT
    "como funciona", "como é que", "o que chama", "quem chama",
    "o que depende", "quem depende", "qual o fluxo", "qual é o fluxo",
    "que relação", "como se liga", "como interage",
    "o que acontece se mudar", "impacto de alterar",
    "este projeto", "este repo", "este módulo", "este pipeline",
    "o meu pipeline", "o meu repo", "o meu projeto",
    # EN
    "how does", "what calls", "what depends", "call chain", "call flow",
    "depends on", "used by", "calls to",
    "this project", "this repo", "this module", "this pipeline",
    "my pipeline", "my repo", "my project",
)


def _heuristic_route(query: str) -> RoutingDecision:
    """Fast keyword heuristic — domain-agnostic."""
    q_lower = query.lower()
    words = {w.strip(".,!?:;\"'()[]{}") for w in q_lower.split()}
    words.discard("")

    has_local = bool(words & _LOCAL_SIGNALS)
    has_graph = bool(words & _GRAPH_SIGNALS) or any(p in q_lower for p in _GRAPH_PATTERNS)

    if has_local and has_graph:
        return RoutingDecision(
            mode=ContextMode.RAG_AND_GRAPH,
            confidence=0.7,
            reason="Local reference + structural/relational signals.",
            method="heuristic",
        )
    if has_graph and not has_local:
        # Graph signals without explicit local ref — could be general or local
        # Be conservative: if they mention structural terms, check if they refer to their project
        # Look for additional project-like context
        project_hints = {"meu", "minha", "nosso", "my", "our", "repo", "projeto", "project"}
        if words & project_hints:
            return RoutingDecision(
                mode=ContextMode.RAG_AND_GRAPH,
                confidence=0.6,
                reason="Structural/relational signals with project reference.",
                method="heuristic",
            )
        # Pure structural words without local context → general question
        return RoutingDecision(
            mode=ContextMode.NO_CONTEXT,
            confidence=0.5,
            reason="Structural terms without explicit local content reference.",
            method="heuristic",
        )
    if has_local:
        return RoutingDecision(
            mode=ContextMode.RAG_ONLY,
            confidence=0.7,
            reason="Explicit reference to user's local content.",
            method="heuristic",
        )

    # No signals → general question
    return RoutingDecision(
        mode=ContextMode.NO_CONTEXT,
        confidence=0.6,
        reason="No local content references — general question.",
        method="heuristic",
    )


# ── LLM-based router ────────────────────────────────────────────────────────

_ROUTE_PATTERN = re.compile(
    r"ROUTE:\s*(NO_CONTEXT|RAG_ONLY|GRAPH_ONLY|RAG_AND_GRAPH|SYSTEM|SYSTEM_AND_RAG|CLARIFY)",
    re.IGNORECASE,
)
_REASON_PATTERN = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)

# Strip <think>…</think> blocks (chain-of-thought model compatibility)
_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


def _llm_route(query: str, model: str, base_url: str, *, history: list[dict] | None = None) -> RoutingDecision | None:
    """Call LLM to classify the query. Returns None on failure.

    If history is provided, the last 2 user messages are prepended for
    follow-up detection so the router can see conversational context.
    """
    from llm import get_llm_client

    t0 = time.perf_counter()

    # Build messages: system + optional history excerpt + current query
    messages: list[dict] = [{"role": "system", "content": ROUTER_SYSTEM}]
    if history:
        # Include up to 2 recent user messages as context for follow-ups
        recent_user = [m for m in history if m.get("role") == "user"][-2:]
        for msg in recent_user:
            messages.append({"role": "user", "content": ROUTER_USER_TEMPLATE.format(query=msg["content"])})
            messages.append({"role": "assistant", "content": "(previous turn — classify the NEXT question)"})
    messages.append({"role": "user", "content": ROUTER_USER_TEMPLATE.format(query=query)})

    try:
        raw = get_llm_client().chat(
            messages,
            model,
            temperature=0.0,
            max_tokens=64,
            timeout=float(settings.performance.query_timeout_seconds),
        )
    except Exception as exc:
        log.warning("Router LLM call failed: %s", exc)
        return None

    latency = (time.perf_counter() - t0) * 1000

    # _THINK_PATTERN already stripped by LLMClient
    route_match = _ROUTE_PATTERN.search(raw)
    if not route_match:
        log.warning("Router LLM returned unparsable response: %s", raw[:200])
        return None

    mode_str = route_match.group(1).upper()
    try:
        mode = ContextMode(mode_str)
    except ValueError:
        return None

    reason_match = _REASON_PATTERN.search(raw)
    reason = reason_match.group(1).strip() if reason_match else "LLM classification"

    return RoutingDecision(
        mode=mode,
        confidence=0.9,
        reason=reason,
        method="llm",
        latency_ms=round(latency, 1),
    )


# ── Public API ───────────────────────────────────────────────────────────────

def route_query(query: str, *, context_mode: str | None = None, history: list[dict] | None = None) -> RoutingDecision:
    """Route a query to the appropriate context strategy.

    If context_mode is an explicit mode (not "auto"), returns it directly.
    Otherwise, tries LLM router first, falls back to keyword heuristic.

    Args:
        query: current user query text.
        context_mode: explicit mode or "auto".
        history: previous chat messages for multi-turn follow-up detection.
    """
    mode = context_mode or settings.retrieval.context_mode

    # Explicit modes bypass router
    explicit_map = {
        "none": ContextMode.NO_CONTEXT,
        "rag_only": ContextMode.RAG_ONLY,
        "graph_only": ContextMode.GRAPH_ONLY,
        "both": ContextMode.RAG_AND_GRAPH,
        "system": ContextMode.SYSTEM,
    }
    if mode in explicit_map:
        return RoutingDecision(
            mode=explicit_map[mode],
            confidence=1.0,
            reason=f"Explicit mode: {mode}",
            method="explicit",
        )

    # Auto mode: try LLM router, fallback to heuristic
    router_cfg = getattr(settings, "router", None)
    use_llm = router_cfg.enabled if router_cfg else True

    if use_llm:
        model = router_cfg.model if router_cfg else get_rag_model("router")
        base_url = settings.ollama.base_url
        decision = _llm_route(query, model, base_url, history=history)
        if decision is not None:
            log.info(
                "Router LLM: %s (confidence=%.1f, %dms) — %s",
                decision.mode.value, decision.confidence,
                decision.latency_ms, decision.reason,
            )
            return decision

    # Fallback to heuristic
    decision = _heuristic_route(query)
    log.info(
        "Router heuristic: %s (confidence=%.1f) — %s",
        decision.mode.value, decision.confidence, decision.reason,
    )
    return decision
