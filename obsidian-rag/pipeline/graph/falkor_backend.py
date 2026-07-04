"""FalkorDB GraphBackend adapter.

FalkorDB is optional and only imported when the RAG graph query backend is set
to ``falkordb``. Graphify remains the producer; this adapter imports Graphify
``graph.json`` data into a service-backed graph for query/runtime use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pipeline.graph.backend import (
    GraphImportResult,
    GraphStats,
    _graph_artifact_path,
    _links,
    _load_optional_dict,
    _node_label,
    _nodes,
    _query_terms,
)

FalkorClientFactory = Callable[[], Any]
_CYPHER_DIR = Path(__file__).resolve().parent / "cypher"
_CYPHER_CACHE: dict[str, str] = {}


def _cypher(name: str) -> str:
    text = _CYPHER_CACHE.get(name)
    if text is None:
        text = (_CYPHER_DIR / name).read_text(encoding="utf-8").strip()
        _CYPHER_CACHE[name] = text
    return text


@dataclass(frozen=True)
class FalkorConnectionConfig:
    host: str = "localhost"
    port: int = 6379
    graph_name: str = "obsidian_rag"
    username: str = ""
    password: str = ""
    ssl: bool = False


class FalkorGraphBackend:
    """GraphBackend implementation over FalkorDB/openCypher."""

    name = "falkordb"

    def __init__(
        self,
        config: FalkorConnectionConfig | None = None,
        *,
        client_factory: FalkorClientFactory | None = None,
    ) -> None:
        self.config = config or FalkorConnectionConfig()
        self._client_factory = client_factory
        self._graph: Any | None = None

    @classmethod
    def from_settings(cls, graphify_settings: Any) -> "FalkorGraphBackend":
        return cls(
            FalkorConnectionConfig(
                host=str(getattr(graphify_settings, "falkor_host", "localhost")),
                port=int(getattr(graphify_settings, "falkor_port", 6379)),
                graph_name=str(getattr(graphify_settings, "falkor_graph", "obsidian_rag")),
                username=str(getattr(graphify_settings, "falkor_username", "") or ""),
                password=str(getattr(graphify_settings, "falkor_password", "") or ""),
                ssl=bool(getattr(graphify_settings, "falkor_ssl", False)),
            )
        )

    def health(self, repo: str | None = None) -> dict[str, Any]:
        try:
            self._graph_client().ro_query("RETURN 1")
        except Exception as exc:
            return {
                "backend": self.name,
                "ok": False,
                "repo": repo,
                "graph": self.config.graph_name,
                "error": str(exc)[:200],
            }
        return {"backend": self.name, "ok": True, "repo": repo, "graph": self.config.graph_name}

    def stats(self, repo: str) -> GraphStats:
        result = self._graph_client().ro_query(
            "MATCH (m:GraphImport {repo: $repo}) "
            "RETURN m.source_hash, m.node_count, m.edge_count LIMIT 1",
            {"repo": repo},
        )
        rows = _result_rows(result)
        if not rows:
            return GraphStats(repo=repo, graph_built=False)
        row = rows[0]
        return GraphStats(
            repo=repo,
            graph_built=True,
            node_count=_int_or_none(_row_value(row, 1)),
            edge_count=_int_or_none(_row_value(row, 2)),
            graph_path=f"falkordb://{self.config.host}:{self.config.port}/{self.config.graph_name}/{repo}",
        )

    def import_graph(self, repo: str, graph: dict[str, Any], *, source_hash: str) -> GraphImportResult:
        nodes = [_falkor_node(node) for node in _nodes(graph)]
        edges = [
            edge
            for edge in (_falkor_edge(link, index) for index, link in enumerate(_links(graph)))
            if edge is not None
        ]

        current_hash = self._current_source_hash(repo)
        if current_hash == source_hash:
            return GraphImportResult(
                repo=repo,
                backend=self.name,
                source_hash=source_hash,
                node_count=len(nodes),
                edge_count=len(edges),
                skipped=True,
            )

        graph_client = self._graph_client()
        graph_client.query(_cypher("falkor_delete_nodes.cypher"), {"repo": repo})
        graph_client.query(_cypher("falkor_delete_import_marker.cypher"), {"repo": repo})

        if nodes:
            graph_client.query(
                _cypher("falkor_import_nodes.cypher"),
                {"repo": repo, "nodes": nodes},
            )
        if edges:
            graph_client.query(
                _cypher("falkor_import_edges.cypher"),
                {"repo": repo, "edges": edges},
            )
        graph_client.query(
            _cypher("falkor_import_marker.cypher"),
            {
                "repo": repo,
                "source_hash": source_hash,
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        )
        return GraphImportResult(
            repo=repo,
            backend=self.name,
            source_hash=source_hash,
            node_count=len(nodes),
            edge_count=len(edges),
            skipped=False,
        )

    def neighbors(self, repo: str, node: str, *, depth: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        graph_client = self._graph_client()
        params = {"repo": repo, "node": node, "limit": max(1, limit)}
        outgoing = graph_client.ro_query(
            _cypher("falkor_neighbors_outgoing.cypher"),
            params,
        )
        rows = [_neighbor_from_row(row) for row in _result_rows(outgoing)]
        if len(rows) >= limit:
            return rows[:limit]

        incoming = graph_client.ro_query(
            _cypher("falkor_neighbors_incoming.cypher"),
            params,
        )
        rows.extend(_neighbor_from_row(row) for row in _result_rows(incoming))
        return rows[:limit]

    def shortest_paths(self, repo: str, source: str, target: str, *, limit: int = 1) -> list[list[str]]:
        result = self._graph_client().ro_query(
            _cypher("falkor_shortest_paths.cypher"),
            {"repo": repo, "source": source, "target": target, "limit": max(1, limit)},
        )
        paths: list[list[str]] = []
        for row in _result_rows(result):
            value = _row_value(row, 0)
            if isinstance(value, list):
                paths.append([str(item) for item in value])
        return paths

    def subgraph_for_chunks(self, repo: str, chunk_ids: list[str], *, budget: int) -> dict[str, list[dict[str, Any]]]:
        terms = sorted(_query_terms(" ".join(chunk_ids)))
        if not terms or budget <= 0:
            return {"nodes": [], "links": []}
        result = self._graph_client().ro_query(
            _cypher("falkor_subgraph_for_chunks.cypher"),
            {"repo": repo, "terms": terms, "budget": max(1, budget)},
        )
        nodes_by_id: dict[str, dict[str, Any]] = {}
        links: list[dict[str, Any]] = []
        for row in _result_rows(result):
            first = _node_from_values(row, 0)
            if first["id"]:
                nodes_by_id[first["id"]] = first
            second = _node_from_values(row, 5)
            if second["id"]:
                nodes_by_id[second["id"]] = second
                links.append({
                    "source": first["id"],
                    "target": second["id"],
                    "relation": _row_value(row, 10) or "related_to",
                    "confidence": _row_value(row, 11) or "EXTRACTED",
                })
        return {"nodes": list(nodes_by_id.values())[:budget], "links": links[:budget]}

    def node_for_chunk(self, repo: str, source_file: str, section_header: str) -> dict[str, Any] | None:
        result = self._graph_client().ro_query(
            _cypher("falkor_node_for_chunk.cypher"),
            {"repo": repo, "source_file": source_file, "section_header": section_header},
        )
        rows = _result_rows(result)
        return _node_from_values(rows[0], 0) if rows else None

    def node_by_id(self, repo: str, node_id: str) -> dict[str, Any] | None:
        result = self._graph_client().ro_query(
            _cypher("falkor_node_by_id.cypher"),
            {"repo": repo, "node_id": node_id},
        )
        rows = _result_rows(result)
        return _node_from_values(rows[0], 0) if rows else None

    def get_summaries(self, repo: str) -> dict[str, str]:
        path = _graph_artifact_path(repo, "community_summaries.json")
        if path is None:
            return {}
        data = _load_optional_dict(path)
        return {str(key): str(value) for key, value in data.items()} if data else {}

    def get_gods(self, repo: str) -> list[dict[str, Any]]:
        path = _graph_artifact_path(repo, ".graphify_analysis.json")
        if path is None:
            return []
        data = _load_optional_dict(path)
        gods = data.get("gods", []) if data else []
        return [node for node in gods if isinstance(node, dict)]

    def query(self, repo: str, query: str, *, limit: int = 10) -> str:
        terms = sorted(_query_terms(query))
        if not terms:
            return "Query demasiado curta para consultar o grafo."
        result = self._graph_client().ro_query(
            _cypher("falkor_query.cypher"),
            {"repo": repo, "terms": terms, "limit": max(1, limit)},
        )
        rows = _result_rows(result)
        if not rows:
            return f"Sem nós relevantes para '{query}' em '{repo}'."

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[tuple[str, str, str]] = []
        for row in rows:
            node = _node_from_values(row, 0)
            if node["id"]:
                nodes[node["id"]] = node
            target = _row_value(row, 5)
            if target:
                edges.append((str(_row_value(row, 0)), str(target), str(_row_value(row, 7) or "related_to")))

        lines = [f"Graph query: {repo}", "", "Matched nodes:"]
        for node in nodes.values():
            suffix = " | ".join(str(part) for part in (node.get("type"), node.get("source_file")) if part)
            lines.append(f"- {node['label'] or node['id']}" + (f" ({suffix})" if suffix else ""))
        if edges:
            lines.extend(["", "Related edges:"])
            for source, target, relation in edges[: max(1, limit * 2)]:
                lines.append(f"- {source} -> {target} ({relation})")
        return "\n".join(lines)

    def _current_source_hash(self, repo: str) -> str | None:
        result = self._graph_client().ro_query(
            "MATCH (m:GraphImport {repo: $repo}) RETURN m.source_hash LIMIT 1",
            {"repo": repo},
        )
        rows = _result_rows(result)
        if not rows:
            return None
        value = _row_value(rows[0], 0)
        return str(value) if value else None

    def _graph_client(self) -> Any:
        if self._graph is not None:
            return self._graph
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            try:
                from falkordb import FalkorDB
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "FalkorDB backend selected but Python package 'FalkorDB' is not installed. "
                    "Install obsidian-rag with the falkordb extra."
                ) from exc

            kwargs: dict[str, Any] = {"host": self.config.host, "port": self.config.port}
            if self.config.username:
                kwargs["username"] = self.config.username
            if self.config.password:
                kwargs["password"] = self.config.password
            if self.config.ssl:
                kwargs["ssl"] = True
            client = FalkorDB(**kwargs)
        self._graph = client.select_graph(self.config.graph_name)
        return self._graph


def _falkor_node(node: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("id") or node.get("label") or node.get("name") or "")
    return {
        "id": node_id,
        "label": _node_label(node),
        "name": str(node.get("name") or ""),
        "type": str(node.get("type") or ""),
        "file_type": str(node.get("file_type") or ""),
        "source_file": str(node.get("source_file") or ""),
        "props_json": json.dumps(node, ensure_ascii=False, sort_keys=True),
    }


def _falkor_edge(link: dict[str, Any], index: int) -> dict[str, Any] | None:
    source = link.get("source", link.get("from"))
    target = link.get("target", link.get("to"))
    if source is None or target is None:
        return None
    relation = str(link.get("relation", link.get("type", "related_to")) or "related_to")
    return {
        "source": str(source),
        "target": str(target),
        "relation": relation,
        "confidence": str(link.get("confidence", "EXTRACTED") or "EXTRACTED"),
        "edge_id": f"{source}|{relation}|{target}|{index}",
        "props_json": json.dumps(link, ensure_ascii=False, sort_keys=True),
    }


def _result_rows(result: Any) -> list[Any]:
    return list(getattr(result, "result_set", []) or [])


def _row_value(row: Any, index: int) -> Any:
    if isinstance(row, dict):
        try:
            return list(row.values())[index]
        except IndexError:
            return None
    try:
        return row[index]
    except (IndexError, TypeError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _neighbor_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(_row_value(row, 0) or ""),
        "label": str(_row_value(row, 1) or _row_value(row, 0) or ""),
        "file_type": str(_row_value(row, 2) or ""),
        "source_file": str(_row_value(row, 3) or ""),
        "relation": str(_row_value(row, 4) or "related_to"),
        "confidence": str(_row_value(row, 5) or "EXTRACTED"),
        "direction": str(_row_value(row, 6) or "outgoing"),
        "depth": int(_row_value(row, 7) or 1),
    }


def _node_from_values(row: Any, offset: int) -> dict[str, Any]:
    node_id = str(_row_value(row, offset) or "")
    return {
        "id": node_id,
        "label": str(_row_value(row, offset + 1) or node_id),
        "type": str(_row_value(row, offset + 2) or ""),
        "file_type": str(_row_value(row, offset + 3) or ""),
        "source_file": str(_row_value(row, offset + 4) or ""),
    }
