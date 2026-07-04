"""Observability — structured logging for the RAG pipeline.

Captures and logs the full decision chain: routing, retrieval,
scoring, context rejection, and timing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from obsidian_rag.config import settings

log = logging.getLogger("obsidian_rag")


@dataclass
class QueryTrace:
    """Full trace of a single query through the pipeline."""
    query: str = ""
    query_rewritten: str = ""

    # Router
    route_mode: str = ""
    route_reason: str = ""
    route_method: str = ""
    route_confidence: float = 0.0
    route_latency_ms: float = 0.0

    # Adaptive top_k
    query_complexity: str = ""     # "simple" | "normal" | "complex"
    effective_top_k: int = 0

    # RAG retrieval
    collection: str = ""
    notes_retrieved: int = 0
    notes_after_filter: int = 0
    code_retrieved: int = 0
    code_after_filter: int = 0
    best_note_score: float = 0.0
    best_code_score: float = 0.0
    results_count: int = 0
    results_after_filter: int = 0
    best_score: float = 0.0
    threshold_used: float = 0.0
    search_latency_ms: float = 0.0
    exact_removed: int = 0
    semantic_removed: int = 0

    # Hybrid search (dense + BM25 sparse) visibility
    dense_used: bool = False
    sparse_used: bool = False
    sparse_available: bool = False

    # Graph
    graph_nodes_matched: int = 0
    graph_communities_used: int = 0

    # Reranker
    reranker_used: bool = False
    reranker_backend: str = ""  # "cross_encoder" or "llm"
    reranker_before: int = 0
    reranker_after: int = 0
    reranker_best_score: float = 0.0
    reranker_mean_score: float = 0.0
    candidates_examined: int = 0
    candidates_retained: int = 0
    reranker_latency_ms: float = 0.0

    # HyDE
    hyde_used: bool = False
    hyde_chars: int = 0
    hyde_latency_ms: float = 0.0

    # Context decision
    context_accepted: bool = False
    context_rejected_reason: str = ""
    sources_used: str = "none"
    total_context_tokens: int = 0

    # Sources detail
    note_sources: list[str] = field(default_factory=list)
    code_sources: list[str] = field(default_factory=list)

    # CAG
    cag_packs_used: int = 0
    cag_hit: bool = False

    # Model
    model: str = ""

    # Timing
    total_ms: float = 0.0
    _start: float = field(default_factory=time.perf_counter, repr=False)

    def finish(self):
        """Record total elapsed time."""
        self.total_ms = round((time.perf_counter() - self._start) * 1000, 1)

    def log_summary(self):
        """Log a structured summary of the query trace."""
        self.finish()
        log.info(
            "Query trace: route=%s confidence=%.1f method=%s sources=%s "
            "notes=%d/%d code=%d/%d graph_nodes=%d "
            "context_accepted=%s total=%dms | %s",
            self.route_mode, self.route_confidence, self.route_method,
            self.sources_used,
            self.notes_after_filter, self.notes_retrieved,
            self.code_after_filter, self.code_retrieved,
            self.graph_nodes_matched,
            self.context_accepted, self.total_ms,
            self.query[:80],
        )
        if not self.context_accepted and self.context_rejected_reason:
            log.info("Context rejected: %s", self.context_rejected_reason)

    def to_debug_dict(self) -> dict:
        """Return a dict suitable for debug output / API headers."""
        return {
            "route_mode": self.route_mode,
            "route_reason": self.route_reason,
            "route_method": self.route_method,
            "route_confidence": self.route_confidence,
            "route_latency_ms": self.route_latency_ms,
            "query_complexity": self.query_complexity,
            "effective_top_k": self.effective_top_k,
            "query_rewritten": self.query_rewritten,
            "notes_retrieved": self.notes_retrieved,
            "notes_after_filter": self.notes_after_filter,
            "code_retrieved": self.code_retrieved,
            "code_after_filter": self.code_after_filter,
            "best_note_score": self.best_note_score,
            "best_code_score": self.best_code_score,
            "graph_nodes_matched": self.graph_nodes_matched,
            "graph_communities_used": self.graph_communities_used,
            "dense_used": self.dense_used,
            "sparse_used": self.sparse_used,
            "sparse_available": self.sparse_available,
            "reranker_used": self.reranker_used,
            "reranker_backend": self.reranker_backend,
            "reranker_before": self.reranker_before,
            "reranker_after": self.reranker_after,
            "reranker_latency_ms": self.reranker_latency_ms,
            "threshold_used": self.threshold_used,
            "context_accepted": self.context_accepted,
            "context_rejected_reason": self.context_rejected_reason,
            "sources_used": self.sources_used,
            "total_context_tokens": self.total_context_tokens,
            "cag_packs_used": self.cag_packs_used,
            "cag_hit": self.cag_hit,
            "note_sources": self.note_sources[:5],
            "code_sources": self.code_sources[:5],
            "model": self.model,
            "total_ms": self.total_ms,
        }

    def format_debug_output(self) -> str:
        """Human-readable debug output for terminal."""
        lines = [
            "── Pipeline Debug ──",
            f"  Route:      {self.route_mode} (confidence={self.route_confidence:.1f}, method={self.route_method})",
            f"  Reason:     {self.route_reason}",
        ]
        if self.query_rewritten and self.query_rewritten != self.query:
            lines.append(f"  Rewritten:  {self.query_rewritten}")
        if self.route_mode != "NO_CONTEXT":
            lines.append(f"  Notes:      {self.notes_after_filter}/{self.notes_retrieved} (best={self.best_note_score:.2f})")
            lines.append(f"  Code:       {self.code_after_filter}/{self.code_retrieved} (best={self.best_code_score:.2f})")
            if self.graph_nodes_matched:
                lines.append(f"  Graph:      {self.graph_nodes_matched} nodes, {self.graph_communities_used} communities")
            if self.reranker_used:
                lines.append(f"  Reranker:   {self.reranker_before} → {self.reranker_after}")
            lines.append(f"  Context:    {'accepted' if self.context_accepted else 'REJECTED'}")
            if not self.context_accepted:
                lines.append(f"  Reject:     {self.context_rejected_reason}")
            if self.note_sources:
                lines.append(f"  Sources:    {', '.join(self.note_sources[:3])}")
            if self.code_sources:
                lines.append(f"  Code src:   {', '.join(self.code_sources[:3])}")
        lines.append(f"  Sources:    {self.sources_used}")
        lines.append(f"  Time:       {self.total_ms:.0f}ms")
        lines.append("──────────────────")
        return "\n".join(lines)


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging():
    """Configure logging based on debug settings."""
    debug_cfg = settings.debug
    level = getattr(logging, debug_cfg.log_level.upper(), logging.INFO)
    logger = logging.getLogger("obsidian_rag")
    logger.setLevel(level)

    use_json = debug_cfg.log_format.lower() == "json"

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        if use_json:
            handler.setFormatter(_JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            ))
        logger.addHandler(handler)

    if debug_cfg.log_to_file:
        from obsidian_rag.config import PROJECT_ROOT
        fh = logging.FileHandler(PROJECT_ROOT / "obsidian_rag.log")
        fh.setLevel(logging.DEBUG)
        # File logs always use JSON for machine parsing
        fh.setFormatter(_JsonFormatter())
        logger.addHandler(fh)
