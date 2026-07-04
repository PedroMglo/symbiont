"""Signal extraction — cheap pre-LLM analysis of incoming requests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orchestrator.prewarming.feature_catalog import FeatureCatalog


@dataclass
class RequestSignals:
    """Extracted signals from a raw request (before any LLM processing)."""

    has_file: bool = False
    file_extensions: list[str] = field(default_factory=list)
    keywords_found: list[str] = field(default_factory=list)
    has_url: bool = False
    has_path: bool = False
    has_code_block: bool = False
    pattern_matches: dict[str, list[str]] = field(default_factory=dict)  # feature_id → [reasons]


# Compiled once at module level
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:^|[\s\"'])([~/][\w/.\-]+)", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_FILE_EXT_RE = re.compile(r"\.(\w{1,10})(?:\s|$|[\"',;:\)])")


class SignalExtractor:
    """Extracts cheap signals from a request using patterns from the feature catalog."""

    def __init__(self, catalog: FeatureCatalog) -> None:
        self._catalog = catalog

    def extract(self, query: str, *, file_names: list[str] | None = None) -> RequestSignals:
        """Extract all cheap signals from the query text and optional file metadata."""
        signals = RequestSignals()
        q_lower = query.lower()
        words = {w.strip(".,!?:;\"'()[]{}") for w in q_lower.split()}
        words.discard("")

        # --- File detection ---
        if file_names:
            signals.has_file = True
            for fname in file_names:
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                if ext:
                    signals.file_extensions.append(ext)

        # Detect file extensions mentioned in text
        for m in _FILE_EXT_RE.finditer(q_lower):
            ext = m.group(1).lower()
            if ext not in signals.file_extensions:
                signals.file_extensions.append(ext)

        # --- URL / Path / Code block detection ---
        signals.has_url = bool(_URL_RE.search(query))
        signals.has_path = bool(_PATH_RE.search(query))
        signals.has_code_block = bool(_CODE_BLOCK_RE.search(query))

        # --- Keyword matching against catalog ---
        for word in words:
            feature_ids = self._catalog.get_by_keyword(word)
            if feature_ids:
                signals.keywords_found.append(word)

        # --- File extension matching ---
        for ext in signals.file_extensions:
            feature_ids = self._catalog.get_by_extension(ext)
            for fid in feature_ids:
                if self._catalog.is_prewarm_target(fid):
                    signals.pattern_matches.setdefault(fid, []).append(f"file_ext:{ext}")

        # --- Pattern matching against catalog ---
        for fid in self._catalog.prewarm_target_ids:
            for pattern in self._catalog.get_patterns(fid):
                if pattern.search(q_lower):
                    signals.pattern_matches.setdefault(fid, []).append(f"pattern:{pattern.pattern}")

        return signals
