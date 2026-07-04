"""Editable flat YAML glossaries."""

from __future__ import annotations

import re
from pathlib import Path


def load_mapping(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    mapping: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip().strip("'\"")
        if key and value:
            mapping[key] = value
    return mapping


class Glossary:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.mapping = load_mapping(self.path)
        self.version = _version(self.mapping)

    def apply(self, text: str) -> tuple[str, bool]:
        changed = False
        result = text
        for source, target in sorted(self.mapping.items(), key=lambda item: len(item[0]), reverse=True):
            pattern = re.compile(rf"(?<![\w-]){re.escape(source)}(?![\w-])", re.IGNORECASE)

            def replace(match: re.Match[str]) -> str:
                nonlocal changed
                changed = True
                return _match_case(match.group(0), target)

            result = pattern.sub(replace, result)
        return result, changed


def _match_case(source: str, target: str) -> str:
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _version(mapping: dict[str, str]) -> str:
    import hashlib
    payload = "\n".join(f"{k}:{v}" for k, v in sorted(mapping.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
