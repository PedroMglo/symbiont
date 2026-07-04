"""Reciprocal Rank Fusion — combines multiple ranked lists into one.

Implements RRF as described by Cormack et al. (2009):
  score(d) = Σ 1 / (k + rank_i(d))

where k is a constant (default 60) and rank_i(d) is the rank of document d
in the i-th ranked list. Documents appearing in multiple lists naturally
score higher.

Usage:
    from obsidian_rag.retrieval.fusion import rrf_fuse
    fused = rrf_fuse([dense_results, sparse_results], k=60)
"""

from __future__ import annotations

from dataclasses import replace

from obsidian_rag.retrieval.scored_chunk import ScoredChunk

_DEFAULT_K = 60


def rrf_fuse(
    ranked_lists: list[list[ScoredChunk]],
    *,
    k: int = _DEFAULT_K,
    weights: list[float] | None = None,
) -> list[ScoredChunk]:
    """Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    Args:
        ranked_lists: Each list is pre-sorted by score (descending).
        k: RRF constant — higher values flatten rank differences.
        weights: Optional per-list weight multipliers (default: equal weight).

    Returns:
        Single fused list sorted by combined RRF score (descending).
    """
    if not ranked_lists:
        return []

    if weights is None:
        weights = [1.0] * len(ranked_lists)

    # Accumulate RRF scores by dedup_key
    scores: dict[str, float] = {}
    chunks: dict[str, ScoredChunk] = {}

    for rank_list, weight in zip(ranked_lists, weights):
        for rank, chunk in enumerate(rank_list, start=1):
            key = chunk.dedup_key()
            rrf_score = weight / (k + rank)
            scores[key] = scores.get(key, 0.0) + rrf_score
            # Keep the chunk with the highest original score for metadata
            if key not in chunks or chunk.score > chunks[key].score:
                chunks[key] = chunk

    # Build fused result with RRF scores
    fused = []
    for key, rrf_score in scores.items():
        chunk = chunks[key]
        fused.append(replace(chunk, score=rrf_score))

    fused.sort(key=lambda c: c.score, reverse=True)
    return fused


def rrf_fuse_with_reranker(
    dense: list[ScoredChunk],
    sparse: list[ScoredChunk],
    reranked: list[ScoredChunk] | None = None,
    *,
    k: int = _DEFAULT_K,
    dense_weight: float = 1.0,
    sparse_weight: float = 0.8,
    reranker_weight: float = 1.5,
) -> list[ScoredChunk]:
    """Convenience wrapper: fuse dense + sparse + optional reranker results.

    Default weights favor the reranker (highest quality signal) while
    giving dense a slight edge over sparse.
    """
    lists = [dense, sparse]
    weights = [dense_weight, sparse_weight]

    if reranked:
        lists.append(reranked)
        weights.append(reranker_weight)

    return rrf_fuse(lists, k=k, weights=weights)
