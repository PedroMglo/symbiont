"""CAG — Cached Augmented Generation layer.

Provides pre-computed context packs that supplement RAG retrieval
with condensed, frequently-needed information (architecture, repo state,
config, system status, etc.).

Packs are generated eagerly after sync or lazily on first query,
and are validated for freshness (TTL, hash, mtime) before injection.
"""

from __future__ import annotations

import threading

from cag.store import PackStore

__all__ = ["PackStore", "get_pack_store"]

_lock = threading.Lock()
_store: PackStore | None = None


def get_pack_store() -> PackStore:
    """Return the process-wide PackStore singleton."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from rag_config import settings
                _store = PackStore(settings.cag.db_path)
    return _store


def _reset_pack_store() -> None:
    """Reset singleton — for testing only."""
    global _store
    with _lock:
        if _store is not None:
            _store.close()
        _store = None
