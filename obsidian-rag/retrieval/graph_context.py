"""Constrói contexto de grafo para injecção no prompt RAG.

Para cada chunk de código relevante, procura o nó correspondente no
knowledge graph e enriquece com:
  - Sumário da comunidade
  - Vizinhos directos (calls, imports, uses)
  - Flags de god node
"""

from __future__ import annotations

import logging
import time as _time
from collections import defaultdict
from typing import Any

from pipeline.graph.backend import get_graph_backend
from retrieval.budget import estimate_tokens

log = logging.getLogger("obsidian_rag")

# Relações interessantes para contexto (skip contains, method, rationale_for)
_INTERESTING_RELS = {"calls", "imports_from", "uses"}


def build_graph_context(
    code_chunks: list[tuple[str, dict, float]],
    query: str,
    *,
    max_neighbors: int = 5,
    max_communities: int = 3,
    token_budget: int = 1000,
) -> str:
    """Constrói bloco de contexto estrutural a partir de code chunks relevantes.

    Para cada chunk que tem match no knowledge graph:
      1. Identifica o nó e a sua comunidade
      2. Busca sumário da comunidade (pré-computado por enrich.py)
      3. Busca vizinhos directos (outgoing/incoming)
      4. Marca god nodes

    Args:
        code_chunks: lista de (doc, metadata, score) — chunks de código relevantes
        query: texto da query original (para futuro ranking contextual)
        max_neighbors: máx vizinhos por nó
        max_communities: máx comunidades com sumário no output
        token_budget: tokens máximos para este bloco

    Returns:
        String formatada para injecção, ou "" se sem dados.
    """
    # Agrupar chunks por repo
    _t0 = _time.time()
    by_repo: dict[str, list[tuple[str, dict, float]]] = defaultdict(list)
    for doc, meta, score in code_chunks:
        repo = meta.get("repo_name", "")
        if repo:
            by_repo[repo].append((doc, meta, score))

    if not by_repo:
        return ""

    output_parts: list[str] = []
    total_tokens = 0
    total_nodes_matched = 0
    total_communities = 0

    for repo_name, repo_chunks in by_repo.items():
        backend = get_graph_backend()
        stats = backend.stats(repo_name)
        if not stats.graph_built:
            log.debug("Graph: no graph data for repo %s", repo_name)
            continue

        summaries = _backend_summaries(backend, repo_name)
        gods_list = _backend_gods(backend, repo_name)
        god_ids = {str(g.get("id", "")) for g in gods_list}

        # Track which communities we've already summarized
        summarized_communities: set[str] = set()
        seen_nodes: set[str] = set()
        repo_lines: list[str] = []

        for _doc, meta, _score in repo_chunks:
            source_file = meta.get("source_path", "")
            section_header = meta.get("section_header", "")

            if not source_file or not section_header:
                continue

            node = backend.node_for_chunk(repo_name, source_file, section_header)
            if node is None:
                continue

            node_id = str(node["id"])
            if node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)
            node_label = node.get("label", section_header)
            community = str(node.get("community", ""))
            is_god = node_id in god_ids

            # Community summary (max N communities)
            if community and community in summaries and community not in summarized_communities:
                if len(summarized_communities) < max_communities:
                    summary = summaries[community]
                    repo_lines.append(f"Comunidade {community}: {summary}")
                    summarized_communities.add(community)

            # Node info with neighbors
            god_tag = " [god-node]" if is_god else ""
            node_line = f"{node_label} ({source_file}){god_tag}:"

            neighbors = backend.neighbors(repo_name, node_id, limit=max_neighbors * 2)

            outgoing: list[str] = []
            incoming: list[str] = []
            for nb in neighbors:
                rel = nb["relation"]
                if str(rel).lower() not in _INTERESTING_RELS:
                    continue
                label = nb["label"]
                if nb["direction"] == "outgoing":
                    outgoing.append(f"{label} ({rel})")
                else:
                    incoming.append(f"{label} ({rel})")

            if not outgoing and not incoming:
                continue

            total_nodes_matched += 1
            repo_lines.append(node_line)
            if outgoing:
                repo_lines.append(f"  chama/usa: {', '.join(outgoing[:max_neighbors])}")
            if incoming:
                repo_lines.append(f"  chamado por: {', '.join(incoming[:max_neighbors])}")

        if not repo_lines:
            continue

        block = f"[CONTEXTO ESTRUTURAL — {repo_name}]\n"
        block += "\n".join(repo_lines)
        block += f"\n[/CONTEXTO ESTRUTURAL — {repo_name}]"

        block_tokens = estimate_tokens(block)
        if total_tokens + block_tokens > token_budget and output_parts:
            log.debug("Graph: budget exceeded, stopping at repo %s", repo_name)
            break

        output_parts.append(block)
        total_tokens += block_tokens
        total_communities += len(summarized_communities)

    log.info(
        "Graph context: %d nodes matched, %d communities, %d tokens across %d repos",
        total_nodes_matched, total_communities, total_tokens, len(output_parts),
    )

    from observability import emit, is_enabled
    if is_enabled():
        from observability import EventName, RAGEvent
        emit(RAGEvent(
            event=EventName.GRAPH_CONTEXT_BUILT,
            latency_ms=(_time.time() - _t0) * 1000,
            nodes_matched=total_nodes_matched,
            communities_used=total_communities,
            traversal_depth=max_neighbors,
            graph_context_hit=bool(output_parts),
        ))

    return "\n\n".join(output_parts)


