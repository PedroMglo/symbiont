"""Classify node — determines intent, complexity, and routing confidence."""

from __future__ import annotations

import logging

from orchestrator.cache.semantic_cache import get_classify_cache
from orchestrator.config import get_settings
from orchestrator.pipeline.state import SymbiontState
from orchestrator.routing.complexity import HeuristicComplexityClassifier
from orchestrator.routing.intent import HeuristicIntentClassifier, is_anaphoric
from orchestrator.types import Complexity, Intent

log = logging.getLogger(__name__)

_intent_clf = HeuristicIntentClassifier()
_complexity_clf = HeuristicComplexityClassifier()

# Intent signals that indicate high confidence (strong keyword matches)
_HIGH_CONFIDENCE_INTENTS = frozenset({
    Intent.SYSTEM,
    Intent.CODE,
    Intent.GRAPH,
})

# Combined intents have slightly lower confidence (ambiguity inherent)
_MEDIUM_CONFIDENCE_INTENTS = frozenset({
    Intent.LOCAL_AND_GRAPH,
    Intent.SYSTEM_AND_LOCAL,
    Intent.LOCAL,
    Intent.RESEARCH,
    Intent.PERSONAL_CONTEXT,
})


def _compute_confidence(
    query: str,
    intent: Intent,
    complexity: Complexity,
    history: list[dict] | None = None,
) -> float:
    """Compute routing confidence based on classification signals.

    Returns a score 0.0-1.0 indicating how certain we are about the routing.
    Below the configured threshold, the LLM fallback router will be invoked.
    History-aware penalties (anaphoric follow-ups, short GENERAL follow-ups) are
    governed by the [classify] config — never hardcoded.
    """
    cfg = get_settings().classify
    words = {w.strip(".,!?:;\"'()[]{}") for w in query.lower().split()}
    words.discard("")
    word_count = len(words)
    has_history = bool(history)

    # Start with base confidence per intent category
    if intent in _HIGH_CONFIDENCE_INTENTS:
        confidence = 0.9
    elif intent in _MEDIUM_CONFIDENCE_INTENTS:
        confidence = 0.8
    elif intent == Intent.CLARIFY:
        confidence = 0.95  # Very clear intent
    else:
        # GENERAL — lowest base confidence (catch-all)
        confidence = 0.6

    # Penalize very short queries (hard to classify)
    if word_count <= 2:
        confidence -= 0.15

    # Penalize very long queries (multi-part, ambiguous)
    if word_count > 20:
        confidence -= 0.1

    # Boost if complexity aligns with simple patterns
    if complexity == Complexity.SIMPLE and word_count <= 5:
        confidence += 0.05

    # Penalize DEEP complexity with GENERAL intent (likely needs better routing)
    if complexity == Complexity.DEEP and intent == Intent.GENERAL:
        confidence -= 0.15

    # Context-aware penalties — only meaningful with an active session.
    if has_history:
        # Anaphoric follow-ups ("explica isso", "e o ponto 2") need the LLM to
        # resolve the reference against history, so lower confidence to trigger it.
        if is_anaphoric(query):
            confidence -= cfg.anaphora_confidence_penalty
        # Short GENERAL follow-ups are likely continuations of the prior turn.
        elif intent == Intent.GENERAL and word_count <= cfg.general_followup_max_words:
            confidence -= cfg.general_followup_penalty

    return max(0.0, min(1.0, confidence))


def classify_node(state: SymbiontState) -> dict:
    """Classify intent, complexity, and compute routing confidence.

    This is the entry node of the graph — it analyzes the query and
    sets the classification fields that drive all subsequent routing decisions.
    """
    query = state["query"]
    history = state.get("history")

    # The semantic cache is keyed on query text alone, so it is only safe when
    # there is no conversation history (the same text can classify differently as
    # a follow-up). Skip it entirely for context-aware turns.
    cache = get_classify_cache()
    if not history:
        cached = cache.get(query)
        if cached is not None:
            log.debug("classify: cache hit for %r", query[:60])
            return cached

    intent = _intent_clf.classify(query, history=history)
    complexity = _complexity_clf.classify(query)
    confidence = _compute_confidence(query, intent, complexity, history=history)

    log.info(
        "classify: intent=%s complexity=%s confidence=%.2f query=%r",
        intent.value, complexity.value, confidence, query[:80],
    )

    result = {
        "intent": intent,
        "complexity": complexity,
        "confidence": confidence,
        "execution_trace": [f"classify:{intent.value}/{complexity.value}/{confidence:.2f}"],
    }

    # Only cache context-free classifications.
    if not history:
        cache.put(query, result)
    return result
