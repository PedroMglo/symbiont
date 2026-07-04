"""Optional cross-encoder reranker via Ollama.

Uses a fast LLM to score relevance of retrieved chunks against the
original query.  Candidates are scored in parallel via ThreadPoolExecutor
(I/O-bound HTTP calls to Ollama).  Disabled by default (adds latency).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from rag_config import settings

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


log = logging.getLogger("obsidian_rag")

_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)

_RERANK_PROMPT = (
    _prompt("rerank.md")
)

_SCORE_PATTERN = re.compile(r"\b(\d{1,2})\b")


def rerank_chunks(
    chunks: list[tuple[str, dict, float]],
    query: str,
) -> list[tuple[str, dict, float]]:
    """Rerank chunks using LLM scoring.

    Args:
        chunks: list of (doc, metadata, vector_score)
        query: original user query

    Returns:
        Reranked list, filtered by min_score, sorted by reranker score.
    """
    cfg = settings.reranker
    if not cfg.enabled or not chunks:
        return chunks

    # Only evaluate top candidates
    candidates = chunks[:cfg.top_k_candidates]
    scored: list[tuple[str, dict, float]] = []

    # Parallel scoring — each _score_chunk is an I/O-bound HTTP call
    max_workers = min(3, len(candidates))

    def _evaluate(item: tuple[str, dict, float]) -> tuple[str, dict, float] | None:
        doc, meta, vec_score = item
        display = meta.get("display_text", doc)
        text_for_scoring = display[:1500] if len(display) > 1500 else display
        score = _score_chunk(query, text_for_scoring, cfg.model)
        if score is not None and score >= cfg.min_score:
            combined = 0.6 * score + 0.4 * vec_score
            return (doc, meta, combined)
        if score is None:
            # LLM scoring failed — keep with original score
            return (doc, meta, vec_score)
        return None  # below min_score

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_evaluate, c): c for c in candidates}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    scored.append(result)
            except Exception as exc:
                # On unexpected error, keep chunk with original vector score
                doc, meta, vec_score = futures[future]
                log.debug("Reranker: parallel scoring error: %s", exc)
                scored.append((doc, meta, vec_score))

    scored.sort(key=lambda x: x[2], reverse=True)
    log.info("Reranker: %d/%d chunks passed (min_score=%.1f)", len(scored), len(candidates), cfg.min_score)
    return scored


@lru_cache(maxsize=256)
def _score_chunk(query: str, chunk_text: str, model: str) -> float | None:
    """Score a single chunk's relevance (0.0–1.0)."""
    from llm import get_llm_client

    prompt = _RERANK_PROMPT.format(query=query, chunk=chunk_text)
    try:
        raw = get_llm_client().generate(
            prompt,
            model,
            temperature=0.0,
            max_tokens=8,
            timeout=10.0,
        )
        # _THINK_PATTERN already stripped by LLMClient
        match = _SCORE_PATTERN.search(raw)
        if match:
            val = int(match.group(1))
            return min(val, 10) / 10.0
    except Exception as exc:
        log.debug("Reranker scoring failed: %s", exc)

    return None
