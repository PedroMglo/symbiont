"""Language-related state helpers for the orchestration pipeline."""

from __future__ import annotations

from typing import Any, Literal

LanguageMode = Literal["off", "shadow", "assisted", "enforce"]

_BEHAVIORAL_MODES = {"assisted", "enforce"}
_BLOCKING_DRIFT_RISKS = {"high", "critical"}


def normalize_language_mode(value: object = None) -> LanguageMode:
    mode = str(value or "shadow").lower()
    if mode not in {"off", "shadow", "assisted", "enforce"}:
        return "shadow"
    return mode  # type: ignore[return-value]


def _bool_value(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _protected_spans_count(raw: dict[str, Any]) -> int:
    protected_spans = raw.get("protected_spans")
    if isinstance(protected_spans, dict):
        try:
            return int(protected_spans.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(raw.get("protected_spans_count") or 0)
    except (TypeError, ValueError):
        return 0


def _quality_payload(raw: dict[str, Any]) -> dict[str, Any]:
    quality = raw.get("quality")
    if isinstance(quality, dict):
        return dict(quality)
    confidence = _float_or_none(raw.get("confidence"))
    return {
        "semantic_drift_score": _float_or_none(raw.get("semantic_drift_score")),
        "confidence": confidence,
        "drift_risk": "unknown",
        "reason": raw.get("fallback_reason") or "quality_not_reported",
    }


def language_context_fallback(
    text: str,
    *,
    reason: str | None = None,
    mode: LanguageMode = "shadow",
) -> dict[str, Any]:
    return {
        "original_text": text,
        "normalized_text": text,
        "english_text": text,
        "source_language": "unknown",
        "source_variant": "unknown",
        "target_language": "en",
        "user_language": "unknown",
        "response_language": "same_as_user",
        "translation_available": False,
        "translation_latency_ms": 0.0,
        "translation_cache_hit": False,
        "protected_spans_count": 0,
        "fallback_used": reason is not None,
        "fallback_reason": reason,
        "mode": normalize_language_mode(mode),
        "contract_version": "orchestrator.language_context.fallback.v1",
        "semantic_drift_score": None,
        "confidence": None,
        "translation_safe": False,
        "quality": {
            "semantic_drift_score": None,
            "confidence": None,
            "drift_risk": "unknown",
            "reason": reason or "fallback",
        },
        "safety_error": None,
        "language_context_contract": None,
    }


def normalize_language_context(
    raw: dict[str, Any] | None,
    *,
    original_query: str | None = None,
) -> dict[str, Any] | None:
    """Return the orchestrator's stable language context shape.

    The translation feature owns the versioned LanguageContext contract. The
    orchestrator normalizes that contract, older translation payloads, and
    fallback dicts into one runtime shape for routing assistance, RAG dual-query
    and downstream agent metadata.
    """

    if not isinstance(raw, dict):
        return None

    original = raw.get("original_text") or raw.get("original_query") or raw.get("original") or original_query
    normalized = raw.get("normalized_text") or raw.get("normalized_query") or raw.get("normalized") or original
    english = raw.get("english_text") or raw.get("working_query") or raw.get("translated") or normalized
    if original is None or normalized is None or english is None:
        return None

    source_language = str(raw.get("source_language") or "unknown")
    source_variant = str(raw.get("source_variant") or "unknown")
    user_language = str(raw.get("user_language") or (source_variant if source_variant != "unknown" else source_language))
    response_language = str(
        raw.get("response_language")
        or ("pt-PT" if source_variant == "pt-PT" else "same_as_user")
    )
    fallback_reason = raw.get("fallback_reason")
    fallback_used = _bool_value(raw.get("fallback_used"), default=fallback_reason is not None)
    safety_error = raw.get("safety_error") if raw.get("safety_error") else None
    translation_available = _bool_value(
        raw.get("translation_available", raw.get("translation_applied")),
        default=False,
    )
    translation_safe = _bool_value(
        raw.get("translation_safe"),
        default=translation_available and safety_error is None,
    )
    quality = _quality_payload(raw)
    confidence = _float_or_none(raw.get("confidence"))
    if confidence is None:
        confidence = _float_or_none(quality.get("confidence"))
    semantic_drift_score = _float_or_none(raw.get("semantic_drift_score"))
    if semantic_drift_score is None:
        semantic_drift_score = _float_or_none(quality.get("semantic_drift_score"))

    context = dict(raw)
    context.update(
        {
            "original_text": str(original),
            "normalized_text": str(normalized),
            "english_text": str(english),
            "source_language": source_language,
            "source_variant": source_variant,
            "target_language": str(raw.get("target_language") or "en"),
            "user_language": user_language,
            "response_language": response_language,
            "translation_available": translation_available,
            "translation_latency_ms": _float_or_none(raw.get("translation_latency_ms") or raw.get("latency_ms")) or 0.0,
            "translation_cache_hit": _bool_value(raw.get("translation_cache_hit", raw.get("cache_hit")), default=False),
            "protected_spans_count": _protected_spans_count(raw),
            "fallback_used": fallback_used,
            "fallback_reason": str(fallback_reason) if fallback_reason else None,
            "mode": normalize_language_mode(raw.get("mode")),
            "contract_version": str(raw.get("contract_version") or ""),
            "semantic_drift_score": semantic_drift_score,
            "confidence": confidence,
            "translation_safe": translation_safe,
            "quality": quality,
            "safety_error": safety_error,
        }
    )
    if "language_context_contract" not in context:
        context["language_context_contract"] = raw if str(raw.get("contract_version") or "").startswith("translation.") else None
    return context


def language_context_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    raw = state.get("language_context")
    return normalize_language_context(
        raw if isinstance(raw, dict) else None,
        original_query=str(state.get("original_query") or state.get("query") or "") or None,
    )


def behavioral_mode(context: dict[str, Any] | None) -> LanguageMode:
    if context is None:
        return "off"
    return normalize_language_mode(context.get("mode"))


def has_usable_english(context: dict[str, Any] | None, original_query: str) -> bool:
    if context is None:
        return False
    if behavioral_mode(context) not in _BEHAVIORAL_MODES:
        return False
    english = str(context.get("english_text") or "").strip()
    if not bool(context.get("translation_available")) or not english:
        return False
    if context.get("translation_safe") is False or context.get("safety_error"):
        return False
    quality = context.get("quality")
    if isinstance(quality, dict):
        drift_risk = str(quality.get("drift_risk") or "").lower()
        if drift_risk in _BLOCKING_DRIFT_RISKS:
            return False
        confidence = _float_or_none(quality.get("confidence"))
        if confidence is not None and confidence < 0.4:
            return False
    return english != original_query.strip()


def english_query_for_assistance(context: dict[str, Any] | None, original_query: str) -> str | None:
    if not has_usable_english(context, original_query):
        return None
    return str(context.get("english_text") or "").strip()


def routing_prompt_query(query: str, context: dict[str, Any] | None) -> str:
    """Render query text for model-assisted routing without changing source truth."""

    english = english_query_for_assistance(context, query)
    if not english:
        return query
    return (
        "Original user query:\n"
        f"{query}\n\n"
        "English normalization for routing only:\n"
        f"{english}\n\n"
        "Use the original query as the source of truth."
    )


def rag_dual_query_enabled(settings: Any, context: dict[str, Any] | None, original_query: str) -> bool:
    raw = getattr(settings, "i18n_raw", {}) or {}
    rag_cfg = raw.get("i18n_rag", {})
    if not bool(rag_cfg.get("dual_query", False)):
        return False
    if not bool(rag_cfg.get("use_original_query", True)):
        return False
    if not bool(rag_cfg.get("use_translated_query", True)):
        return False
    return has_usable_english(context, original_query)


def choose_model_query(context: dict[str, Any], *, mode: object) -> str:
    if normalize_language_mode(mode) in {"off", "shadow"}:
        return str(context.get("original_text") or "")
    original = str(context.get("original_text") or "")
    english = english_query_for_assistance(context, original)
    if english:
        return english
    return str(context.get("original_text") or "")
