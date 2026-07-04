"""Protected span detection for the translation service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from pydantic import BaseModel


PLACEHOLDER_PREFIX = "__PROTECTED_"


class ProtectedSpan(BaseModel):
    index: int
    placeholder: str
    text: str
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class _Pattern:
    kind: str
    regex: re.Pattern[str]


_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern("markdown_code_block", re.compile(r"```[\s\S]*?```", re.MULTILINE)),
    _Pattern("inline_code", re.compile(r"`[^`\n]+`")),
    _Pattern("url", re.compile(r"\bhttps?://[^\s<>)\]]+", re.IGNORECASE)),
    _Pattern("email", re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    _Pattern("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)),
    _Pattern("hash", re.compile(r"\b[0-9a-f]{32,128}\b", re.IGNORECASE)),
    _Pattern("sql", re.compile(r"\bSELECT\b[\s\S]{0,500}?\bFROM\b[^\n;]*(?:;)?", re.IGNORECASE)),
    _Pattern("traceback", re.compile(r"Traceback \(most recent call last\):[\s\S]*?(?=\n\S|\Z)")),
    _Pattern("env_assignment", re.compile(r"\b[A-Z][A-Z0-9_]{1,}=[^\s]+")),
    _Pattern("env_var", re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")),
    _Pattern("shell_command", re.compile(r"(?m)(?:^\s*|(?<=[;&|])\s*)(?:docker\s+compose|docker|git|python3?|pip|uv|make|nvidia-smi|ollama|curl|wget|sudo|systemctl|ps|rg|sed|cat|ls|cd)\b[^\n]*")),
    _Pattern("windows_path", re.compile(r"\b[A-Za-z]:\\[^\s]+")),
    _Pattern("linux_path", re.compile(r"(?<!\w)(?:~|/|\.\.?/)[A-Za-z0-9_./~+@:%=-]+")),
    _Pattern("dotfile", re.compile(r"(?<!\w)\.[A-Za-z0-9_.-]+")),
    _Pattern("filename", re.compile(r"\b[A-Za-z0-9_.-]+\.(?:py|pyi|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|log|env|sh|zsh|bash|ini|cfg|sql|db|sqlite|csv|parquet|lock)\b", re.IGNORECASE)),
    _Pattern("ip_port", re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|(?:\d{1,3}\.){3}\d{1,3})(?::\d{2,5})?\b", re.IGNORECASE)),
    _Pattern("model_name", re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_.-]{1,}:[A-Za-z0-9][A-Za-z0-9_.-]*\b")),
    _Pattern("cuda_error", re.compile(r"\bCUDA out of memory\b", re.IGNORECASE)),
    _Pattern("technical_token", re.compile(r"\b[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+\b")),
    _Pattern("acronym", re.compile(r"\b[A-Z][A-Z0-9_]{1,}\b")),
    _Pattern("structured_line", re.compile(r"(?m)^[ \t]*[\w.-]+[ \t]*[:=][ \t]*[^,\n]+$")),
)


def _overlaps(start: int, end: int, spans: Iterable[tuple[int, int, str]]) -> bool:
    return any(start < existing_end and end > existing_start for existing_start, existing_end, _ in spans)


def find_protected_spans(text: str) -> list[ProtectedSpan]:
    ranges: list[tuple[int, int, str]] = []
    for pattern in _PATTERNS:
        for match in pattern.regex.finditer(text):
            start, end = match.span()
            if start == end or not _keep_match(pattern.kind, match.group(0)) or _overlaps(start, end, ranges):
                continue
            ranges.append((start, end, pattern.kind))
    ranges.sort(key=lambda item: item[0])
    return [
        ProtectedSpan(
            index=idx,
            placeholder=f"{PLACEHOLDER_PREFIX}{idx}__",
            text=text[start:end],
            start=start,
            end=end,
            kind=kind,
        )
        for idx, (start, end, kind) in enumerate(ranges)
    ]


def _keep_match(kind: str, value: str) -> bool:
    if kind == "acronym":
        return "_" in value or any(char.isdigit() for char in value)
    if kind != "technical_token":
        return True
    return (
        "/" in value
        or "_" in value
        or any(char.isdigit() for char in value)
        or any(char.isupper() for char in value)
    )


def protect_text(text: str) -> tuple[str, list[ProtectedSpan]]:
    spans = find_protected_spans(text)
    if not spans:
        return text, []
    parts: list[str] = []
    cursor = 0
    for span in spans:
        parts.append(text[cursor:span.start])
        parts.append(span.placeholder)
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts), spans


def restore_text(text: str, spans: Iterable[ProtectedSpan]) -> str:
    restored = text
    for span in spans:
        restored = restored.replace(span.placeholder, span.text)
    return restored


def protected_ratio(text: str, spans: Iterable[ProtectedSpan]) -> float:
    if not text:
        return 0.0
    protected_chars = sum(max(0, span.end - span.start) for span in spans)
    return min(1.0, protected_chars / len(text))


def spans_structure_hash(spans: Iterable[ProtectedSpan]) -> str:
    payload = "|".join(f"{span.kind}:{span.start}:{span.end}:{len(span.text)}" for span in spans)
    return sha256(payload.encode("utf-8")).hexdigest()


def spans_content_hash(spans: Iterable[ProtectedSpan]) -> str:
    payload = "|".join(
        f"{span.index}:{span.kind}:{sha256(span.text.encode('utf-8')).hexdigest()}" for span in spans
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def spans_output_content_hash(spans: Iterable[ProtectedSpan], text: str) -> tuple[str, list[str]]:
    entries: list[str] = []
    missing_kinds: list[str] = []
    cursor = 0
    for span in spans:
        location = text.find(span.text, cursor)
        if location < 0:
            entries.append(f"{span.index}:{span.kind}:missing")
            missing_kinds.append(span.kind)
            continue
        entries.append(f"{span.index}:{span.kind}:{sha256(span.text.encode('utf-8')).hexdigest()}")
        cursor = location + len(span.text)
    return sha256("|".join(entries).encode("utf-8")).hexdigest(), missing_kinds
