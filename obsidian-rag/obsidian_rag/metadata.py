"""Metadata key constants used across chunking, indexing, and retrieval."""

from __future__ import annotations

import hashlib
from pathlib import Path

SOURCE_ID = "source_id"
SOURCE_PATH = "source_path"
SOURCE_TYPE = "source_type"
SOURCE_NAME = "source_name"
NOTE_TITLE = "note_title"
SECTION_HEADER = "section_header"
CHUNK_INDEX = "chunk_index"
DISPLAY_TEXT = "display_text"
REPO_NAME = "repo_name"
SYMBOL_TYPE = "symbol_type"


def stable_source_id(name: str, root: str | Path) -> str:
    """Return a deterministic source namespace for one indexed root."""
    try:
        root_text = str(Path(root).expanduser().resolve())
    except OSError:
        root_text = str(root)
    digest = hashlib.sha256(root_text.encode("utf-8")).hexdigest()[:16]
    return f"{name}:{digest}"
