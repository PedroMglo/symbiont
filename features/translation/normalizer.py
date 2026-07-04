"""Normalization pipeline for PT-PT -> EN assistance."""

from __future__ import annotations

import re
import time

from cache import CacheKeyParts, SQLiteCache, build_cache_key
from config import TranslationConfig
from glossary import Glossary
from language_detector import detect_language
from models import (
    I18NMode,
    LanguageContext,
    LanguageTransformation,
    LanguageWarning,
    NormalizeRequest,
    NormalizeResponse,
    ProtectedSpanAudit,
    SemanticQuality,
    TranslationSafetyError,
)
from protected_spans import (
    protect_text,
    protected_ratio,
    restore_text,
    spans_content_hash,
    spans_output_content_hash,
    spans_structure_hash,
)
from spellcheck import Spellchecker, SpellcheckConfig
from translator import LocalTranslator, TranslatorConfig


_TRANSLATION_BLOCKING_SPAN_KINDS = {"markdown_code_block", "sql", "traceback"}
_TRANSLATION_CRITICAL_SPAN_KINDS = {
    "markdown_code_block",
    "inline_code",
    "url",
    "email",
    "uuid",
    "hash",
    "sql",
    "traceback",
    "env_assignment",
    "env_var",
    "shell_command",
    "windows_path",
    "linux_path",
    "dotfile",
    "filename",
    "ip_port",
    "model_name",
    "cuda_error",
    "structured_line",
}


