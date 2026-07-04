"""Type-safe representation of a scored retrieval result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A chunk with its metadata and retrieval score."""

    text: str
    metadata: dict[str, Any]
    score: float

    @property
    def source_path(self) -> str:
        return self.metadata.get("source_path", "")

    @property
    def note_title(self) -> str:
        return self.metadata.get("note_title", "")

    @property
    def section_header(self) -> str:
        return self.metadata.get("section_header", "")

    @property
    def repo_name(self) -> str:
        return self.metadata.get("repo_name", "")

    @property
    def source_type(self) -> str:
        return self.metadata.get("source_type", "")

    @property
    def display_text(self) -> str:
        return self.metadata.get("display_text", self.text)

    @property
    def chunk_index(self) -> int:
        return self.metadata.get("chunk_index", 0)

    def dedup_key(self) -> str:
        """Composite key for deduplication."""
        source_id = self.metadata.get("source_id", self.metadata.get("source_name", ""))
        return f"{source_id}:{self.source_path}:{self.section_header}:{self.chunk_index}"

    def as_tuple(self) -> tuple[str, dict[str, Any], float]:
        """Tuple representation used by tuple-based rerankers."""
        return (self.text, self.metadata, self.score)

    @classmethod
    def from_tuple(cls, t: tuple[str, dict[str, Any], float]) -> ScoredChunk:
        """Create from a (text, metadata, score) tuple."""
        return cls(text=t[0], metadata=t[1], score=t[2])
