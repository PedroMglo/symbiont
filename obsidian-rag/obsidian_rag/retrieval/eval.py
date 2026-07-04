"""Retrieval quality evaluation framework.

Provides tools to measure retrieval quality using golden queries:
  - Precision@k, Recall@k, MRR (Mean Reciprocal Rank)
  - Latency P50/P95 tracking
  - Golden query set management (JSON format)
  - Regression detection between runs

Usage:
    from obsidian_rag.retrieval.eval import EvalHarness, load_golden_queries
    harness = EvalHarness()
    results = harness.run(golden_queries)
    harness.report(results)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obsidian_rag.retrieval.scored_chunk import ScoredChunk


@dataclass(frozen=True)
class GoldenQuery:
    """A single evaluation query with expected results."""
    query: str
    expected_sources: list[str]
    min_score: float = 0.5
    expected_keywords: list[str] = field(default_factory=list)
    collection: str = "obsidian_vault"
    top_k: int = 10


@dataclass
class QueryEvalResult:
    """Evaluation result for a single query."""
    query: str
    precision_at_k: float = 0.0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    latency_ms: float = 0.0
    retrieved_sources: list[str] = field(default_factory=list)
    relevant_found: list[str] = field(default_factory=list)
    score_above_min: bool = True


@dataclass
class EvalReport:
    """Aggregated evaluation metrics across all golden queries."""
    mean_precision: float = 0.0
    mean_recall: float = 0.0
    mean_mrr: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    total_queries: int = 0
    queries_with_results: int = 0
    per_query: list[QueryEvalResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean_precision": self.mean_precision,
            "mean_recall": self.mean_recall,
            "mean_mrr": self.mean_mrr,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "total_queries": self.total_queries,
            "queries_with_results": self.queries_with_results,
            "per_query": [
                {
                    "query": r.query,
                    "precision_at_k": r.precision_at_k,
                    "recall_at_k": r.recall_at_k,
                    "mrr": r.mrr,
                    "latency_ms": r.latency_ms,
                    "retrieved_sources": r.retrieved_sources,
                    "relevant_found": r.relevant_found,
                    "score_above_min": r.score_above_min,
                }
                for r in self.per_query
            ],
        }


def load_golden_queries(path: str | Path) -> list[GoldenQuery]:
    """Load golden queries from a JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Golden queries file not found: {p}")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return [GoldenQuery(**item) for item in data]


