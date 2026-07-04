"""Small local heuristics for language and Portuguese variant detection."""

from __future__ import annotations

import re
from dataclasses import dataclass


_PT_WORDS = {
    "a", "ao", "aos", "as", "com", "como", "corrige", "diz", "e", "em",
    "está", "esta", "este", "eu", "faz", "ficheiro", "me", "memória",
    "não", "o", "os", "para", "pasta", "política", "por", "qual",
    "que", "quero", "se", "tenho", "um", "uma", "utilizador",
    "arquivo", "tela", "usuário", "usuario", "celular", "ônibus",
    "onibus", "baixar", "cadastro",
}
_PTPT_SIGNALS = {"ficheiro", "ecrã", "telemóvel", "rato", "autocarro", "descarregar", "registo"}
_PTBR_SIGNALS = {"arquivo", "tela", "usuário", "usuario", "celular", "ônibus", "onibus", "baixar", "cadastro"}
_EN_WORDS = {
    "a", "and", "build", "configuration", "explain", "file", "for",
    "in", "policy", "preserve", "preserving", "project", "protected",
    "spans", "the", "to", "with",
}


@dataclass(frozen=True)
class LanguageGuess:
    language: str
    variant: str
    confidence: float


def detect_language(text: str, *, default_variant_for_pt: str = "pt-PT") -> LanguageGuess:
    tokens = re.findall(r"[A-Za-zÀ-ÿ]+", text.lower())
    if not tokens:
        return LanguageGuess("unknown", "unknown", 0.0)
    pt_hits = sum(1 for token in tokens if token in _PT_WORDS or re.search(r"[ãõáéíóúâêôç]", token))
    pt_score = pt_hits / max(1, min(len(tokens), 24))
    en_hits = sum(1 for token in tokens if token in _EN_WORDS)
    en_score = en_hits / max(1, min(len(tokens), 24))
    if en_score >= 0.30 and en_score > pt_score:
        return LanguageGuess("en", "en", min(0.99, 0.45 + en_score))
    if pt_score < 0.12:
        return LanguageGuess("unknown", "unknown", min(0.5, pt_score))
    ptpt_hits = sum(1 for token in tokens if token in _PTPT_SIGNALS)
    ptbr_hits = sum(1 for token in tokens if token in _PTBR_SIGNALS)
    variant = default_variant_for_pt
    if ptbr_hits > ptpt_hits:
        variant = "pt-BR"
    elif ptpt_hits > 0:
        variant = "pt-PT"
    confidence = min(0.99, 0.45 + pt_score + (0.08 * (ptpt_hits + ptbr_hits)))
    return LanguageGuess("pt", variant, confidence)
