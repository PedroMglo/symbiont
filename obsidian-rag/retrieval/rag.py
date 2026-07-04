"""Multi-strategy RAG retrieval and context builder.

Domain-agnostic design: context is only injected when the router
determines it's needed AND retrieval quality passes the relevance gate.
"""

import logging
import math as _math
import time as _time
from pathlib import Path as _Path

from embeddings import get_embedder
from metadata import (
    NOTE_TITLE,
    REPO_NAME,
    SOURCE_PATH,
)
from prompts.templates import get_context_instruction
from rag_config import settings
from retrieval.budget import allocate_budget, truncate_chunks, truncate_text
from retrieval.intent import detect_intent_full
from retrieval.observe import QueryTrace
from retrieval.router import _GRAPH_PATTERNS, _GRAPH_SIGNALS, ContextMode
from retrieval.scored_chunk import ScoredChunk
from retrieval.sparse import BM25Vectorizer, tokenize
from retrieval.tokenization import STOP_WORDS, extract_keywords
from store import VectorStore, _reset_store, get_store

log = logging.getLogger("obsidian_rag")

_NAVIGATION_SECTIONS = frozenset({
    "Ficheiros", "Navegação", "Índice", "Navigation", "Files", "Conteúdo",
})


# === VectorStore singleton (delegated to obsidian_rag.store) ===


def _get_store(*, _override: VectorStore | None = None) -> VectorStore:
    """Proxy to the process-wide singleton in obsidian_rag.store."""
    return get_store(_override=_override)


def _reset_collections():
    """Reset singletons — for testing only."""
    _reset_store()


# === Collection-size cache (TTL 60 s) ===

_count_cache: dict[str, tuple[int, float]] = {}
_COUNT_TTL = 60.0


def _cached_count(store: VectorStore, collection: str) -> int:
    """Return collection size with 60 s TTL caching."""
    now = _time.monotonic()
    cached = _count_cache.get(collection)
    if cached is not None and now - cached[1] < _COUNT_TTL:
        return cached[0]
    try:
        n = store.count(collection=collection)
    except Exception:
        n = 0
    _count_cache[collection] = (n, now)
    return n


def _scale_k_by_size(base_k: int, collection_size: int) -> int:
    """Scale effective_k by log10(size / 1000) when collection > 1000."""
    if collection_size <= 1000:
        return base_k
    factor = 1.0 + _math.log10(collection_size / 1000)
    return max(3, min(30, int(base_k * factor)))


# === Helpers ===

def _is_navigation_chunk(chunk: ScoredChunk) -> bool:
    """True if this chunk is navigation/index with low info value."""
    section = chunk.section_header
    if section in _NAVIGATION_SECTIONS:
        return True
    lines = [ln.strip() for ln in chunk.text.strip().splitlines() if ln.strip()]
    if not lines:
        return True
    link_count = sum(1 for ln in lines if "[[" in ln and "]]" in ln and len(ln) < 80)
    return link_count / len(lines) > 0.6


def _estimate_complexity(query: str) -> str:
    """Estimate query complexity for adaptive top_k.

    Returns "simple" | "normal" | "complex".
    """
    q_lower = query.lower()
    words = [w for w in q_lower.split() if w.strip()]
    word_count = len(words)

    has_graph = bool({w.strip(".,!?") for w in words} & _GRAPH_SIGNALS) or any(p in q_lower for p in _GRAPH_PATTERNS)
    has_boolean = any(op in q_lower for op in (" and ", " or ", " not ", " && ", " || "))
    multi_question = q_lower.count("?") > 1

    if has_graph or has_boolean or multi_question or word_count > 8:
        return "complex"
    if word_count <= 3:
        return "simple"
    return "normal"


def _vector_search(store: VectorStore, query_text: str, n: int, *, collection: str = "obsidian_vault", filters: dict | None = None, trace: QueryTrace | None = None) -> list[ScoredChunk]:
    """Hybrid search: dense embedding + BM25 sparse (when available)."""
    embedding = get_embedder().get_query_embedding(query_text)

    sparse_query = _get_sparse_query(query_text, collection)

    if trace is not None:
        trace.dense_used = True
        trace.sparse_available = trace.sparse_available or _bm25_cache.get(collection) is not None
        if sparse_query is not None:
            trace.sparse_used = True

    results = store.query(
        embedding,
        n=min(n * 3, 50),
        collection=collection,
        filters=filters,
        sparse_query=sparse_query,
    )
    return [ScoredChunk(text=r.document, metadata=r.metadata, score=r.score) for r in results]


# === BM25 sparse query helper ===

_bm25_cache: dict[str, BM25Vectorizer | None] = {}


