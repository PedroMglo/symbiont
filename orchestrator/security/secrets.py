"""Secrets detection — scan text for potential secrets before they reach LLM prompts.

Detects API keys, tokens, passwords, private keys, and other sensitive material.
Used in the active pipeline (between context gathering and prompt building).
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.security._patterns import SECRET_PATTERNS


@dataclass(frozen=True)
class SecretMatch:
    pattern_name: str
    start: int
    end: int
    preview: str  # first 4 chars + "***"


class SecretsScanner:
    """Detect and redact secrets in text content."""

    def scan(self, text: str) -> list[SecretMatch]:
        """Scan text for potential secrets. Returns list of matches."""
        if not text:
            return []

        matches: list[SecretMatch] = []
        for name, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                matched_text = m.group(0)
                preview = matched_text[:4] + "***" if len(matched_text) > 4 else "***"
                matches.append(SecretMatch(
                    pattern_name=name,
                    start=m.start(),
                    end=m.end(),
                    preview=preview,
                ))
        return matches

    def redact(self, text: str) -> str:
        """Replace detected secrets with [REDACTED:pattern_name] markers."""
        if not text:
            return text

        replacements: list[tuple[int, int, str]] = []
        for name, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                replacements.append((m.start(), m.end(), f"[REDACTED:{name}]"))

        if not replacements:
            return text

        # Sort by position (reverse) to replace from end to start
        replacements.sort(key=lambda x: x[0], reverse=True)

        # Remove overlapping replacements (keep longest)
        filtered: list[tuple[int, int, str]] = []
        for start, end, repl in replacements:
            if not filtered or end <= filtered[-1][0]:
                filtered.append((start, end, repl))

        result = text
        for start, end, repl in filtered:
            result = result[:start] + repl + result[end:]

        return result

    def has_secrets(self, text: str) -> bool:
        """Quick check — returns True if any secret pattern matches."""
        if not text:
            return False
        for _, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return True
        return False
