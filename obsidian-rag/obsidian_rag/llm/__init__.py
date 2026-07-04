"""LLM — backend-agnostic LLM generation client.

Exports a process-wide singleton via ``get_llm_client()`` so that
the router, reranker, and graph enrichment share the same instance.
"""

import threading

from obsidian_rag.llm.base import LLMClient, create_llm_client

__all__ = ["LLMClient", "create_llm_client", "get_llm_client"]

_lock = threading.Lock()
_client: LLMClient | None = None


def get_llm_client(*, _override: LLMClient | None = None) -> LLMClient:
    """Return the process-wide LLMClient singleton.

    Args:
        _override: inject a client for testing (bypasses singleton).
    """
    global _client
    if _override is not None:
        return _override
    if _client is None:
        with _lock:
            if _client is None:
                _client = create_llm_client()
    return _client


def _reset_llm_client() -> None:
    """Reset singleton — for testing only."""
    global _client
    with _lock:
        _client = None
