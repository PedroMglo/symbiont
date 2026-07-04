"""Retrieval audit log — persistent record of query results for observability.

Records every retrieval operation with query, results, scores, latency,
and strategy used. Enables offline analysis of retrieval quality, debugging
poor results, and tracking improvements over time.

Storage: append-only JSONL file (one JSON object per line).
Location: {data_dir}/audit/retrieval.jsonl

Usage:
    from retrieval.audit import log_retrieval
    log_retrieval(query="...", results=[...], strategy="hybrid", latency_ms=42.5)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rag_config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()


@dataclass
class AuditEntry:
    """Single retrieval audit record."""

    timestamp: str
    query: str
    strategy: str
    collection: str
    top_k: int
    result_count: int
    top_scores: list[float]
    top_sources: list[str]
    latency_ms: float
    hyde_used: bool = False
    reranker_used: bool = False
    sparse_used: bool = False
    accepted: bool | None = None
    gate_reason: str = ""
    sources_used: str = ""
    filters: dict[str, Any] = field(default_factory=dict)


def _get_audit_path() -> Path:
    data_dir = Path(settings.paths.data_dir)
    audit_dir = data_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "retrieval.jsonl"


def log_retrieval(
    *,
    query: str,
    results: list[tuple[str, dict, float]] | list[Any],
    strategy: str = "hybrid",
    collection: str = "obsidian_vault",
    top_k: int = 10,
    latency_ms: float = 0.0,
    hyde_used: bool = False,
    reranker_used: bool = False,
    sparse_used: bool = False,
    accepted: bool | None = None,
    gate_reason: str = "",
    sources_used: str = "",
    filters: dict[str, Any] | None = None,
) -> None:
    """Append a retrieval audit entry to the log file.

    Best-effort: failures are logged but never raise.
    """
    try:
        top_scores: list[float] = []
        top_sources: list[str] = []

        for r in results[:10]:
            if hasattr(r, "score"):
                top_scores.append(round(r.score, 4))
                top_sources.append(getattr(r, "source_path", "") or r.metadata.get("source_path", ""))
            elif isinstance(r, tuple) and len(r) >= 3:
                top_scores.append(round(r[2], 4))
                meta = r[1] if isinstance(r[1], dict) else {}
                top_sources.append(meta.get("source_path", ""))

        entry = AuditEntry(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            query=query,
            strategy=strategy,
            collection=collection,
            top_k=top_k,
            result_count=len(results),
            top_scores=top_scores,
            top_sources=top_sources,
            latency_ms=round(latency_ms, 1),
            hyde_used=hyde_used,
            reranker_used=reranker_used,
            sparse_used=sparse_used,
            accepted=accepted,
            gate_reason=gate_reason,
            sources_used=sources_used,
            filters=filters or {},
        )

        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"

        with _lock:
            with open(_get_audit_path(), "a", encoding="utf-8") as f:
                f.write(line)

    except Exception as exc:
        log.debug("Audit log write failed: %s", exc)


def read_recent(n: int = 50) -> list[dict[str, Any]]:
    """Read the N most recent audit entries (for diagnostics)."""
    path = _get_audit_path()
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-n:] if len(lines) > n else lines
    entries = []
    for line in recent:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def summary_stats(hours: int = 24) -> dict[str, Any]:
    """Aggregate stats from recent audit entries."""
    entries = read_recent(500)
    if not entries:
        return {"total_queries": 0}

    cutoff = time.time() - (hours * 3600)
    cutoff_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cutoff))

    recent = [e for e in entries if e.get("timestamp", "") >= cutoff_str]
    if not recent:
        return {"total_queries": 0, "period_hours": hours}

    latencies = [e["latency_ms"] for e in recent if "latency_ms" in e]
    scores = []
    decisions = [e for e in recent if e.get("strategy") == "context_decision"]
    accepted = [e for e in decisions if e.get("accepted") is True]
    gate_reasons: dict[str, int] = {}
    for e in recent:
        if e.get("top_scores"):
            scores.append(e["top_scores"][0])
        reason = e.get("gate_reason") or ""
        if reason:
            gate_reasons[reason] = gate_reasons.get(reason, 0) + 1

    stats = {
        "total_queries": len(recent),
        "period_hours": hours,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0, 1),
        "avg_top_score": round(sum(scores) / len(scores), 3) if scores else 0,
        "zero_result_pct": round(sum(1 for e in recent if e.get("result_count", 0) == 0) / len(recent) * 100, 1),
        "hyde_usage_pct": round(sum(1 for e in recent if e.get("hyde_used")) / len(recent) * 100, 1),
        "reranker_usage_pct": round(sum(1 for e in recent if e.get("reranker_used")) / len(recent) * 100, 1),
        "sparse_usage_pct": round(sum(1 for e in recent if e.get("sparse_used")) / len(recent) * 100, 1),
    }
    if decisions:
        stats["context_decisions"] = len(decisions)
        stats["context_acceptance_pct"] = round(len(accepted) / len(decisions) * 100, 1)
        stats["top_gate_reasons"] = sorted(gate_reasons.items(), key=lambda item: item[1], reverse=True)[:5]
    return stats
