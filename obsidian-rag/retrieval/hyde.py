"""HyDE — Hypothetical Document Embeddings for query expansion.

Generates a hypothetical answer to the query using a fast LLM, then
embeds that answer alongside the original query. The average of both
embeddings tends to land closer to relevant documents in embedding space.

Enabled via config: [retrieval] hyde_enabled = true
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx
from context_governor import govern_chat_completion

from rag_config import settings

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_HYDE_PROMPT = (
    _prompt("hyde.md")
)


def generate_hypothetical_document(query: str) -> str | None:
    """Generate a hypothetical answer using Ollama (fast model).

    Returns None if generation fails or is disabled.
    """
    if not getattr(settings.retrieval, "hyde_enabled", False):
        return None

    model = getattr(settings.retrieval, "hyde_model", None)
    if not model:
        from registry import get_rag_model
        model = get_rag_model("router") or "qwen3:1.7b"

    prompt = _HYDE_PROMPT.format(query=query)
    start = time.time()

    try:
        text = govern_chat_completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            base_url=settings.ollama.base_url,
            temperature=0.0,
            max_tokens=_hyde_max_tokens(),
            timeout=10.0,
            phase="hyde",
            post=httpx.post,
        )
        text = text.strip()
        elapsed = (time.time() - start) * 1000
        log.debug("HyDE: generated %d chars in %.0fms", len(text), elapsed)
        return text if len(text) > 20 else None
    except httpx.HTTPError as exc:
        log.debug("HyDE: generation failed: %s", exc)
        return None


def _hyde_max_tokens() -> int:
    try:
        return max(32, int(os.environ.get("RAG_HYDE_MAX_TOKENS", "256")))
    except ValueError:
        return 256


def hyde_embed(query: str) -> list[float] | None:
    """Generate HyDE-augmented embedding: average of query + hypothetical doc.

    Returns None if HyDE is disabled or generation fails.
    """
    hypothetical = generate_hypothetical_document(query)
    if hypothetical is None:
        return None

    from embeddings import get_embedder
    embedder = get_embedder()

    try:
        embeddings = embedder.embed_texts([query, hypothetical])
        if len(embeddings) != 2:
            return None
        # Average the two embeddings
        dim = len(embeddings[0])
        averaged = [(embeddings[0][i] + embeddings[1][i]) / 2.0 for i in range(dim)]
        return averaged
    except Exception as exc:
        log.debug("HyDE: embedding failed: %s", exc)
        return None
