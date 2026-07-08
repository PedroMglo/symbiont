"""Graph provider — knowledge graph context via RAG API."""

from __future__ import annotations

import logging

import httpx

from code_analysis.config import get_settings

log = logging.getLogger(__name__)


def _derived_summary(item: dict) -> str:
    parts: list[str] = []
    summaries = [str(s) for s in item.get("summaries", []) if s]
    god_nodes = [str(n) for n in item.get("god_nodes", []) if n]
    nodes = item.get("nodes", [])
    edges = item.get("edges", [])

    if summaries:
        parts.extend(summaries[:3])
    if nodes:
        labels = [str(n.get("label") or n.get("id") or "") for n in nodes if isinstance(n, dict)]
        labels = [label for label in labels if label]
        if labels:
            parts.append("Matched nodes: " + ", ".join(labels[:10]))
    if god_nodes:
        parts.append("Central nodes: " + ", ".join(god_nodes[:5]))
    if edges:
        parts.append(f"Edges: {len(edges)} relationships matched")
    return "\n\n".join(parts)


def get_graph_context(query: str, budget_tokens: int = 2000) -> str:
    """Fetch knowledge graph context from the RAG service."""
    cfg = get_settings()
    url = f"{cfg.graph.rag_url}/graph/context"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.graph.api_key:
        headers["Authorization"] = f"Bearer {cfg.graph.api_key}"

    payload = {
        "query": query,
        "max_nodes": cfg.graph.max_nodes,
        "include_summaries": True,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=cfg.graph.timeout_seconds)
        if resp.status_code != 200:
            log.debug("Graph API: HTTP %d", resp.status_code)
            return _unavailable_context(f"http_status_{resp.status_code}")

        data = resp.json()
        results = data.get("results", [])
        if not results:
            return _unavailable_context("no_results")

        parts: list[str] = []
        for item in results:
            title = item.get("title") or item.get("repo", "")
            summary = item.get("summary") or _derived_summary(item)
            if title:
                parts.append(f"### {title}\n{summary}" if summary else f"### {title}")

        return "## Knowledge Graph\n\n" + "\n\n".join(parts)

    except Exception as exc:
        log.debug("Graph API: request failed: %s", exc)
        return _unavailable_context(type(exc).__name__)


def _unavailable_context(reason: str) -> str:
    return f"## Knowledge Graph\n\nGraph context unavailable: {reason}"