def _get_sparse_query(query_text: str, collection: str) -> dict | None:
    """Load BM25 model and transform query to sparse vector. Returns None if unavailable."""
    if collection not in _bm25_cache:
        try:
            model_path = _Path(settings.paths.data_dir) / "bm25" / f"{collection}.json"
            if model_path.exists():
                loaded = BM25Vectorizer.load(model_path)
                _bm25_cache[collection] = loaded
                log.info("BM25: loaded model for %r (vocab=%d)", collection, loaded.vocab_size)
            else:
                _bm25_cache[collection] = None
        except Exception as exc:
            log.debug("BM25: could not load model for %r: %s", collection, exc)
            _bm25_cache[collection] = None

    bm25 = _bm25_cache.get(collection)
    if bm25 is None:
        return None

    tokens = tokenize(query_text)
    tokens = [t for t in tokens if t not in STOP_WORDS]
    if not tokens:
        return None

    return bm25.transform(tokens)


# === Deduplication and filtering ===

def _deduplicate(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    """Deduplicate chunks by composite key, keeping highest score."""
    seen: dict[str, ScoredChunk] = {}
    for chunk in chunks:
        key = chunk.dedup_key()
        if key not in seen or chunk.score > seen[key].score:
            seen[key] = chunk
    return list(seen.values())


def _semantic_deduplicate(chunks: list[ScoredChunk], threshold: float | None = None) -> list[ScoredChunk]:
    """Remove near-duplicate chunks by embedding cosine similarity.

    Keeps the higher-scored chunk when two chunks have cosine sim > threshold.
    Uses the embedding provider to compute similarity between chunk texts.
    If *threshold* is None, reads ``settings.retrieval.semantic_dedup_threshold``.
    """
    if threshold is None:
        threshold = settings.retrieval.semantic_dedup_threshold
    if len(chunks) <= 1 or threshold >= 1.0:
        return chunks

    embedder = get_embedder()
    texts = [c.text for c in chunks]

    try:
        embed_fn = getattr(embedder, "embed_texts", None) or getattr(embedder, "embed_batch")
        embeddings = embed_fn(texts)
    except (AttributeError, Exception):
        return chunks

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0

    keep = [True] * len(chunks)
    for i in range(len(chunks)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(chunks)):
            if not keep[j]:
                continue
            if _cosine(embeddings[i], embeddings[j]) > threshold:
                keep[j] = False

    return [c for c, k in zip(chunks, keep) if k]


def _apply_threshold(
    chunks: list[ScoredChunk],
    score_threshold: float,
    dynamic_ratio: float,
) -> list[ScoredChunk]:
    """Apply dynamic threshold filtering."""
    if not chunks:
        return []
    chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
    best = chunks[0].score
    threshold = max(score_threshold, best * dynamic_ratio)
    return [c for c in chunks if c.score >= threshold]


# Final reranked top_k by query complexity (capped by settings.retrieval.top_k).
_RERANK_TOP_K_BY_COMPLEXITY = {"simple": 3, "normal": 5, "complex": 8}


def _rerank_top_k_for(complexity: str) -> int:
    """Return the reranked output size for a given query complexity."""
    cap = _RERANK_TOP_K_BY_COMPLEXITY.get(complexity, 5)
    return min(settings.retrieval.top_k, cap)


def _maybe_rerank(chunks: list[ScoredChunk], query: str, trace: QueryTrace) -> list[ScoredChunk]:
    """Apply the configured reranker to already-filtered candidates.

    Priority: cross-encoder (fast, ~200ms) > LLM-based (slow, ~30s).

    Cost policy by query complexity (``trace.query_complexity``):
      - "simple"  → skip the reranker entirely (truncate to a small top_k);
                    cheapest path, avoids cross-encoder scoring cost.
      - "normal"/"" → rerank, final top_k = min(retrieval.top_k, 5).
      - "complex" → rerank, final top_k = min(retrieval.top_k, 8).
    An empty complexity is treated as "normal".
    """
    if not settings.reranker.enabled or not chunks:
        return chunks

    complexity = trace.query_complexity or "normal"
    final_top_k = _rerank_top_k_for(complexity)

    # Cost policy: simple queries skip the (relatively) expensive reranker.
    if complexity == "simple":
        trace.reranker_backend = "skipped_simple"
        return chunks[:final_top_k]
    try:
        start = _time.perf_counter()
        trace.reranker_used = True
        trace.reranker_before += len(chunks)
        trace.candidates_examined += len(chunks)

        # Prefer cross-encoder (fast batch scoring) over LLM-based
        try:
            from retrieval.cross_encoder_reranker import (
                is_available,
                rerank_with_cross_encoder,
            )
            if is_available():
                top_k = final_top_k
                reranked = [
                    ScoredChunk.from_tuple(t)
                    for t in rerank_with_cross_encoder(
                        [c.as_tuple() for c in chunks],
                        query,
                        top_k=top_k,
                        min_score=settings.reranker.min_score,
                    )
                ]
                trace.reranker_backend = "cross_encoder"
                trace.reranker_after += len(reranked)
                trace.candidates_retained += len(reranked)
                trace.reranker_latency_ms += round((_time.perf_counter() - start) * 1000, 1)
                if reranked:
                    scores = [c.score for c in reranked]
                    trace.reranker_best_score = max(scores)
                    trace.reranker_mean_score = sum(scores) / len(scores)
                return reranked or chunks
        except ImportError:
            pass

        # Fallback: LLM-based reranker (slow but always available)
        from retrieval.reranker import rerank_chunks
        trace.reranker_backend = "llm"
        reranked = [ScoredChunk.from_tuple(t) for t in rerank_chunks([c.as_tuple() for c in chunks], query)]
        trace.reranker_after += len(reranked)
        trace.candidates_retained += len(reranked)
        trace.reranker_latency_ms += round((_time.perf_counter() - start) * 1000, 1)
        if reranked:
            scores = [c.score for c in reranked]
            trace.reranker_best_score = max(scores)
            trace.reranker_mean_score = sum(scores) / len(scores)
        return reranked or chunks
    except Exception as exc:
        log.debug("Reranker skipped: %s", exc)
        return chunks


def _passes_relevance_gate(
    notes: list[ScoredChunk],
    code: list[ScoredChunk],
    graph_str: str,
    cag_str: str,
    trace: QueryTrace,
) -> bool:
    """Check if retrieved context meets the relevance policy."""
    policy = settings.context_policy
    all_chunks = notes + code
    if not all_chunks and not graph_str and not cag_str:
        trace.context_rejected_reason = "no_chunks"
        return False

    if cag_str and not all_chunks and not graph_str:
        return True

    if all_chunks:
        best_score = max(c.score for c in all_chunks)
        if best_score < policy.min_relevance_score and not cag_str:
            trace.context_rejected_reason = "low_score"
            return False

        if len(all_chunks) < policy.min_relevant_chunks and not cag_str:
            trace.context_rejected_reason = "no_chunks"
            return False

    return True


# === Retrieval sub-functions ===

def _retrieve_notes(query: str, effective_k: int, cfg, trace: QueryTrace) -> list[ScoredChunk]:
    """Retrieve relevant chunks from notes collection."""
    start = _time.perf_counter()
    try:
        store = _get_store()

        primary = _vector_search(store, query, effective_k, collection="obsidian_vault", trace=trace)
        trace.notes_retrieved += len(primary)

        keywords = extract_keywords(query)
        secondary: list[ScoredChunk] = []
        if keywords != query.lower().strip() and len(keywords) > 3:
            secondary = _vector_search(store, keywords, effective_k, collection="obsidian_vault", trace=trace)
            trace.notes_retrieved += len(secondary)

        all_notes = _deduplicate(primary + secondary)
        all_notes = [c for c in all_notes if not _is_navigation_chunk(c)]
        notes_relevant = _apply_threshold(all_notes, cfg.score_threshold, cfg.dynamic_threshold_ratio)
        notes_relevant = notes_relevant[:effective_k]

        # Semantic dedup on final results
        notes_relevant = _semantic_deduplicate(notes_relevant)
        notes_relevant = _maybe_rerank(notes_relevant, query, trace)

        trace.notes_after_filter = len(notes_relevant)
        if notes_relevant:
            trace.best_note_score = notes_relevant[0].score
            trace.note_sources = [
                c.metadata.get(NOTE_TITLE, c.metadata.get(SOURCE_PATH, "?"))
                for c in notes_relevant[:5]
            ]

        _audit_retrieval(
            query=query,
            results=notes_relevant,
            strategy="hybrid_notes",
            collection="obsidian_vault",
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
            reranker_used=trace.reranker_used,
            sparse_used=trace.sparse_used,
        )
        return notes_relevant

    except Exception as exc:
        log.warning("Notes search failed: %s", exc)
        _audit_retrieval(
            query=query,
            results=[],
            strategy="hybrid_notes_error",
            collection="obsidian_vault",
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
        )
        return []


def _retrieve_code(query: str, effective_k: int, cfg, decision, trace: QueryTrace) -> list[ScoredChunk]:
    """Retrieve relevant chunks from code collection."""
    if not settings.repos.paths:
        return []

    start = _time.perf_counter()
    try:
        store = _get_store()
        code_col_name = settings.repos.collection_name

        code_filters: dict | None = None
        if decision.mode == ContextMode.RAG_ONLY:
            code_filters = {"__exclude_source_type": "repo_doc"}

        code_results = _vector_search(store, query, effective_k, collection=code_col_name, filters=code_filters, trace=trace)
        trace.code_retrieved += len(code_results)

        keywords = extract_keywords(query)
        if keywords != query.lower().strip() and len(keywords) > 3:
            code_kw = _vector_search(store, keywords, effective_k, collection=code_col_name, filters=code_filters, trace=trace)
            code_results = code_results + code_kw
            trace.code_retrieved += len(code_kw)

        code_dedup = _deduplicate(code_results)
        code_relevant = _apply_threshold(code_dedup, cfg.score_threshold, cfg.dynamic_threshold_ratio)
        code_relevant = code_relevant[:effective_k]

        # Semantic dedup on final results
        code_relevant = _semantic_deduplicate(code_relevant)
        code_relevant = _maybe_rerank(code_relevant, query, trace)

        trace.code_after_filter = len(code_relevant)
        if code_relevant:
            trace.best_code_score = code_relevant[0].score
            trace.code_sources = [
                f"{c.metadata.get(REPO_NAME, '?')}:{c.metadata.get(SOURCE_PATH, '?')}"
                for c in code_relevant[:5]
            ]

        _audit_retrieval(
            query=query,
            results=code_relevant,
            strategy="hybrid_code",
            collection=code_col_name,
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
            reranker_used=trace.reranker_used,
            filters=code_filters,
            sparse_used=trace.sparse_used,
        )
        return code_relevant

    except Exception as exc:
        log.warning("Code search failed: %s", exc)
        _audit_retrieval(
            query=query,
            results=[],
            strategy="hybrid_code_error",
            collection=settings.repos.collection_name,
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
        )
        return []


def _build_context_string(
    notes: list[ScoredChunk],
    code: list[ScoredChunk],
    graph_str: str,
    cag_str: str,
    budget: dict[str, int],
) -> str:
    """Assemble the final context string from all sources."""
    # Apply budget truncation
    if notes:
        notes_tuples = [c.as_tuple() for c in notes]
        notes_tuples = truncate_chunks(notes_tuples, budget["notes"])
        notes = [ScoredChunk.from_tuple(t) for t in notes_tuples]
    if code:
        code_tuples = [c.as_tuple() for c in code]
        code_tuples = truncate_chunks(code_tuples, budget["code"])
        code = [ScoredChunk.from_tuple(t) for t in code_tuples]
    if graph_str and budget["graph"]:
        graph_str = truncate_text(graph_str, budget["graph"])

    context_parts: list[str] = []

    if cag_str:
        context_parts.append(cag_str)

    if notes:
        lines = ["[SEMANTIC — PERSONAL NOTES]"]
        for chunk in notes:
            display = chunk.display_text
            title = chunk.note_title
            section = chunk.section_header
            label = f"[{title} / {section}]" if section else f"[{title}]"
            lines.append(f"{label}  score={chunk.score:.2f}")
            lines.append(display)
            lines.append("")
        lines.append("[/SEMANTIC — PERSONAL NOTES]")
        context_parts.append("\n".join(lines))

    if code:
        code_chunks: dict[str, list[ScoredChunk]] = {}
        doc_chunks: dict[str, list[ScoredChunk]] = {}
        for chunk in code:
            repo = chunk.repo_name or "repo"
            st = chunk.source_type
            if st == "repo_doc":
                doc_chunks.setdefault(repo, []).append(chunk)
            else:
                code_chunks.setdefault(repo, []).append(chunk)

        for repo_name, repo_items in code_chunks.items():
            lines = [f"[SEMANTIC — CODE: {repo_name}]"]
            for chunk in repo_items:
                display = chunk.display_text
                symbol = chunk.section_header
                fpath = chunk.source_path
                label = f"[{fpath} / {symbol}]" if symbol else f"[{fpath}]"
                lines.append(f"{label}  score={chunk.score:.2f}")
                lines.append(display)
                lines.append("")
            lines.append(f"[/SEMANTIC — CODE: {repo_name}]")
            context_parts.append("\n".join(lines))

        for repo_name, repo_items in doc_chunks.items():
            lines = [f"[SEMANTIC — REPO DOCS: {repo_name}]"]
            for chunk in repo_items:
                display = chunk.display_text
                section = chunk.section_header
                fpath = chunk.source_path
                label = f"[{fpath} / {section}]" if section else f"[{fpath}]"
                lines.append(f"{label}  score={chunk.score:.2f}")
                lines.append(display)
                lines.append("")
            lines.append(f"[/SEMANTIC — REPO DOCS: {repo_name}]")
            context_parts.append("\n".join(lines))

    if graph_str:
        context_parts.append(graph_str)

    return "\n\n".join(context_parts)


def _inject_cag_context(decision_mode: str, query: str, trace: QueryTrace) -> str:
    """Load relevant CAG packs and return formatted context string."""
    if not settings.cag.enabled:
        return ""
    try:
        from cag import get_pack_store
        from cag.packs import get_relevant_packs

        cag_store = get_pack_store()
        relevant = get_relevant_packs(cag_store, decision_mode, query)
        if relevant:
            cag_lines = ["[CACHED CONTEXT]"]
            for pack_type, content in relevant:
                cag_lines.append(f"## {pack_type}")
                cag_lines.append(content)
                cag_lines.append("")
            cag_lines.append("[/CACHED CONTEXT]")
            trace.cag_packs_used = len(relevant)
            trace.cag_hit = True
            return "\n".join(cag_lines)
    except Exception as exc:
        log.debug("CAG pack injection skipped: %s", exc)
    return ""


def _audit_retrieval(
    *,
    query: str,
    results: list[ScoredChunk],
    strategy: str,
    collection: str,
    top_k: int,
    latency_ms: float,
    reranker_used: bool = False,
    sparse_used: bool = False,
    filters: dict | None = None,
) -> None:
    try:
        from retrieval.audit import log_retrieval

        log_retrieval(
            query=query,
            results=results,
            strategy=strategy,
            collection=collection,
            top_k=top_k,
            latency_ms=latency_ms,
            reranker_used=reranker_used,
            sparse_used=sparse_used,
            filters=filters,
        )
    except Exception as exc:
        log.debug("Retrieval audit skipped: %s", exc)


def _audit_context_decision(
    *,
    query: str,
    notes: list[ScoredChunk],
    code: list[ScoredChunk],
    graph_str: str,
    cag_str: str,
    trace: QueryTrace,
    cfg,
    started_at: float,
) -> None:
    try:
        from retrieval.audit import log_retrieval

        synthetic: list[ScoredChunk] = list(notes) + list(code)
        if graph_str and not synthetic:
            synthetic.append(ScoredChunk(graph_str[:500], {"source_path": "graph", "source_type": "graph"}, 1.0))
        if cag_str and not synthetic:
            synthetic.append(ScoredChunk(cag_str[:500], {"source_path": "cag", "source_type": "cag"}, 1.0))

        log_retrieval(
            query=query,
            results=synthetic,
            strategy="context_decision",
            collection="combined",
            top_k=cfg.top_k,
            latency_ms=(_time.time() - started_at) * 1000,
            reranker_used=trace.reranker_used,
            accepted=trace.context_accepted,
            gate_reason=trace.context_rejected_reason,
            sources_used=trace.sources_used,
        )
    except Exception as exc:
        log.debug("Context decision audit skipped: %s", exc)


# === Main entry point ===

def build_rag_context(
    query: str,
    *,
    context_mode: str | None = None,
    trace: QueryTrace | None = None,
    history: list[dict] | None = None,
) -> tuple[str, bool, str]:
    """Multi-strategy search with optional graph augmentation.

    Returns (context_string, was_relevant, sources_used).
    sources_used: "none" | "rag" | "graph" | "rag+graph"
    """
    _t0 = _time.time()
    cfg = settings.retrieval
    mode = context_mode or cfg.context_mode

    if trace is None:
        trace = QueryTrace(query=query)

    # Get routing decision
    intent, decision = detect_intent_full(query, mode, history=history)
    trace.route_mode = decision.mode.value
    trace.route_reason = decision.reason
    trace.route_method = decision.method
    trace.route_confidence = decision.confidence
    trace.route_latency_ms = decision.latency_ms

    # NO_CONTEXT / CLARIFY: skip all retrieval
    if decision.mode in (ContextMode.NO_CONTEXT, ContextMode.CLARIFY):
        trace.context_accepted = False
        trace.context_rejected_reason = f"Router: {decision.mode.value}, no retrieval needed."
        trace.sources_used = "none"
        trace.log_summary()
        return "", False, "none"

    notes_relevant: list[ScoredChunk] = []
    code_relevant: list[ScoredChunk] = []
    graph_context_str = ""
    cag_context_str = ""

    # --- Adaptive top_k ---
    complexity = _estimate_complexity(query)
    trace.query_complexity = complexity
    if complexity == "simple":
        effective_k = max(3, cfg.top_k // 3)
    elif complexity == "complex":
        effective_k = min(cfg.top_k * 2, 20)
    else:
        effective_k = cfg.top_k

    store = _get_store()
    if intent.use_notes:
        col_size = _cached_count(store, "obsidian_vault")
        effective_k = _scale_k_by_size(effective_k, col_size)
    elif intent.use_code and settings.repos.paths:
        col_size = _cached_count(store, settings.repos.collection_name)
        effective_k = _scale_k_by_size(effective_k, col_size)

    trace.effective_top_k = effective_k

    # --- Retrieval ---
    if intent.use_notes:
        notes_relevant = _retrieve_notes(query, effective_k, cfg, trace)

    if intent.use_code:
        code_relevant = _retrieve_code(query, effective_k, cfg, decision, trace)

    # Strategy: Graph context
    if intent.use_graph and code_relevant:
        try:
            from retrieval.graph_context import build_graph_context

            graph_budget = allocate_budget(
                cfg.token_budget,
                has_notes=bool(notes_relevant),
                has_code=bool(code_relevant),
                has_graph=True,
            )

            graph_context_str = build_graph_context(
                [c.as_tuple() for c in code_relevant],
                query,
                max_neighbors=cfg.graph_max_neighbors,
                max_communities=cfg.graph_max_communities,
                token_budget=graph_budget["graph"],
            )
        except Exception as exc:
            log.warning("Graph context failed: %s", exc)
            graph_context_str = ""

    # Graph-only mode (no code chunks needed)
    if intent.use_graph and not code_relevant and not intent.use_code:
        try:
            from retrieval.graph_context import build_graph_query_context

            graph_context_str = build_graph_query_context(
                query,
                max_neighbors=cfg.graph_max_neighbors,
                max_communities=cfg.graph_max_communities,
                token_budget=cfg.token_budget,
            )
        except Exception as exc:
            log.warning("Graph-only context failed: %s", exc)
            graph_context_str = ""

    # CAG packs are intentionally loaded before the gate so cached context can
    # recover queries where fresh vector hits are sparse or temporarily weak.
    cag_context_str = _inject_cag_context(decision.mode.value, query, trace)

    # --- Relevance gate ---
    if not _passes_relevance_gate(notes_relevant, code_relevant, graph_context_str, cag_context_str, trace):
        trace.context_accepted = False
        trace.sources_used = "none"
        if settings.context_policy.log_weak_context:
            log.info(
                "Context rejected for query: %s — %s",
                query[:80], trace.context_rejected_reason,
            )
        _audit_context_decision(
            query=query,
            notes=notes_relevant,
            code=code_relevant,
            graph_str=graph_context_str,
            cag_str=cag_context_str,
            trace=trace,
            cfg=cfg,
            started_at=_t0,
        )
        _emit_retrieval_event(trace, query, _t0, cfg)
        trace.log_summary()
        return "", False, "none"

    trace.context_accepted = True

    # --- Budget allocation and context assembly ---
    budget = allocate_budget(
        cfg.token_budget,
        has_notes=bool(notes_relevant),
        has_code=bool(code_relevant),
        has_graph=bool(graph_context_str),
    )

    full_context = _build_context_string(
        notes_relevant, code_relevant,
        graph_context_str,
        cag_context_str, budget,
    )

    # Determine sources used
    sources: set[str] = set()
    if notes_relevant or code_relevant:
        sources.add("rag")
    if graph_context_str:
        sources.add("graph")
    if cag_context_str:
        sources.add("cag")
    sources_used = "+".join(sorted(sources)) or "none"
    trace.sources_used = sources_used

    _audit_context_decision(
        query=query,
        notes=notes_relevant,
        code=code_relevant,
        graph_str=graph_context_str,
        cag_str=cag_context_str,
        trace=trace,
        cfg=cfg,
        started_at=_t0,
    )

    instruction = get_context_instruction(sources_used)
    if instruction:
        full_context += "\n\n" + instruction

    _emit_retrieval_event(trace, query, _t0, cfg)
    trace.log_summary()
    return full_context, True, sources_used


def _emit_retrieval_event(trace: QueryTrace, query: str, t0: float, cfg) -> None:
    """Emit RETRIEVAL_COMPLETED event to observability (no-op if disabled)."""
    from observability import emit, is_enabled
    if not is_enabled():
        return
    from observability import EventName, RAGEvent, query_hash

    results_count = trace.notes_retrieved + trace.code_retrieved
    results_after_filter = trace.notes_after_filter + trace.code_after_filter
    best_score = max(trace.best_note_score, trace.best_code_score)

    emit(RAGEvent(
        event=EventName.RETRIEVAL_COMPLETED,
        latency_ms=(_time.time() - t0) * 1000,
        route_mode=trace.route_mode,
        route_method=trace.route_method,
        route_confidence=trace.route_confidence,
        route_latency_ms=trace.route_latency_ms,
        query_hash=query_hash(query),
        query_length=len(query),
        query_complexity=trace.query_complexity,
        effective_top_k=trace.effective_top_k,
        collection=trace.collection or "obsidian_vault",
        results_count=results_count,
        results_after_filter=results_after_filter,
        best_score=best_score,
        threshold_used=cfg.score_threshold,
        search_latency_ms=trace.search_latency_ms,
        exact_removed=trace.exact_removed,
        semantic_removed=trace.semantic_removed,
        reranker_used=trace.reranker_used,
        candidates_examined=trace.candidates_examined,
        candidates_retained=trace.candidates_retained,
        reranker_latency_ms=trace.reranker_latency_ms,
        hyde_used=trace.hyde_used,
        hyde_chars=trace.hyde_chars,
        hyde_latency_ms=trace.hyde_latency_ms,
        gate_passed=trace.context_accepted,
        gate_reason=trace.context_rejected_reason,
        total_context_tokens=trace.total_context_tokens,
        sources_used=trace.sources_used,
        success=True,
    ))


def should_use_rag(model: str) -> bool:
    """Check if model has RAG capability enabled in the registry."""
    from registry import is_rag_capable
    return is_rag_capable(model)


# ===========================================================================
# ASYNC API — Non-blocking retrieval for use inside async endpoints
# ===========================================================================

import asyncio as _asyncio  # noqa: E402


async def _vector_search_async(
    store,
    query_text: str,
    n: int,
    *,
    collection: str = "obsidian_vault",
    filters: dict | None = None,
    trace: QueryTrace | None = None,
) -> list[ScoredChunk]:
    """Async hybrid search: dense embedding + BM25 sparse (when available)."""
    embedder = get_embedder()
    embedding = await embedder.get_query_embedding_async(query_text)

    sparse_query = _get_sparse_query(query_text, collection)

    if trace is not None:
        trace.dense_used = True
        trace.sparse_available = trace.sparse_available or _bm25_cache.get(collection) is not None
        if sparse_query is not None:
            trace.sparse_used = True

    results = await store.query_async(
        embedding,
        n=min(n * 3, 50),
        collection=collection,
        filters=filters,
        sparse_query=sparse_query,
    )
    return [ScoredChunk(text=r.document, metadata=r.metadata, score=r.score) for r in results]


async def _retrieve_notes_async(query: str, effective_k: int, cfg, trace: QueryTrace) -> list[ScoredChunk]:
    """Async version of _retrieve_notes."""
    start = _time.perf_counter()
    try:
        store = _get_store()

        primary = await _vector_search_async(store, query, effective_k, collection="obsidian_vault", trace=trace)
        trace.notes_retrieved += len(primary)

        keywords = extract_keywords(query)
        secondary: list[ScoredChunk] = []
        if keywords != query.lower().strip() and len(keywords) > 3:
            secondary = await _vector_search_async(store, keywords, effective_k, collection="obsidian_vault", trace=trace)
            trace.notes_retrieved += len(secondary)

        all_notes = _deduplicate(primary + secondary)
        all_notes = [c for c in all_notes if not _is_navigation_chunk(c)]
        notes_relevant = _apply_threshold(all_notes, cfg.score_threshold, cfg.dynamic_threshold_ratio)
        notes_relevant = notes_relevant[:effective_k]

        notes_relevant = _semantic_deduplicate(notes_relevant)
        notes_relevant = _maybe_rerank(notes_relevant, query, trace)

        trace.notes_after_filter = len(notes_relevant)
        if notes_relevant:
            trace.best_note_score = notes_relevant[0].score
            trace.note_sources = [
                c.metadata.get(NOTE_TITLE, c.metadata.get(SOURCE_PATH, "?"))
                for c in notes_relevant[:5]
            ]

        _audit_retrieval(
            query=query,
            results=notes_relevant,
            strategy="hybrid_notes",
            collection="obsidian_vault",
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
            reranker_used=trace.reranker_used,
            sparse_used=trace.sparse_used,
        )
        return notes_relevant

    except Exception as exc:
        log.warning("Notes async search failed: %s", exc)
        _audit_retrieval(
            query=query,
            results=[],
            strategy="hybrid_notes_error",
            collection="obsidian_vault",
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
        )
        return []


async def _retrieve_code_async(query: str, effective_k: int, cfg, decision, trace: QueryTrace) -> list[ScoredChunk]:
    """Async version of _retrieve_code."""
    if not settings.repos.paths:
        return []

    start = _time.perf_counter()
    try:
        store = _get_store()
        code_col_name = settings.repos.collection_name

        code_filters: dict | None = None
        if decision.mode == ContextMode.RAG_ONLY:
            code_filters = {"__exclude_source_type": "repo_doc"}

        code_results = await _vector_search_async(store, query, effective_k, collection=code_col_name, filters=code_filters, trace=trace)
        trace.code_retrieved += len(code_results)

        keywords = extract_keywords(query)
        if keywords != query.lower().strip() and len(keywords) > 3:
            code_kw = await _vector_search_async(store, keywords, effective_k, collection=code_col_name, filters=code_filters, trace=trace)
            code_results = code_results + code_kw
            trace.code_retrieved += len(code_kw)

        code_dedup = _deduplicate(code_results)
        code_relevant = _apply_threshold(code_dedup, cfg.score_threshold, cfg.dynamic_threshold_ratio)
        code_relevant = code_relevant[:effective_k]

        code_relevant = _semantic_deduplicate(code_relevant)
        code_relevant = _maybe_rerank(code_relevant, query, trace)

        trace.code_after_filter = len(code_relevant)
        if code_relevant:
            trace.best_code_score = code_relevant[0].score
            trace.code_sources = [
                f"{c.metadata.get(REPO_NAME, '?')}:{c.metadata.get(SOURCE_PATH, '?')}"
                for c in code_relevant[:5]
            ]

        _audit_retrieval(
            query=query,
            results=code_relevant,
            strategy="hybrid_code",
            collection=code_col_name,
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
            reranker_used=trace.reranker_used,
            filters=code_filters,
            sparse_used=trace.sparse_used,
        )
        return code_relevant

    except Exception as exc:
        log.warning("Code async search failed: %s", exc)
        _audit_retrieval(
            query=query,
            results=[],
            strategy="hybrid_code_error",
            collection=settings.repos.collection_name,
            top_k=effective_k,
            latency_ms=(_time.perf_counter() - start) * 1000,
        )
        return []


async def build_rag_context_async(
    query: str,
    *,
    context_mode: str | None = None,
    trace: QueryTrace | None = None,
    history: list[dict] | None = None,
) -> tuple[str, bool, str]:
    """Async multi-strategy search with parallel retrieval.

    Returns (context_string, was_relevant, sources_used).
    sources_used: "none" | "rag" | "graph" | "rag+graph"
    """
    _t0 = _time.time()
    cfg = settings.retrieval
    mode = context_mode or cfg.context_mode

    if trace is None:
        trace = QueryTrace(query=query)

    # Get routing decision (CPU-bound, fast — no need for async)
    intent, decision = detect_intent_full(query, mode, history=history)
    trace.route_mode = decision.mode.value
    trace.route_reason = decision.reason
    trace.route_method = decision.method
    trace.route_confidence = decision.confidence
    trace.route_latency_ms = decision.latency_ms

    # NO_CONTEXT / CLARIFY: skip all retrieval
    if decision.mode in (ContextMode.NO_CONTEXT, ContextMode.CLARIFY):
        trace.context_accepted = False
        trace.context_rejected_reason = f"Router: {decision.mode.value}, no retrieval needed."
        trace.sources_used = "none"
        trace.log_summary()
        return "", False, "none"

    notes_relevant: list[ScoredChunk] = []
    code_relevant: list[ScoredChunk] = []
    graph_context_str = ""
    cag_context_str = ""

    # --- Adaptive top_k ---
    complexity = _estimate_complexity(query)
    trace.query_complexity = complexity
    if complexity == "simple":
        effective_k = max(3, cfg.top_k // 3)
    elif complexity == "complex":
        effective_k = min(cfg.top_k * 2, 20)
    else:
        effective_k = cfg.top_k

    store = _get_store()
    if intent.use_notes:
        col_size = _cached_count(store, "obsidian_vault")
        effective_k = _scale_k_by_size(effective_k, col_size)
    elif intent.use_code and settings.repos.paths:
        col_size = _cached_count(store, settings.repos.collection_name)
        effective_k = _scale_k_by_size(effective_k, col_size)

    trace.effective_top_k = effective_k

    # --- Parallel retrieval (notes + code concurrently) ---
    tasks = []
    task_names = []

    if intent.use_notes:
        tasks.append(_retrieve_notes_async(query, effective_k, cfg, trace))
        task_names.append("notes")

    if intent.use_code:
        tasks.append(_retrieve_code_async(query, effective_k, cfg, decision, trace))
        task_names.append("code")

    if tasks:
        results = await _asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(task_names, results):
            if isinstance(result, Exception):
                log.warning("Async retrieval %s failed: %s", name, result)
                continue
            if name == "notes":
                notes_relevant = result
            elif name == "code":
                code_relevant = result

    # Strategy: Graph context
    if intent.use_graph and code_relevant:
        try:
            from retrieval.graph_context import build_graph_context

            graph_budget = allocate_budget(
                cfg.token_budget,
                has_notes=bool(notes_relevant),
                has_code=bool(code_relevant),
                has_graph=True,
            )

            graph_context_str = build_graph_context(
                [c.as_tuple() for c in code_relevant],
                query,
                max_neighbors=cfg.graph_max_neighbors,
                max_communities=cfg.graph_max_communities,
                token_budget=graph_budget["graph"],
            )
        except Exception as exc:
            log.warning("Graph context failed: %s", exc)
            graph_context_str = ""

    # Graph-only mode (no code chunks needed)
    if intent.use_graph and not code_relevant and not intent.use_code:
        try:
            from retrieval.graph_context import build_graph_query_context

            graph_context_str = build_graph_query_context(
                query,
                max_neighbors=cfg.graph_max_neighbors,
                max_communities=cfg.graph_max_communities,
                token_budget=cfg.token_budget,
            )
        except Exception as exc:
            log.warning("Graph-only context failed: %s", exc)
            graph_context_str = ""

    # CAG packs
    cag_context_str = _inject_cag_context(decision.mode.value, query, trace)

    # --- Relevance gate ---
    if not _passes_relevance_gate(notes_relevant, code_relevant, graph_context_str, cag_context_str, trace):
        trace.context_accepted = False
        trace.sources_used = "none"
        if settings.context_policy.log_weak_context:
            log.info(
                "Context rejected for query: %s — %s",
                query[:80], trace.context_rejected_reason,
            )
        _audit_context_decision(
            query=query,
            notes=notes_relevant,
            code=code_relevant,
            graph_str=graph_context_str,
            cag_str=cag_context_str,
            trace=trace,
            cfg=cfg,
            started_at=_t0,
        )
        _emit_retrieval_event(trace, query, _t0, cfg)
        trace.log_summary()
        return "", False, "none"

    trace.context_accepted = True

    # --- Budget allocation and context assembly ---
    budget = allocate_budget(
        cfg.token_budget,
        has_notes=bool(notes_relevant),
        has_code=bool(code_relevant),
        has_graph=bool(graph_context_str),
    )

    full_context = _build_context_string(
        notes_relevant, code_relevant,
        graph_context_str,
        cag_context_str, budget,
    )

    # Determine sources used
    sources: set[str] = set()
    if notes_relevant or code_relevant:
        sources.add("rag")
    if graph_context_str:
        sources.add("graph")
    if cag_context_str:
        sources.add("cag")
    sources_used = "+".join(sorted(sources)) or "none"
    trace.sources_used = sources_used

    _audit_context_decision(
        query=query,
        notes=notes_relevant,
        code=code_relevant,
        graph_str=graph_context_str,
        cag_str=cag_context_str,
        trace=trace,
        cfg=cfg,
        started_at=_t0,
    )

    instruction = get_context_instruction(sources_used)
    if instruction:
        full_context += "\n\n" + instruction

    _emit_retrieval_event(trace, query, _t0, cfg)
    trace.log_summary()
    return full_context, True, sources_used
