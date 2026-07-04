"""Prompt injection detection — multi-layer defense.

Layer 1: Fast regex scan (< 1ms) for known injection patterns.
Layer 2: Heuristic scoring for structural anomalies (only if Layer 1 flags).

Fail-safe: any internal error returns action="allow" and emits a warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("ignore_instructions", re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"), 0.9),
    ("new_identity", re.compile(r"(?i)you\s+are\s+now\s+"), 0.85),
    ("system_role_inject", re.compile(r"(?i)^system\s*:", re.MULTILINE), 0.8),
    ("chatml_marker", re.compile(r"<\|im_start\|>"), 0.95),
    ("llama_inst", re.compile(r"\[INST\]"), 0.85),
    ("markdown_role", re.compile(r"(?i)###\s*(System|Human|Assistant)\s*\n"), 0.8),
    ("forget_above", re.compile(r"(?i)forget\s+(everything|all)\s+(above|before)"), 0.85),
    ("new_instructions", re.compile(r"(?i)new\s+instructions?\s*:"), 0.8),
    ("do_anything_now", re.compile(r"(?i)DAN\s*mode|do\s+anything\s+now"), 0.9),
    ("jailbreak_prompt", re.compile(r"(?i)jailbreak|bypass\s+(safety|filter|content)"), 0.75),
]

_STRUCTURAL_INDICATORS: list[tuple[str, re.Pattern[str], float]] = [
    ("role_separator", re.compile(r"(?i)(human|user|assistant|system)\s*:\s*\n"), 0.3),
    ("xml_tag_system", re.compile(r"</?system>"), 0.4),
    ("base64_block", re.compile(r"[A-Za-z0-9+/]{50,}={0,2}"), 0.2),
]


@dataclass(frozen=True)
class ScanResult:
    is_suspicious: bool
    confidence: float
    pattern_matched: str | None
    action: str  # "allow" | "flag" | "block"
    scan_layer: str  # "regex" | "heuristic" | "none"
    scan_point: str  # "input" | "context" | "output"


def _determine_action(confidence: float, block_threshold: float) -> str:
    if confidence >= block_threshold:
        return "block"
    if confidence >= 0.5:
        return "flag"
    return "allow"


class InjectionScanner:
    """Multi-layer prompt injection detection."""

    def __init__(self, block_threshold: float = 0.8):
        self._block_threshold = block_threshold

    def scan(self, text: str, *, scan_point: str = "input") -> ScanResult:
        """Run injection detection on text. Fast regex first, heuristic if flagged."""
        try:
            return self._scan_internal(text, scan_point=scan_point)
        except Exception as exc:
            log.warning("InjectionScanner error (fail-safe allow): %s", exc)
            return ScanResult(
                is_suspicious=False,
                confidence=0.0,
                pattern_matched=None,
                action="allow",
                scan_layer="none",
                scan_point=scan_point,
            )

    def _scan_internal(self, text: str, *, scan_point: str) -> ScanResult:
        if not text:
            return ScanResult(False, 0.0, None, "allow", "none", scan_point)

        # Layer 1: fast regex
        for name, pattern, confidence in _INJECTION_PATTERNS:
            if pattern.search(text):
                action = _determine_action(confidence, self._block_threshold)
                result = ScanResult(
                    is_suspicious=True,
                    confidence=confidence,
                    pattern_matched=name,
                    action=action,
                    scan_layer="regex",
                    scan_point=scan_point,
                )
                if action != "block":
                    # Layer 2: boost confidence with structural indicators
                    return self._heuristic_boost(text, result)
                return result

        # Layer 2: structural heuristic (only lightweight patterns)
        total_score = 0.0
        matched_indicators: list[str] = []
        for name, pattern, weight in _STRUCTURAL_INDICATORS:
            if pattern.search(text):
                total_score += weight
                matched_indicators.append(name)

        if total_score >= 0.5:
            action = _determine_action(total_score, self._block_threshold)
            return ScanResult(
                is_suspicious=True,
                confidence=min(total_score, 1.0),
                pattern_matched="+".join(matched_indicators),
                action=action,
                scan_layer="heuristic",
                scan_point=scan_point,
            )

        return ScanResult(False, 0.0, None, "allow", "none", scan_point)

    def _heuristic_boost(self, text: str, base: ScanResult) -> ScanResult:
        """Boost confidence if structural indicators also present."""
        extra = 0.0
        for _, pattern, weight in _STRUCTURAL_INDICATORS:
            if pattern.search(text):
                extra += weight * 0.5

        if extra > 0:
            new_conf = min(base.confidence + extra, 1.0)
            new_action = _determine_action(new_conf, self._block_threshold)
            return ScanResult(
                is_suspicious=True,
                confidence=new_conf,
                pattern_matched=base.pattern_matched,
                action=new_action,
                scan_layer="heuristic",
                scan_point=base.scan_point,
            )
        return base

    def scan_input(self, text: str) -> ScanResult:
        return self.scan(text, scan_point="input")

    def scan_context(self, text: str) -> ScanResult:
        return self.scan(text, scan_point="context")

    def scan_output(self, text: str) -> ScanResult:
        return self.scan(text, scan_point="output")
