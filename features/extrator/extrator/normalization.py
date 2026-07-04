"""Text normalization helpers."""

from __future__ import annotations

import re


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def clean_text(text: str) -> str:
    value = normalize_newlines(text)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def title_from_markdown(markdown: str, fallback: str) -> str:
    for line in normalize_newlines(markdown).splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback
