"""Cross-encoder reranker using sentence-transformers.

Uses BAAI/bge-reranker-v2-m3 (multilingual, ~560MB) for fast batch scoring.
Falls back to LLM-based reranker if sentence-transformers is not installed.

Typical latency: ~200ms for 20 chunks (vs ~30s with LLM-based reranker).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from obsidian_rag.config import settings

if TYPE_CHECKING:
    pass

log = logging.getLogger("obsidian_rag")

# Default cross-encoder model (multilingual, good for PT+EN)
_DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"

# Singleton to avoid reloading the model on every call
_cross_encoder = None
_model_load_attempted = False


def _get_cross_encoder():
    """Lazy-load cross-encoder model (singleton)."""
    global _cross_encoder, _model_load_attempted
    if _model_load_attempted:
        return _cross_encoder
    _model_load_attempted = True
    try:
        from sentence_transformers import CrossEncoder
        model_name = settings.reranker.cross_encoder_model or _DEFAULT_MODEL
        log.info("Loading cross-encoder model: %s", model_name)
        _cross_encoder = CrossEncoder(model_name, max_length=512, device="cpu")
        log.info("Cross-encoder loaded successfully")
    except ImportError:
        log.warning(
            "sentence-transformers not installed. "
            "Install with: pip install 'obsidian-rag[reranker]'"
        )
    except Exception as exc:
        log.warning("Failed to load cross-encoder: %s", exc)
    return _cross_encoder


def rerank_with_cross_encoder(
    chunks: list[tuple[str, dict, float]],
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
) -> list[tuple[str, dict, float]]:
    """Rerank chunks using cross-encoder scoring.

    Args:
        chunks: list of (doc_text, metadata, vector_score)
        query: original user query
        top_k: number of results to return after reranking
        min_score: minimum cross-encoder score (0-1 range)

    Returns:
        Reranked and filtered list, sorted by combined score.
        Falls back to input order if cross-encoder unavailable.
    """
    if not chunks:
        return chunks

    encoder = _get_cross_encoder()
    if encoder is None:
        # Fallback: return top_k by vector score
        return chunks[:top_k]

    # Prepare pairs for cross-encoder
    pairs: list[tuple[str, str]] = []
    for doc, meta, _score in chunks:
        # Use display_text if available (richer context), else raw doc
        text = meta.get("display_text", doc)
        # Truncate to avoid exceeding model max_length
        text = text[:1500] if len(text) > 1500 else text
        pairs.append((query, text))

    # Batch scoring — much faster than individual calls
    try:
        scores = encoder.predict(pairs, show_progress_bar=False)
    except Exception as exc:
        log.warning("Cross-encoder scoring failed: %s", exc)
        return chunks[:top_k]

    # Combine cross-encoder score with vector score
    # cross-encoder output is logit — convert to 0-1 via sigmoid for interpretability
    import math
    scored: list[tuple[str, dict, float, float]] = []
    for i, (doc, meta, vec_score) in enumerate(chunks):
        ce_score = 1.0 / (1.0 + math.exp(-float(scores[i])))  # sigmoid
        # Weighted combination: cross-encoder is more precise than vector similarity
        combined = 0.7 * ce_score + 0.3 * vec_score
        if ce_score >= min_score:
            scored.append((doc, meta, combined, ce_score))

    # Sort by combined score descending
    scored.sort(key=lambda x: x[2], reverse=True)

    log.info(
        "CrossEncoder rerank: %d/%d passed (top_k=%d, min=%.2f, best_ce=%.3f)",
        len(scored), len(chunks), top_k,
        min_score,
        scored[0][3] if scored else 0.0,
    )

    # Return top_k with combined score
    return [(doc, meta, combined) for doc, meta, combined, _ce in scored[:top_k]]


def is_available() -> bool:
    """Check if cross-encoder is available (model loaded)."""
    return _get_cross_encoder() is not None
