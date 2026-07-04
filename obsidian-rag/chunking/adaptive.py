"""Adaptive chunking — selects chunk parameters based on content type.

Different content types benefit from different chunk sizes:
  - Prose (notes): larger chunks (2000 chars) preserve narrative flow
  - Code: smaller chunks (1200 chars) align with function boundaries
  - Config/YAML: very small chunks (800 chars) for discrete entries
  - Documentation: medium chunks (1500 chars) balance detail and context

Usage:
    from chunking.adaptive import get_chunk_params
    params = get_chunk_params("python")  # or "markdown", "yaml", etc.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkParams:
    """Content-type-specific chunking parameters."""
    max_chars: int
    overlap_chars: int
    min_chars: int
    contextual_prefix: bool = True


# Default parameters per content category
_PARAMS: dict[str, ChunkParams] = {
    "markdown": ChunkParams(max_chars=2000, overlap_chars=200, min_chars=50),
    "python": ChunkParams(max_chars=1200, overlap_chars=100, min_chars=80),
    "code": ChunkParams(max_chars=1200, overlap_chars=100, min_chars=80),
    "config": ChunkParams(max_chars=800, overlap_chars=50, min_chars=30),
    "documentation": ChunkParams(max_chars=1500, overlap_chars=150, min_chars=50),
}

# File extension → content category mapping
_EXT_MAP: dict[str, str] = {
    ".md": "markdown",
    ".mdx": "markdown",
    ".txt": "documentation",
    ".rst": "documentation",
    ".py": "python",
    ".js": "code",
    ".ts": "code",
    ".jsx": "code",
    ".tsx": "code",
    ".java": "code",
    ".go": "code",
    ".rs": "code",
    ".c": "code",
    ".cpp": "code",
    ".rb": "code",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".json": "config",
    ".env": "config",
    ".sh": "code",
}


def get_chunk_params(content_type: str) -> ChunkParams:
    """Get chunking parameters for a content type or file extension.

    Args:
        content_type: Either a category name ("markdown", "python", "code", "config")
                      or a file extension (".py", ".md", etc.)
    """
    if content_type.startswith("."):
        category = _EXT_MAP.get(content_type, "markdown")
    else:
        category = content_type
    return _PARAMS.get(category, _PARAMS["markdown"])


def get_chunk_params_for_file(filename: str) -> ChunkParams:
    """Get chunking parameters based on file extension."""
    from pathlib import PurePath
    ext = PurePath(filename).suffix.lower()
    return get_chunk_params(ext)
