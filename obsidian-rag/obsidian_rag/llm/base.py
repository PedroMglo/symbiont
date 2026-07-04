"""LLMClient protocol — backend-agnostic interface for LLM generation.

Every LLM backend implements ``LLMClient``
so that router, reranker, and other consumers never call httpx directly.

Usage::

    from obsidian_rag.llm import get_llm_client

    llm = get_llm_client()
    response = llm.generate("Summarize this text.", model="<model>")
    chat_resp = llm.chat([{"role": "user", "content": "Hello"}], model="<model>")
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    """Backend-agnostic LLM interface for text generation."""

    def generate(
        self,
        prompt: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float = 30.0,
    ) -> str:
        """Generate text from a prompt.

        Returns the generated text (with ``<think>`` blocks stripped).
        """
        ...

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float = 30.0,
    ) -> str:
        """Chat completion from a list of messages.

        Returns the assistant's response text (with ``<think>`` blocks stripped).
        """
        ...

    def health(self) -> bool:
        """Return *True* if the LLM backend is reachable."""
        ...


def create_llm_client(backend: str | None = None, **kwargs) -> LLMClient:
    """Instantiate the configured LLM backend.

    Args:
        backend: ``"ollama"``.  If *None*, defaults to ``"ollama"``.
        **kwargs: forwarded to the backend constructor.
    """
    if backend is None:
        backend = "ollama"

    backend = backend.lower().strip()

    if backend == "ollama":
        from obsidian_rag.llm.ollama import OllamaLLMClient
        return OllamaLLMClient(**kwargs)

    raise ValueError(f"Unknown LLM backend: {backend!r}  (expected 'ollama')")
