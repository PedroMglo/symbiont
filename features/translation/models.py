"""Pydantic models for the translation feature API."""

from __future__ import annotations

from typing import Literal

from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


I18NMode = Literal["off", "shadow", "assisted", "enforce"]
LanguageContextVersion = Literal["translation.language_context.v1"]
DriftRisk = Literal["none", "low", "medium", "high", "blocked"]
TranslationSafetyErrorCode = Literal[
    "protected_spans_altered",
    "empty_translation",
    "unsafe_translation",
]
TranslationSafetyStage = Literal["cache", "translation", "output_validation"]


class HealthResponse(ServiceHealthResponse):
    status: str = "ok"
    dictionary_loaded: bool = False
    translator_loaded: bool = False
    mode: I18NMode = "shadow"


class NormalizeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source_language_hint: str | None = "pt-PT"
    target_language: str = "en"
    mode: I18NMode | None = None
    max_latency_ms: int = 250
    protect_spans: bool = True
    spellcheck: bool = True
    translate: bool = True
    return_debug: bool = False


class LanguageTransformation(BaseModel):
    name: str
    applied: bool
    reason: str | None = None


class LanguageWarning(BaseModel):
    code: str
    detail: str | None = None


class ProtectedSpanAudit(BaseModel):
    count: int = 0
    before_hash: str = ""
    after_hash: str = ""
    hashes_match: bool = True
    altered: bool = False
    missing_kinds: list[str] = Field(default_factory=list)


class TranslationSafetyError(BaseModel):
    code: TranslationSafetyErrorCode
    message: str
    stage: TranslationSafetyStage
    severity: Literal["warning", "blocking"] = "blocking"
    fallback_applied: bool = True
    protected_span_kinds: list[str] = Field(default_factory=list)


class SemanticQuality(BaseModel):
    mode: I18NMode = "shadow"
    semantic_drift_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    drift_risk: DriftRisk = "none"
    assessed_by: Literal["deterministic_guardrails"] = "deterministic_guardrails"
    confidence_reason: str = ""


class LanguageContext(BaseModel):
    contract_version: LanguageContextVersion = "translation.language_context.v1"
    mode: I18NMode = "shadow"
    original_query: str
    normalized_query: str
    working_query: str
    source_language: str = "unknown"
    source_variant: str = "unknown"
    target_language: str = "en"
    transformations: list[LanguageTransformation] = Field(default_factory=list)
    warnings: list[LanguageWarning] = Field(default_factory=list)
    protected_spans: ProtectedSpanAudit = Field(default_factory=ProtectedSpanAudit)
    fallback_used: bool = False
    fallback_reason: str | None = None
    translation_applied: bool = False
    cache_hit: bool = False
    semantic_drift_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    translation_safe: bool = True
    quality: SemanticQuality
    safety_error: TranslationSafetyError | None = None


class NormalizeResponse(BaseModel):
    original: str
    normalized: str
    translated: str
    working_query: str | None = None
    mode: I18NMode = "shadow"
    source_language: str = "unknown"
    source_variant: str = "unknown"
    target_language: str = "en"
    protected_spans_count: int = 0
    spellcheck_applied: bool = False
    glossary_applied: bool = False
    translation_applied: bool = False
    cache_hit: bool = False
    latency_ms: float = 0.0
    fallback_used: bool = False
    fallback_reason: str | None = None
    semantic_drift_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    translation_safe: bool = True
    quality: SemanticQuality
    safety_error: TranslationSafetyError | None = None
    warnings: list[LanguageWarning] = Field(default_factory=list)
    language_context: LanguageContext
    debug: dict | None = None


class LintPTPTRequest(BaseModel):
    text: str = Field(..., min_length=1)
    protect_spans: bool = True


class LintChange(BaseModel):
    from_text: str = Field(..., alias="from")
    to: str
    reason: str


class LintPTPTResponse(BaseModel):
    original: str
    corrected: str
    changes: list[LintChange] = Field(default_factory=list)
    latency_ms: float = 0.0


class SpellcheckRequest(BaseModel):
    text: str = Field(..., min_length=1)
    variant: str = "pt-PT"


class SpellcheckToken(BaseModel):
    token: str
    ok: bool
    suggestions: list[str] = Field(default_factory=list)
    autocorrected: bool = False
    correction: str | None = None


class SpellcheckResponse(BaseModel):
    tokens: list[SpellcheckToken] = Field(default_factory=list)
