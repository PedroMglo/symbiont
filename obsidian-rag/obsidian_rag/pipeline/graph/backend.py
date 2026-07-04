"""Graph backend interface and current JSON implementation.

The JSON backend is the baseline adapter for existing Graphify ``graph.json``
artifacts. It keeps the public RAG API behavior while making the runtime
replaceable by a service-backed graph database in a later phase.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from obsidian_rag.pipeline.graph.builder import get_graph_json_path

GraphPathResolver = Callable[[str], Path | None]


@dataclass(frozen=True)
class GraphStats:
    repo: str
    graph_built: bool
    node_count: int | None = None
    edge_count: int | None = None
    graph_path: str | None = None


@dataclass(frozen=True)
class GraphImportResult:
    repo: str
    backend: str
    source_hash: str
    node_count: int
    edge_count: int
    skipped: bool = False


class GraphBackend(Protocol):
    """Runtime interface for GraphRAG graph query backends."""

    name: str

    def health(self, repo: str | None = None) -> dict[str, Any]:
        ...

    def stats(self, repo: str) -> GraphStats:
        ...

    def import_graph(self, repo: str, graph: dict[str, Any], *, source_hash: str) -> GraphImportResult:
        ...

    def neighbors(self, repo: str, node: str, *, depth: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def shortest_paths(self, repo: str, source: str, target: str, *, limit: int = 1) -> list[list[str]]:
        ...

    def subgraph_for_chunks(self, repo: str, chunk_ids: list[str], *, budget: int) -> dict[str, list[dict[str, Any]]]:
        ...

    def node_for_chunk(self, repo: str, source_file: str, section_header: str) -> dict[str, Any] | None:
        ...

    def node_by_id(self, repo: str, node_id: str) -> dict[str, Any] | None:
        ...

    def context_for_query(
        self,
        repo: str,
        query: str,
        *,
        max_nodes: int,
        include_summaries: bool,
    ) -> dict[str, Any]:
        ...

    def query(self, repo: str, query: str, *, limit: int = 10) -> str:
        ...


class JsonGraphBackend:
    """Graph backend over Graphify node-link JSON artifacts."""

    name = "json"

    def __init__(self, graph_path_resolver: GraphPathResolver = get_graph_json_path) -> None:
        self._graph_path_resolver = graph_path_resolver

    def health(self, repo: str | None = None) -> dict[str, Any]:
        if repo is None:
            return {"backend": self.name, "ok": True}
        stats = self.stats(repo)
        return {"backend": self.name, "ok": stats.graph_built, "repo": repo, "graph_path": stats.graph_path}

    def stats(self, repo: str) -> GraphStats:
        graph_path = self._graph_path_resolver(repo)
        if graph_path is None:
            return GraphStats(repo=repo, graph_built=False)
        try:
            data = self._load_graph_data(repo)
        except (OSError, json.JSONDecodeError):
            return GraphStats(repo=repo, graph_built=True, graph_path=str(graph_path))
        return GraphStats(
            repo=repo,
            graph_built=True,
            node_count=len(_nodes(data)),
            edge_count=len(_links(data)),
            graph_path=str(graph_path),
        )

    def load_raw_graph(self, repo: str) -> dict[str, Any]:
        return self._load_graph_data(repo)

    def import_graph(self, repo: str, graph: dict[str, Any], *, source_hash: str) -> GraphImportResult:
        return GraphImportResult(
            repo=repo,
            backend=self.name,
            source_hash=source_hash,
            node_count=len(_nodes(graph)),
            edge_count=len(_links(graph)),
            skipped=True,
        )

    def neighbors(self, repo: str, node: str, *, depth: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        data = self._load_graph_data(repo)
        target = _find_node(data, node)
        if target is None:
            return []

        nodes_by_id = _nodes_by_id(data)
        adjacent = _adjacency(data)
        reverse = _reverse_adjacency(data)
        target_id = str(target.get("id", target.get("label", node)))

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        frontier = deque([(target_id, 0)])
        while frontier and len(results) < limit:
            current, current_depth = frontier.popleft()
            if current_depth >= max(1, depth):
                continue
            for edge, direction in [*adjacent.get(current, []), *reverse.get(current, [])]:
                neighbor_id = str(edge["target"] if direction == "outgoing" else edge["source"])
                if neighbor_id in seen or neighbor_id == target_id:
                    continue
                seen.add(neighbor_id)
                neighbor = nodes_by_id.get(neighbor_id, {"id": neighbor_id, "label": neighbor_id})
                results.append(_neighbor_row(neighbor, edge, direction, current_depth + 1))
                if len(results) >= limit:
                    break
                frontier.append((neighbor_id, current_depth + 1))
        return results

    def shortest_paths(self, repo: str, source: str, target: str, *, limit: int = 1) -> list[list[str]]:
        data = self._load_graph_data(repo)
        source_node = _find_node(data, source)
        target_node = _find_node(data, target)
        if source_node is None or target_node is None:
            return []

        source_id = str(source_node.get("id", source))
        target_id = str(target_node.get("id", target))
        nodes_by_id = _nodes_by_id(data)
        adjacent = _adjacency(data)

        queue: deque[list[str]] = deque([[source_id]])
        shortest: list[list[str]] = []
        seen_depth: dict[str, int] = {source_id: 0}
        while queue and len(shortest) < limit:
            path = queue.popleft()
            current = path[-1]
            if current == target_id:
                shortest.append([_node_label(nodes_by_id.get(node_id, {"id": node_id})) for node_id in path])
                continue
            for edge, _direction in adjacent.get(current, []):
                next_id = str(edge["target"])
                next_depth = len(path)
                if next_id in path:
                    continue
                if seen_depth.get(next_id, next_depth) < next_depth:
                    continue
                seen_depth[next_id] = next_depth
                queue.append([*path, next_id])
        return shortest

    def subgraph_for_chunks(self, repo: str, chunk_ids: list[str], *, budget: int) -> dict[str, list[dict[str, Any]]]:
        data = self._load_graph_data(repo)
        terms = _query_terms(" ".join(str(chunk_id) for chunk_id in chunk_ids))
        if not terms or budget <= 0:
            return {"nodes": [], "links": []}

        matched_ids = {
            str(node.get("id"))
            for node in _nodes(data)
            if any(term in _node_haystack(node) for term in terms)
        }
        selected_ids = set(matched_ids)
        selected_links: list[dict[str, Any]] = []
        for link in _links(data):
            source = str(link.get("source", link.get("from", "")))
            target = str(link.get("target", link.get("to", "")))
            if source in matched_ids or target in matched_ids:
                selected_links.append(link)
                selected_ids.update((source, target))
            if len(selected_ids) >= budget:
                break

        selected_nodes = [node for node in _nodes(data) if str(node.get("id")) in selected_ids]
        return {"nodes": selected_nodes[:budget], "links": selected_links[:budget]}

    def node_for_chunk(self, repo: str, source_file: str, section_header: str) -> dict[str, Any] | None:
        data = self._load_graph_data(repo)
        return _node_for_chunk(_nodes(data), source_file, section_header)

    def node_by_id(self, repo: str, node_id: str) -> dict[str, Any] | None:
        data = self._load_graph_data(repo)
        return _nodes_by_id(data).get(node_id)

    def context_for_query(
        self,
        repo: str,
        query: str,
        *,
        max_nodes: int,
        include_summaries: bool,
    ) -> dict[str, Any]:
        data = self._load_graph_data(repo)
        terms = _query_terms(query)
        query_lower = query.lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        for node in _nodes(data):
            label = _node_label(node).lower()
            haystack = _node_haystack(node)
            score = 0.0
            if query_lower and query_lower in label:
                score = 1.0
            elif terms:
                matches = sum(1 for term in terms if term in haystack)
                if matches:
                    score = min(0.9, 0.25 + matches / max(len(terms), 1))
            if score > 0:
                scored.append((score, node))
        scored.sort(key=lambda item: item[0], reverse=True)
        matched_nodes = [node for _score, node in scored[:max_nodes]]
        matched_keys = {
            str(value)
            for node in matched_nodes
            for value in (node.get("id"), node.get("label"), node.get("name"))
            if value
        }
        matched_edges = [
            edge for edge in _links(data)
            if str(edge.get("source", edge.get("from", ""))) in matched_keys
            or str(edge.get("target", edge.get("to", ""))) in matched_keys
        ][: max(max_nodes * 2, 10)]
        return {
            "nodes": matched_nodes,
            "edges": matched_edges,
            "summaries": list(self.get_summaries(repo).values())[:5] if include_summaries else [],
            "god_nodes": [node.get("label", "") for node in self.get_gods(repo)[:5]],
        }

    def get_summaries(self, repo: str) -> dict[str, str]:
        path = self._artifact_path(repo, "community_summaries.json")
        if path is None:
            return {}
        data = _load_optional_dict(path)
        return {str(key): str(value) for key, value in data.items()} if data else {}

    def get_gods(self, repo: str) -> list[dict[str, Any]]:
        path = self._artifact_path(repo, ".graphify_analysis.json")
        if path is None:
            return []
        data = _load_optional_dict(path)
        gods = data.get("gods", []) if data else []
        return [node for node in gods if isinstance(node, dict)]

    def query(self, repo: str, query: str, *, limit: int = 10) -> str:
        try:
            data = self._load_graph_data(repo)
        except FileNotFoundError:
            return f"Grafo não encontrado para '{repo}'."
        except (OSError, json.JSONDecodeError) as exc:
            return f"Erro ao ler grafo '{repo}': {exc}"

        terms = _query_terms(query)
        if not terms:
            return "Query demasiado curta para consultar o grafo."

        scored: list[tuple[int, dict[str, Any]]] = []
        for node in _nodes(data):
            haystack = _node_haystack(node)
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, node))

        scored.sort(key=lambda item: item[0], reverse=True)
        matches = [node for _score, node in scored[:limit]]
        if not matches:
            return f"Sem nós relevantes para '{query}' em '{repo}'."

        match_ids = {
            str(value)
            for node in matches
            for value in (node.get("id"), node.get("label"), node.get("name"))
            if value
        }
        related_edges = [
            edge for edge in _links(data)
            if str(edge.get("source", edge.get("from", ""))) in match_ids
            or str(edge.get("target", edge.get("to", ""))) in match_ids
        ][: max(1, limit * 2)]

        lines = [f"Graph query: {repo}", "", "Matched nodes:"]
        for node in matches:
            label = _node_label(node)
            node_type = node.get("type") or node.get("file_type") or ""
            source_file = node.get("source_file") or ""
            suffix = " | ".join(str(part) for part in (node_type, source_file) if part)
            lines.append(f"- {label}" + (f" ({suffix})" if suffix else ""))
        if related_edges:
            lines.extend(["", "Related edges:"])
            for edge in related_edges:
                src = edge.get("source", edge.get("from", "?"))
                tgt = edge.get("target", edge.get("to", "?"))
                rel = edge.get("relation", edge.get("type", "related_to"))
                lines.append(f"- {src} -> {tgt} ({rel})")
        return "\n".join(lines)

    def _load_graph_data(self, repo: str) -> dict[str, Any]:
        graph_path = self._graph_path_resolver(repo)
        if graph_path is None:
            raise FileNotFoundError(
                f"Grafo não encontrado para '{repo}'. "
                'Chama POST /admin/reprocess {"target":"graph"} primeiro.'
            )
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("graph.json must contain an object", doc=str(data), pos=0)
        return data

    def _artifact_path(self, repo: str, filename: str) -> Path | None:
        graph_path = self._graph_path_resolver(repo)
        if graph_path is None:
            return None
        candidate = graph_path.parent / filename
        return candidate if candidate.exists() else None


_DEFAULT_BACKEND = JsonGraphBackend()
_BACKEND_CACHE: dict[tuple[Any, ...], GraphBackend] = {}


def get_graph_backend() -> GraphBackend:
    try:
        from obsidian_rag.config import settings
    except Exception:
        return _DEFAULT_BACKEND

    backend_name = str(getattr(settings.graphify, "query_backend", "json") or "json").strip().lower()
    if backend_name == "json":
        return _DEFAULT_BACKEND
    if backend_name == "falkordb":
        cache_key = (
            backend_name,
            getattr(settings.graphify, "falkor_host", "localhost"),
            int(getattr(settings.graphify, "falkor_port", 6379)),
            getattr(settings.graphify, "falkor_graph", "obsidian_rag"),
            getattr(settings.graphify, "falkor_username", ""),
            getattr(settings.graphify, "falkor_password", ""),
            bool(getattr(settings.graphify, "falkor_ssl", False)),
        )
        cached = _BACKEND_CACHE.get(cache_key)
        if cached is not None:
            return cached
        from obsidian_rag.pipeline.graph.falkor_backend import FalkorGraphBackend

        backend = FalkorGraphBackend.from_settings(settings.graphify)
        _BACKEND_CACHE[cache_key] = backend
        return backend
    raise ValueError(f"Unknown graph query backend: {backend_name!r} (expected 'json' or 'falkordb')")


def reset_graph_backend_cache() -> None:
    _BACKEND_CACHE.clear()


def _nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in data.get("nodes", []) if isinstance(node, dict)]


def _links(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_links = data.get("links", data.get("edges", []))
    return [link for link in raw_links if isinstance(link, dict)]


def _nodes_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node.get("id")): node for node in _nodes(data) if node.get("id") is not None}


def _node_label(node: dict[str, Any]) -> str:
    return str(node.get("label") or node.get("name") or node.get("id") or "")


def _node_haystack(node: dict[str, Any]) -> str:
    return " ".join(
        str(node.get(field, ""))
        for field in ("id", "label", "name", "type", "file_type", "source_file")
    ).lower()


def _normalize_label(label: str) -> str:
    normalized = label.lower().strip()
    normalized = re.sub(r"\s*\(parte\s+\d+\)$", "", normalized)
    normalized = re.sub(r"\s*\(module-level\)$", "", normalized)
    normalized = normalized.rstrip("()")
    return normalized.lstrip(".")


def _node_for_chunk(nodes: list[dict[str, Any]], source_file: str, section_header: str) -> dict[str, Any] | None:
    norm = _normalize_label(section_header)
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        node_source = str(node.get("source_file", ""))
        node_label = _node_label(node)
        if node_source and node_label:
            indexed[(node_source, _normalize_label(node_label))] = node

    node = indexed.get((source_file, norm))
    if node is not None:
        return node
    for (node_source, node_label), candidate in indexed.items():
        if node_label != norm:
            continue
        if source_file.endswith(node_source) or node_source.endswith(source_file):
            return candidate
    for (node_source, node_label), candidate in indexed.items():
        source_match = (
            node_source == source_file
            or source_file.endswith(node_source)
            or node_source.endswith(source_file)
        )
        if source_match and (norm in node_label or node_label in norm):
            return candidate
    return None


def _find_node(data: dict[str, Any], label: str) -> dict[str, Any] | None:
    needle = label.lower()
    for node in _nodes(data):
        node_id = str(node.get("id", ""))
        node_label = _node_label(node)
        if needle == node_id.lower() or needle == node_label.lower():
            return node
    for node in _nodes(data):
        node_label = _node_label(node)
        if needle in node_label.lower() or node_label.lower() in needle:
            return node
    return None


def _normalized_link(link: dict[str, Any]) -> dict[str, Any] | None:
    source = link.get("source", link.get("from"))
    target = link.get("target", link.get("to"))
    if source is None or target is None:
        return None
    normalized = dict(link)
    normalized["source"] = str(source)
    normalized["target"] = str(target)
    return normalized


def _adjacency(data: dict[str, Any]) -> dict[str, list[tuple[dict[str, Any], str]]]:
    adjacent: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for link in _links(data):
        normalized = _normalized_link(link)
        if normalized is None:
            continue
        adjacent.setdefault(str(normalized["source"]), []).append((normalized, "outgoing"))
    return adjacent


def _reverse_adjacency(data: dict[str, Any]) -> dict[str, list[tuple[dict[str, Any], str]]]:
    reverse: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for link in _links(data):
        normalized = _normalized_link(link)
        if normalized is None:
            continue
        reverse.setdefault(str(normalized["target"]), []).append((normalized, "incoming"))
    return reverse


def _neighbor_row(
    node: dict[str, Any],
    edge: dict[str, Any],
    direction: str,
    depth: int,
) -> dict[str, Any]:
    return {
        "id": str(node.get("id", _node_label(node))),
        "label": _node_label(node),
        "file_type": node.get("file_type", ""),
        "source_file": node.get("source_file", ""),
        "relation": edge.get("relation", edge.get("type", "related_to")),
        "confidence": edge.get("confidence", "EXTRACTED"),
        "direction": direction,
        "depth": depth,
    }


def _query_terms(query: str) -> set[str]:
    return {
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9_./-]+", query.lower())
        if len(term) > 2
        and term not in {"the", "and", "for", "com", "uma", "que", "para", "como"}
    }


def _graph_artifact_path(repo: str, filename: str) -> Path | None:
    graph_path = get_graph_json_path(repo)
    if graph_path is None:
        return None
    candidate = graph_path.parent / filename
    return candidate if candidate.exists() else None


def _load_optional_dict(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