class Normalizer:
    def __init__(self, config: TranslationConfig):
        self.config = config
        self.spellchecker = Spellchecker(
            SpellcheckConfig(
                dictionary_path=config.hunspell_dic_path,
                autocorrect_enabled=config.autocorrect_enabled,
                autocorrect_threshold=config.autocorrect_threshold,
                max_edit_distance=config.max_edit_distance,
            )
        )
        self.pt_to_en = Glossary(config.pt_to_en_path)
        self.en_to_pt = Glossary(config.en_to_pt_path)
        self.cache = SQLiteCache(config.cache_path, ttl_seconds=config.cache_ttl_seconds, enabled=config.cache_enabled)
        self.translator = LocalTranslator(
            TranslatorConfig(
                enabled=config.translation_enabled,
                backend=config.translation_backend,
                model_path=config.ct2_model_path,
                source_lang=config.source_lang,
                target_lang=config.target_lang,
                device=config.device,
                compute_type=config.compute_type,
                intra_threads=config.intra_threads,
                inter_threads=config.inter_threads,
                ollama_base_url=config.ollama_base_url,
                ollama_model=config.ollama_model,
                ollama_timeout_seconds=config.ollama_timeout_seconds,
                ollama_chunk_chars=config.ollama_chunk_chars,
                ollama_max_tokens=config.ollama_max_tokens,
            )
        )

    def normalize(self, request: NormalizeRequest) -> NormalizeResponse:
        start = time.perf_counter()
        mode = _normalize_mode(request.mode or self.config.mode)
        original = request.text
        guess = detect_language(original)

        protected_text, spans = protect_text(original) if request.protect_spans else (original, [])
        protected_before_hash = spans_content_hash(spans)
        fallback_reason: str | None = None
        spellcheck_applied = False
        glossary_applied = False
        translation_applied = False
        cache_hit = False
        safety_error: TranslationSafetyError | None = None
        warnings: list[LanguageWarning] = []

        working = protected_text
        spell_latency_ms = 0.0
        if request.spellcheck:
            apply_autocorrect = not request.translate
            working, _spell_response, spell_changed, spell_latency_ms = self.spellchecker.check_text(
                working,
                apply_autocorrect=apply_autocorrect,
            )
            spellcheck_applied = spell_changed

        normalized = restore_text(working, spans) if spans else working

        glossary_working, glossary_changed = self.pt_to_en.apply(working)
        glossary_applied = glossary_changed
        translated_candidate = restore_text(glossary_working, spans) if spans else glossary_working

        should_translate = request.translate and mode != "off" and len(original.strip()) >= self.config.min_translate_chars
        if should_translate and _source_matches_target(guess.language, guess.variant, request.target_language):
            should_translate = False
            fallback_reason = "source_already_target_language"
        blocking_spans = [span for span in spans if span.kind in _TRANSLATION_BLOCKING_SPAN_KINDS]
        if blocking_spans and protected_ratio(original, blocking_spans) > self.config.max_protected_ratio:
            should_translate = False
            fallback_reason = "too_many_protected_spans"

        cache_key = build_cache_key(
            CacheKeyParts(
                normalized_text=normalized,
                source_lang=guess.variant,
                target_lang=request.target_language,
                glossary_version=self.pt_to_en.version,
                model_version=self.translator.model_version,
                spans_hash=spans_structure_hash(spans),
            )
        )
        cached = self.cache.get(cache_key)
        if cached:
            cache_hit = True
            translated = str(cached.get("translated", translated_candidate))
            translation_applied = bool(cached.get("translation_applied", False))
            fallback_reason = cached.get("fallback_reason")
        elif should_translate:
            translated_working, translation_applied, translate_fallback = self.translator.translate(
                working,
                timeout_ms=request.max_latency_ms,
            )
            translated = restore_text(translated_working, spans) if spans else translated_working
            if translate_fallback and fallback_reason is None:
                fallback_reason = translate_fallback
        else:
            translated = translated_candidate
            if fallback_reason is None:
                fallback_reason = "translation_skipped"

        protected_after_hash, raw_missing_kinds = spans_output_content_hash(spans, translated)
        missing_kinds = _critical_missing_span_kinds(raw_missing_kinds)
        soft_missing_kinds = [kind for kind in raw_missing_kinds if kind not in _TRANSLATION_CRITICAL_SPAN_KINDS]
        protected_altered = bool(missing_kinds)
        if soft_missing_kinds:
            warnings.append(
                LanguageWarning(
                    code="protected_spans_soft_altered",
                    detail=(
                        "translation output changed non-critical technical spans: "
                        + ", ".join(sorted(set(soft_missing_kinds)))
                    ),
                )
            )
        if protected_altered and should_translate and spans:
            repaired = self._append_protected_span_reference(translated, spans)
            repaired_after_hash, repaired_raw_missing_kinds = spans_output_content_hash(spans, repaired)
            repaired_missing_kinds = _critical_missing_span_kinds(repaired_raw_missing_kinds)
            if translation_applied and not repaired_missing_kinds:
                translated = repaired
                fallback_reason = None
                protected_after_hash = repaired_after_hash
                raw_missing_kinds = repaired_raw_missing_kinds
                missing_kinds = []
                protected_altered = False
                if cache_hit:
                    self.cache.delete(cache_key)
                    cache_hit = False
                warnings.append(
                    LanguageWarning(
                        code="protected_spans_repaired",
                        detail="translation was annotated with exact protected spans from the original request",
                    )
                )
        if protected_altered:
            safety_error = TranslationSafetyError(
                code="protected_spans_altered",
                message="Translation output did not preserve all protected spans; normalized fallback returned.",
                stage="cache" if cache_hit else "translation",
                fallback_applied=True,
                protected_span_kinds=missing_kinds,
            )
            warnings.append(
                LanguageWarning(
                    code="protected_spans_altered",
                    detail="translation output did not preserve all protected spans",
                )
            )
            if cache_hit:
                self.cache.delete(cache_key)
            translated = translated_candidate
            translation_applied = False
            fallback_reason = "protected_spans_altered"
            protected_after_hash, raw_missing_kinds = spans_output_content_hash(spans, translated)
            missing_kinds = _critical_missing_span_kinds(raw_missing_kinds)
            protected_altered = bool(missing_kinds)

        artifact_reason = _artifact_translation_reason(
            original=original,
            normalized=normalized,
            translated=translated,
            translation_applied=translation_applied,
        )
        if artifact_reason:
            safety_error = TranslationSafetyError(
                code="unsafe_translation",
                message="Translation output appears to answer or create an artifact instead of translating the user request.",
                stage="cache" if cache_hit else "output_validation",
                fallback_applied=True,
            )
            warnings.append(LanguageWarning(code="unsafe_translation", detail=artifact_reason))
            if cache_hit:
                self.cache.delete(cache_key)
            translated = translated_candidate
            translation_applied = False
            fallback_reason = f"unsafe_translation:{artifact_reason}"

        if not cached and should_translate and safety_error is None:
            self.cache.set(
                cache_key,
                {
                    "normalized": normalized,
                    "translated": translated,
                    "translation_applied": translation_applied,
                    "fallback_reason": fallback_reason,
                    "model_version": self.translator.model_version,
                },
            )

        latency_ms = (time.perf_counter() - start) * 1000
        fallback_used = fallback_reason is not None
        working_query = translated if translation_applied else normalized
        semantic_drift_score = _semantic_drift_score(
            original=original,
            normalized=normalized,
            working_query=working_query,
            mode=mode,
            translation_applied=translation_applied,
            fallback_reason=fallback_reason,
            protected_altered=protected_altered,
            safety_error=safety_error,
        )
        confidence = _confidence_score(
            semantic_drift_score=semantic_drift_score,
            mode=mode,
            fallback_reason=fallback_reason,
            translation_applied=translation_applied,
            protected_altered=protected_altered,
            safety_error=safety_error,
        )
        quality = SemanticQuality(
            mode=mode,
            semantic_drift_score=semantic_drift_score,
            confidence=confidence,
            drift_risk=_drift_risk(semantic_drift_score, safety_error=safety_error),
            confidence_reason=_confidence_reason(
                original=original,
                normalized=normalized,
                mode=mode,
                translation_applied=translation_applied,
                fallback_reason=fallback_reason,
                safety_error=safety_error,
            ),
        )
        if translation_applied:
            warnings.append(
                LanguageWarning(
                    code="semantic_drift_heuristic_only",
                    detail="semantic drift score is based on deterministic guardrails",
                )
            )
        if fallback_reason:
            warnings.append(LanguageWarning(code="fallback_used", detail=fallback_reason))
        if safety_error:
            warnings.append(LanguageWarning(code=safety_error.code, detail=safety_error.message))

        protected_audit = ProtectedSpanAudit(
            count=len(spans),
            before_hash=protected_before_hash,
            after_hash=protected_after_hash,
            hashes_match=protected_before_hash == protected_after_hash,
            altered=protected_altered,
            missing_kinds=missing_kinds,
        )
        transformations = [
            LanguageTransformation(
                name="protect_spans",
                applied=bool(spans),
                reason=None if request.protect_spans else "disabled",
            ),
            LanguageTransformation(name="spellcheck", applied=spellcheck_applied),
            LanguageTransformation(name="glossary", applied=glossary_applied),
            LanguageTransformation(name="translation", applied=translation_applied, reason=fallback_reason),
            LanguageTransformation(name="cache", applied=cache_hit),
            LanguageTransformation(name="fallback", applied=fallback_used, reason=fallback_reason),
        ]
        language_context = LanguageContext(
            mode=mode,
            original_query=original,
            normalized_query=normalized,
            working_query=working_query,
            source_language=guess.language,
            source_variant=guess.variant,
            target_language=request.target_language,
            transformations=transformations,
            warnings=warnings,
            protected_spans=protected_audit,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            translation_applied=translation_applied,
            cache_hit=cache_hit,
            semantic_drift_score=semantic_drift_score,
            confidence=confidence,
            translation_safe=not protected_altered,
            quality=quality,
            safety_error=safety_error,
        )
        debug = None
        if request.return_debug:
            debug = {
                "spellcheck_latency_ms": spell_latency_ms,
                "protected_ratio": protected_ratio(original, spans) if spans else 0.0,
                "mode": mode,
                "translator_fallback": self.translator.fallback_reason,
                "protected_span_hash_before": protected_before_hash,
                "protected_span_hash_after": protected_after_hash,
            }

        return NormalizeResponse(
            original=original,
            normalized=normalized,
            translated=translated,
            working_query=working_query,
            mode=mode,
            source_language=guess.language,
            source_variant=guess.variant,
            target_language=request.target_language,
            protected_spans_count=len(spans),
            spellcheck_applied=spellcheck_applied,
            glossary_applied=glossary_applied,
            translation_applied=translation_applied,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            semantic_drift_score=semantic_drift_score,
            confidence=confidence,
            translation_safe=not protected_altered,
            quality=quality,
            safety_error=safety_error,
            warnings=warnings,
            language_context=language_context,
            debug=debug,
        )

    def _append_protected_span_reference(self, text: str, spans: list) -> str:
        span_texts = [span.text for span in spans]
        if not span_texts:
            return text
        reference = "\n".join(f"- {item}" for item in span_texts)
        suffix = "Preserved technical spans from the original request:\n" + reference
        if not text.strip():
            return suffix
        return text.rstrip() + "\n\n" + suffix + "\n"


