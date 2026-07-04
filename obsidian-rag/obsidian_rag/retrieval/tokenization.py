"""Centralized tokenization utilities and stop-word lists for retrieval."""

from __future__ import annotations

import unicodedata

PT_STOP_WORDS = frozenset({
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "eu", "já", "lhe", "me", "meu", "minha", "na", "nas", "no", "nos", "o",
    "os", "para", "pela", "pelo", "por", "que", "se", "seu", "são", "sua",
    "te", "tem", "tenho", "ter", "um", "uma", "uns", "vai", "à", "é", "há",
    "isso", "isto", "mais", "mas", "muito", "não", "ou", "ser", "como",
    "quando", "quais", "qual", "quem", "quero", "ver", "lista", "todos",
    "todas", "algum", "cada", "esse", "essa", "esses",
})

EN_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "could", "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "him", "his", "how", "if", "in", "into", "is", "it", "its",
    "may", "me", "might", "must", "my", "not", "of", "on", "or", "our",
    "shall", "she", "should", "so", "some", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "to", "too",
    "us", "very", "was", "we", "were", "what", "when", "where", "which",
    "who", "whom", "why", "will", "with", "would", "you", "your",
})

STOP_WORDS = PT_STOP_WORDS | EN_STOP_WORDS


def remove_stop_words(tokens: list[str]) -> list[str]:
    """Filter out stop words from a token list."""
    return [t for t in tokens if t not in STOP_WORDS]


def extract_keywords(text: str) -> str:
    """Extract meaningful keywords from text, removing stop words and short tokens."""
    normalized = unicodedata.normalize("NFC", text)
    words = [w.strip(".,!?:;\"'()[]") for w in normalized.lower().split()]
    keywords = [w for w in words if w and w not in STOP_WORDS and len(w) > 2]
    return " ".join(keywords) if keywords else text