def build_graph_query_context(
    query: str,
    *,
    max_neighbors: int = 5,
    max_communities: int = 3,
    token_budget: int = 1000,
) -> str:
    """Build structural context directly from graph query matches."""
    _t0 = _time.time()
    backend = get_graph_backend()

    from pipeline.graph.query import list_repos

    output_parts: list[str] = []
    total_tokens = 0
    total_nodes_matched = 0
    total_communities = 0

    for repo in list_repos():
        if not repo.get("graph_built"):
            continue
        repo_name = str(repo["name"])
        context = backend.context_for_query(
            repo_name,
            query,
            max_nodes=max_neighbors * 2,
            include_summaries=True,
        )
        summaries = [str(summary) for summary in context.get("summaries", [])][:max_communities]
        nodes = [node for node in context.get("nodes", []) if isinstance(node, dict)]
        if not nodes:
            nodes = _central_nodes(backend, repo_name, max_neighbors)

        repo_lines: list[str] = []
        for index, summary in enumerate(summaries, start=1):
            repo_lines.append(f"Comunidade {index}: {summary}")

        seen_nodes: set[str] = set()
        for node in nodes:
            node_id = str(node.get("id", ""))
            if not node_id or node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)
            node_label = str(node.get("label") or node_id)
            source_file = str(node.get("source_file", ""))
            node_line = f"{node_label} ({source_file}):"
            neighbors = backend.neighbors(repo_name, node_id, limit=max_neighbors * 2)

            outgoing: list[str] = []
            incoming: list[str] = []
            for nb in neighbors:
                rel = str(nb.get("relation", "related_to"))
                if rel.lower() not in _INTERESTING_RELS:
                    continue
                label = str(nb.get("label", nb.get("id", "")))
                if nb.get("direction") == "outgoing":
                    outgoing.append(f"{label} ({rel})")
                else:
                    incoming.append(f"{label} ({rel})")

            if not outgoing and not incoming:
                continue
            total_nodes_matched += 1
            repo_lines.append(node_line)
            if outgoing:
                repo_lines.append(f"  chama/usa: {', '.join(outgoing[:max_neighbors])}")
            if incoming:
                repo_lines.append(f"  chamado por: {', '.join(incoming[:max_neighbors])}")

        if not repo_lines:
            continue

        block = f"[CONTEXTO ESTRUTURAL — {repo_name}]\n"
        block += "\n".join(repo_lines)
        block += f"\n[/CONTEXTO ESTRUTURAL — {repo_name}]"
        block_tokens = estimate_tokens(block)
        if total_tokens + block_tokens > token_budget and output_parts:
            break
        output_parts.append(block)
        total_tokens += block_tokens
        total_communities += min(len(summaries), max_communities)

    log.info(
        "Graph query context: %d nodes matched, %d communities, %d tokens across %d repos",
        total_nodes_matched, total_communities, total_tokens, len(output_parts),
    )

    from observability import emit, is_enabled
    if is_enabled():
        from observability import EventName, RAGEvent
        emit(RAGEvent(
            event=EventName.GRAPH_CONTEXT_BUILT,
            latency_ms=(_time.time() - _t0) * 1000,
            nodes_matched=total_nodes_matched,
            communities_used=total_communities,
            traversal_depth=max_neighbors,
            graph_context_hit=bool(output_parts),
        ))

    return "\n\n".join(output_parts)


def _backend_summaries(backend: Any, repo_name: str) -> dict[str, str]:
    getter = getattr(backend, "get_summaries", None)
    if getter is None:
        return {}
    result = getter(repo_name)
    return result if isinstance(result, dict) else {}


def _backend_gods(backend: Any, repo_name: str) -> list[dict[str, Any]]:
    getter = getattr(backend, "get_gods", None)
    if getter is None:
        return []
    result = getter(repo_name)
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _central_nodes(backend: Any, repo_name: str, limit: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for god in _backend_gods(backend, repo_name)[:limit]:
        node_id = str(god.get("id", ""))
        if not node_id:
            continue
        node = backend.node_by_id(repo_name, node_id)
        if node is not None:
            nodes.append(node)
    return nodes