def _critical_missing_span_kinds(missing_kinds: list[str]) -> list[str]:
    return [kind for kind in missing_kinds if kind in _TRANSLATION_CRITICAL_SPAN_KINDS]


def _semantic_drift_score(
    *,
    original: str,
    normalized: str,
    working_query: str,
    mode: I18NMode,
    translation_applied: bool,
    fallback_reason: str | None,
    protected_altered: bool,
    safety_error: TranslationSafetyError | None,
) -> float:
    if protected_altered or not working_query.strip():
        return 1.0
    if safety_error:
        return 0.95
    if mode == "off":
        return 0.03 if normalized.strip() != original.strip() else 0.0
    score = 0.0
    if translation_applied:
        return {"shadow": 0.08, "assisted": 0.10, "enforce": 0.12}.get(mode, 0.10)
    if normalized.strip() != original.strip():
        score = max(score, 0.05)
    if fallback_reason and fallback_reason not in {"translation_skipped", "source_already_target_language"}:
        score = max(score, 0.45)
    return min(1.0, score)


def _artifact_translation_reason(
    *,
    original: str,
    normalized: str,
    translated: str,
    translation_applied: bool,
) -> str | None:
    if not translation_applied:
        return None
    original_text = (original or "").strip()
    normalized_text = (normalized or "").strip()
    translated_text = (translated or "").strip()
    if not translated_text:
        return "empty_translation"
    original_has_code_fence = "```" in original_text
    translated_has_code_fence = "```" in translated_text
    if translated_has_code_fence and not original_has_code_fence:
        return "introduced_markdown_code_block"
    if not original_has_code_fence and _contains_code_artifact(translated_text):
        return "introduced_code_artifact"
    original_len = max(1, len(normalized_text or original_text))
    if len(translated_text) > max(450, original_len * 4):
        return "expanded_beyond_translation_shape"
    leading = translated_text.lower().lstrip()
    answer_prefixes = (
        "here is ",
        "here's ",
        "below is ",
        "sure,",
        "the provided ",
        "aqui está",
        "claro,",
    )
    if leading.startswith(answer_prefixes) and not (original_text.lower().lstrip().startswith(answer_prefixes)):
        return "answer_style_output"
    return None


