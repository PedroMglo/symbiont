"""Conservative PT-PT spellcheck with optional lightweight backends."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from models import SpellcheckResponse, SpellcheckToken


_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]+")
_UNSAFE_CHARS = set('/\\._-:@${}[]|="\'`')
_COMMON_TYPOS = {
    "uqal": "qual",
    "tradorutor": "tradutor",
}
_TECHNICAL_WORDS = {
    "api", "bash", "cache", "cli", "container", "cuda", "docker", "fastapi",
    "gpu", "http", "https", "json", "linux", "llm", "ollama", "python",
    "qdrant", "rag", "redis", "toml", "url", "vram", "yaml",
}
_KNOWN_SAFE_WORDS = {
    "abre", "colocar", "com", "corre", "de", "diz", "forma", "melhor",
    "me", "memória", "muita", "não", "o", "qual", "usar", "um", "uma",
    "está", "esta", "este",
}


@dataclass(frozen=True)
class SpellcheckConfig:
    dictionary_path: Path
    autocorrect_enabled: bool = True
    autocorrect_threshold: float = 0.92
    max_edit_distance: int = 2


class Spellchecker:
    def __init__(self, config: SpellcheckConfig):
        self.config = config
        self.words: set[str] = set()
        self.by_length: dict[int, list[str]] = defaultdict(list)
        self.by_length_first: dict[tuple[int, str], list[str]] = defaultdict(list)
        self.loaded = False
        self.load()

    def load(self) -> None:
        path = self.config.dictionary_path
        if not path.exists():
            self.loaded = False
            return
        words: set[str] = set()
        for idx, raw_line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
            if idx == 0 and raw_line.strip().isdigit():
                continue
            entry = raw_line.split("\t", 1)[0].split("/", 1)[0].strip().lower()
            if not entry or not _WORD_RE.fullmatch(entry):
                continue
            words.add(entry)
        words.update(_TECHNICAL_WORDS)
        words.update(_KNOWN_SAFE_WORDS)
        words.update(_COMMON_TYPOS.values())
        self.words = words
        self.by_length = defaultdict(list)
        self.by_length_first = defaultdict(list)
        for word in words:
            self.by_length[len(word)].append(word)
            self.by_length_first[(len(word), word[:1])].append(word)
        self.loaded = bool(words)

    def check_text(self, text: str, *, apply_autocorrect: bool = True) -> tuple[str, SpellcheckResponse, bool, float]:
        start = time.perf_counter()
        changed = False
        tokens: list[SpellcheckToken] = []

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            token = match.group(0)
            result = self.check_token(token)
            autocorrect = (
                apply_autocorrect
                and self.config.autocorrect_enabled
                and result.correction is not None
                and result.autocorrected
            )
            tokens.append(result)
            if autocorrect:
                changed = True
                return _match_case(token, result.correction or token)
            return token

        corrected = _WORD_RE.sub(replace, text)
        latency_ms = (time.perf_counter() - start) * 1000
        return corrected, SpellcheckResponse(tokens=tokens), changed, latency_ms

    def check_token(self, token: str) -> SpellcheckToken:
        lowered = token.lower()
        if lowered in _COMMON_TYPOS:
            suggestion = _COMMON_TYPOS[lowered]
            return SpellcheckToken(
                token=token,
                ok=False,
                suggestions=[suggestion],
                autocorrected=True,
                correction=suggestion,
            )
        if not self.loaded or not _is_autocorrectable(token):
            return SpellcheckToken(token=token, ok=True, suggestions=[], autocorrected=False)
        if lowered in self.words or lowered in _KNOWN_SAFE_WORDS:
            return SpellcheckToken(token=token, ok=True, suggestions=[], autocorrected=False)

        suggestions = self.suggest(lowered)
        correction = suggestions[0] if suggestions else None
        autocorrected = False
        if correction:
            distance = _damerau_levenshtein(lowered, correction, self.config.max_edit_distance + 1)
            confidence = _confidence(lowered, correction, distance)
            autocorrected = confidence >= self.config.autocorrect_threshold
        return SpellcheckToken(
            token=token,
            ok=False,
            suggestions=suggestions[:5],
            autocorrected=autocorrected,
            correction=correction if autocorrected else None,
        )

    def suggest(self, lowered: str) -> list[str]:
        best: list[tuple[float, str]] = []
        lengths = range(max(1, len(lowered) - self.config.max_edit_distance), len(lowered) + self.config.max_edit_distance + 1)
        first = lowered[:1]
        for length in lengths:
            candidates = self.by_length_first.get((length, first), [])
            if not candidates and len(lowered) <= 5:
                candidates = self.by_length.get(length, [])
            if len(candidates) > 400:
                continue
            for candidate in candidates:
                if abs(len(candidate) - len(lowered)) > self.config.max_edit_distance:
                    continue
                distance = _damerau_levenshtein(lowered, candidate, self.config.max_edit_distance + 1)
                if distance > self.config.max_edit_distance:
                    continue
                score = _confidence(lowered, candidate, distance)
                if score >= 0.72:
                    best.append((score, candidate))
        best.sort(key=lambda item: (-item[0], item[1]))
        return [candidate for _, candidate in best[:5]]


def _is_autocorrectable(token: str) -> bool:
    if len(token) <= 3 or len(token) > 24:
        return False
    if any(char in _UNSAFE_CHARS for char in token):
        return False
    if not token.islower() and not token.istitle():
        return False
    return token.isalpha()


def _match_case(source: str, target: str) -> str:
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _confidence(source: str, candidate: str, distance: int) -> float:
    if distance <= 0:
        return 1.0
    return max(0.0, 1.0 - (distance / max(len(source), len(candidate), 1) / 4.0))


def _damerau_levenshtein(a: str, b: str, cutoff: int) -> int:
    if abs(len(a) - len(b)) >= cutoff:
        return cutoff
    previous = list(range(len(b) + 1))
    current = [0] * (len(b) + 1)
    previous_previous: list[int] | None = None
    for i, ca in enumerate(a, start=1):
        current[0] = i
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if (
                previous_previous is not None
                and i > 1
                and j > 1
                and ca == b[j - 2]
                and a[i - 2] == cb
            ):
                value = min(value, previous_previous[j - 2] + 1)
            current[j] = value
            row_min = min(row_min, value)
        if row_min >= cutoff:
            return cutoff
        previous_previous, previous, current = previous, current, previous
    return previous[len(b)]