def save_golden_queries(queries: list[GoldenQuery], path: str | Path) -> None:
    """Save golden queries to a JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for q in queries:
        entry: dict[str, Any] = {"query": q.query, "expected_sources": q.expected_sources}
        if q.min_score != 0.5:
            entry["min_score"] = q.min_score
        if q.expected_keywords:
            entry["expected_keywords"] = q.expected_keywords
        if q.collection != "obsidian_vault":
            entry["collection"] = q.collection
        if q.top_k != 10:
            entry["top_k"] = q.top_k
        data.append(entry)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class EvalHarness:
    """Retrieval evaluation harness — runs golden queries and measures quality."""

    def __init__(
        self,
        *,
        store=None,
        embedder=None,
        api_url: str = "",
        api_key: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def _get_store(self):
        if self._store is None:
            from obsidian_rag.store import get_store
            self._store = get_store()
        return self._store

    def _get_embedder(self):
        if self._embedder is None:
            from obsidian_rag.embeddings import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    def _retrieve(self, query: str, collection: str, top_k: int) -> list[ScoredChunk]:
        """Execute a single retrieval query."""
        if self._api_url:
            return self._retrieve_http(query, collection, top_k)

        embedder = self._get_embedder()
        store = self._get_store()
        embedding = embedder.get_query_embedding(query)
        results = store.query(embedding, n=top_k, collection=collection)
        return [
            ScoredChunk(
                text=r.document,
                metadata=r.metadata,
                score=r.score,
            )
            for r in results
        ]

    def _retrieve_http(self, query: str, collection: str, top_k: int) -> list[ScoredChunk]:
        """Execute retrieval through the running FastAPI service."""
        import httpx

        endpoint = "/query/code" if collection == "code_repos" else "/query"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        resp = httpx.post(
            f"{self._api_url}{endpoint}",
            json={"query": query, "top_k": top_k},
            headers=headers,
            timeout=self._timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        chunks: list[ScoredChunk] = []
        for item in data.get("results", []):
            metadata = dict(item)
            text = metadata.pop("content", None) or metadata.pop("text", "")
            chunks.append(ScoredChunk(text=text, metadata=metadata, score=float(item.get("score", 0.0))))
        return chunks

    def evaluate_query(self, golden: GoldenQuery) -> QueryEvalResult:
        """Evaluate a single golden query."""
        start = time.time()
        chunks = self._retrieve(golden.query, golden.collection, golden.top_k)
        latency_ms = (time.time() - start) * 1000

        retrieved_sources = [c.source_path for c in chunks]
        expected = set(golden.expected_sources)

        # Precision@k: fraction of retrieved that are relevant
        relevant_in_results = [s for s in retrieved_sources if self._source_matches(s, expected)]
        precision = len(relevant_in_results) / len(retrieved_sources) if retrieved_sources else 0.0

        # Recall@k: fraction of expected that were retrieved
        found_expected = set()
        for s in retrieved_sources:
            for exp in expected:
                if self._source_matches(s, {exp}):
                    found_expected.add(exp)
        recall = len(found_expected) / len(expected) if expected else 1.0

        # MRR: 1/rank of first relevant result
        mrr = 0.0
        for i, s in enumerate(retrieved_sources):
            if self._source_matches(s, expected):
                mrr = 1.0 / (i + 1)
                break

        # Score check
        score_ok = all(c.score >= golden.min_score for c in chunks[:1]) if chunks else False

        return QueryEvalResult(
            query=golden.query,
            precision_at_k=precision,
            recall_at_k=recall,
            mrr=mrr,
            latency_ms=latency_ms,
            retrieved_sources=retrieved_sources[:5],
            relevant_found=list(found_expected),
            score_above_min=score_ok,
        )

    @staticmethod
    def _source_matches(retrieved: str, expected_set: set[str]) -> bool:
        """Check if a retrieved source matches any expected source (substring match)."""
        for exp in expected_set:
            if exp in retrieved or retrieved.endswith(exp):
                return True
        return False

    def run(self, golden_queries: list[GoldenQuery]) -> EvalReport:
        """Run evaluation on a set of golden queries."""
        results: list[QueryEvalResult] = []
        for gq in golden_queries:
            result = self.evaluate_query(gq)
            results.append(result)

        if not results:
            return EvalReport()

        latencies = sorted(r.latency_ms for r in results)
        n = len(latencies)

        return EvalReport(
            mean_precision=sum(r.precision_at_k for r in results) / n,
            mean_recall=sum(r.recall_at_k for r in results) / n,
            mean_mrr=sum(r.mrr for r in results) / n,
            latency_p50_ms=latencies[n // 2],
            latency_p95_ms=latencies[int(n * 0.95)] if n > 1 else latencies[0],
            total_queries=n,
            queries_with_results=sum(1 for r in results if r.retrieved_sources),
            per_query=results,
        )

    @staticmethod
    def report(eval_report: EvalReport) -> str:
        """Format evaluation report as human-readable text."""
        lines = [
            "=== Retrieval Quality Report ===",
            f"Queries evaluated: {eval_report.total_queries}",
            f"Queries with results: {eval_report.queries_with_results}",
            "",
            f"Mean Precision@k: {eval_report.mean_precision:.3f}",
            f"Mean Recall@k:    {eval_report.mean_recall:.3f}",
            f"Mean MRR:         {eval_report.mean_mrr:.3f}",
            "",
            f"Latency P50: {eval_report.latency_p50_ms:.1f}ms",
            f"Latency P95: {eval_report.latency_p95_ms:.1f}ms",
        ]

        if eval_report.per_query:
            lines.append("")
            lines.append("--- Per-query breakdown ---")
            for r in eval_report.per_query:
                status = "✓" if r.recall_at_k > 0 else "✗"
                lines.append(
                    f"  {status} P={r.precision_at_k:.2f} R={r.recall_at_k:.2f} "
                    f"MRR={r.mrr:.2f} {r.latency_ms:.0f}ms | {r.query[:60]}"
                )

        return "\n".join(lines)