def _contains_code_artifact(text: str) -> bool:
    code_patterns = (
        r"(?m)^\s*class\s+[A-Za-z_][A-Za-z0-9_]*(?:\(|:)",
        r"(?m)^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(",
        r"(?m)^\s*from\s+[A-Za-z_][A-Za-z0-9_.]*\s+import\s+",
        r"(?m)^\s*import\s+[A-Za-z_][A-Za-z0-9_.]*",
    )
    return any(re.search(pattern, text) for pattern in code_patterns)


def _confidence_score(
    *,
    semantic_drift_score: float,
    mode: I18NMode,
    fallback_reason: str | None,
    translation_applied: bool,
    protected_altered: bool,
    safety_error: TranslationSafetyError | None,
) -> float:
    if protected_altered:
        return 0.0
    if safety_error:
        return 0.25
    confidence = max(0.0, 1.0 - semantic_drift_score)
    if translation_applied:
        confidence = min(confidence, {"shadow": 0.92, "assisted": 0.85, "enforce": 0.80}.get(mode, 0.85))
    if fallback_reason and fallback_reason not in {"translation_skipped", "source_already_target_language"}:
        confidence = min(confidence, 0.65)
    return round(confidence, 4)


def _normalize_mode(value: object) -> I18NMode:
    mode = str(value or "shadow").lower()
    if mode in {"off", "shadow", "assisted", "enforce"}:
        return mode  # type: ignore[return-value]
    return "shadow"


def _source_matches_target(source_language: str, source_variant: str, target_language: str) -> bool:
    target = target_language.strip().lower()
    source = source_language.strip().lower()
    variant = source_variant.strip().lower()
    if target in {"en", "eng", "eng_latn", "english"}:
        return source == "en" or variant == "en"
    if target in {"pt", "pt-pt", "pt_pt", "por", "por_latn", "portuguese"}:
        return source == "pt" or variant.startswith("pt")
    return False


def _drift_risk(score: float, *, safety_error: TranslationSafetyError | None) -> str:
    if safety_error or score >= 0.90:
        return "blocked"
    if score >= 0.50:
        return "high"
    if score >= 0.20:
        return "medium"
    if score > 0.05:
        return "low"
    return "none"


def _confidence_reason(
    *,
    original: str,
    normalized: str,
    mode: I18NMode,
    translation_applied: bool,
    fallback_reason: str | None,
    safety_error: TranslationSafetyError | None,
) -> str:
    if safety_error:
        return f"unsafe_translation:{safety_error.code}"
    if translation_applied:
        return f"{mode}_translation_guardrails"
    if fallback_reason and fallback_reason != "translation_skipped":
        return f"fallback:{fallback_reason}"
    if normalized.strip() != original.strip():
        return "normalization_changed_text"
    return "identity_or_translation_skipped"
