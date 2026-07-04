"""Speculative context prefetch node (v1.2 — Pipeline Parallelism).

Runs IN PARALLEL with classify via Send() dispatch. Predicts likely context
sources from raw query keywords using the same signal sets as intent.py.
If speculation matches the final route, context is already gathered — skipping
the context phase entirely (saving 200-500ms).

If speculation is wrong, results are simply discarded (zero correctness impact).
"""

from __future__ import annotations

import logging
import re
import time

from orchestrator.capabilities.context_routing import context_sources_for_intent
from orchestrator.pipeline.state import SymbiontState
from orchestrator.routing.intent import (
    _CODE_SIGNALS,
    _GENERAL_CONTEXT_SIGNALS,
    _GRAPH_SIGNALS,
    _LOCAL_SIGNALS,
    _SYSTEM_SIGNALS,
)
from orchestrator.types import Intent

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\b\w+\b")


def _predict_intent(query: str) -> Intent:
    """Lightweight intent prediction from keywords — no LLM call."""
    words = frozenset(w.lower() for w in _WORD_RE.findall(query))

    has_local = bool(words & _LOCAL_SIGNALS)
    has_graph = bool(words & _GRAPH_SIGNALS)
    has_system = bool(words & _SYSTEM_SIGNALS)
    has_code = bool(words & _CODE_SIGNALS)
    has_general_ctx = bool(words & _GENERAL_CONTEXT_SIGNALS)

    if has_system and has_local:
        return Intent.SYSTEM_AND_LOCAL
    if has_system and not has_general_ctx:
        return Intent.SYSTEM
    if has_code:
        return Intent.CODE
    if has_local and has_graph:
        return Intent.LOCAL_AND_GRAPH
    if has_graph:
        return Intent.GRAPH
    if has_local:
        return Intent.LOCAL
    return Intent.GENERAL


def _predict_sources(query: str) -> list[str]:
    """Predict context sources from query keywords."""
    intent = _predict_intent(query)
    return list(context_sources_for_intent(intent))


def speculate_node(state: SymbiontState) -> dict:
    """Predict context sources and prefetch them.

    This node runs in parallel with classify. Its results are checked
    against the actual route after classification completes.
    """
    query = state["query"]
    t0 = time.perf_counter()

    predicted_sources = _predict_sources(query)
    if not predicted_sources:
        log.debug("speculate_node: no sources predicted for query")
        return {
            "speculative_context": [],
            "speculation_sources": [],
            "execution_trace": ["speculate:no_prediction"],
        }

    log.debug("speculate_node: predicted sources=%s", predicted_sources)

    duration_ms = (time.perf_counter() - t0) * 1000
    log.info(
        "speculate_node: predicted %s (%.0fms)",
        predicted_sources, duration_ms,
    )

    return {
        "speculative_context": [],
        "speculation_sources": predicted_sources,
        "execution_trace": [f"speculate:predicted:{duration_ms:.0f}ms"],
    }
