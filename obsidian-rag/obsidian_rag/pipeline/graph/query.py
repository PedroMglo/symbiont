"""Public graph query helpers backed by the configured GraphBackend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obsidian_rag.pipeline.graph.backend import get_graph_backend
from obsidian_rag.pipeline.graph.builder import (
    _configured_graph_roots,
    _graphify_output_dir,
    get_report_path,
)


def load_graph(repo_name: str) -> dict[str, Any]:
    """Load the raw Graphify graph artifact for graph query callers."""
    backend = get_graph_backend()
    if hasattr(backend, "load_raw_graph"):
        return backend.load_raw_graph(repo_name)  # type: ignore[no-any-return, attr-defined]
    raise FileNotFoundError(f"Grafo não encontrado para '{repo_name}'.")


def get_report(repo_name: str) -> str:
    """Lê o relatório de análise de um repo como texto Markdown.

    Procura por ordem: GRAPH_REPORT.md → .graphify_analysis.json (Graphify v0.7+).
    Se só existir o JSON, gera um relatório Markdown a partir dos dados.

    Raises:
        FileNotFoundError: se nenhum dos dois existir
    """
    report_path = get_report_path(repo_name)
    if report_path is not None:
        return report_path.read_text(encoding="utf-8")

    analysis_path = _get_analysis_json_path(repo_name)
    if analysis_path is None:
        raise FileNotFoundError(
            f"Relatório não encontrado para '{repo_name}'. "
            'Chama POST /admin/reprocess {"target":"graph"} primeiro.'
        )
    return _analysis_to_markdown(repo_name, analysis_path)


def _get_analysis_json_path(repo_name: str) -> Path | None:
    """Devolve o path para .graphify_analysis.json, ou None."""
    for repo_path in _configured_graph_roots():
        p = Path(repo_path)
        if p.name == repo_name:
            ap = _graphify_output_dir(p) / ".graphify_analysis.json"
            return ap if ap.exists() else None
    return None


def _analysis_to_markdown(repo_name: str, path: Path) -> str:
    """Converte .graphify_analysis.json em relatório Markdown legível."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    lines = [f"# Graph Report — {repo_name}", ""]

    gods = data.get("gods", [])
    if gods:
        lines.append("## God Nodes (mais conectados)")
        lines.append("")
        lines.append("| Node | Label | Degree |")
        lines.append("|------|-------|--------|")
        for g in gods:
            lines.append(f"| `{g.get('id', '')}` | {g.get('label', '')} | {g.get('degree', 0)} |")
        lines.append("")

    surprises = data.get("surprises", [])
    if surprises:
        lines.append("## Conexões Surpreendentes")
        lines.append("")
        for s in surprises:
            src = s.get("source", "?")
            tgt = s.get("target", "?")
            rel = s.get("relation", "?")
            why = s.get("why", "")
            lines.append(f"- **{src}** → **{tgt}** ({rel})")
            if why:
                lines.append(f"  - {why}")
        lines.append("")

    communities = data.get("communities", {})
    if communities:
        lines.append(f"## Comunidades ({len(communities)} detectadas)")
        lines.append("")
        for cid, members in communities.items():
            lines.append(f"### Comunidade {cid} ({len(members)} membros)")
            shown = members[:10]
            lines.append(", ".join(f"`{m}`" for m in shown))
            if len(members) > 10:
                lines.append(f"  ... e mais {len(members) - 10}")
            lines.append("")

    tokens = data.get("tokens", {})
    if tokens:
        lines.append("## Tokens")
        lines.append(f"- Input: {tokens.get('input', 0):,}")
        lines.append(f"- Output: {tokens.get('output', 0):,}")
        lines.append("")

    return "\n".join(lines)


def get_neighbors(repo_name: str, node_label: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Devolve os nós vizinhos de um conceito no grafo."""
    return get_graph_backend().neighbors(repo_name, node_label, depth=1, limit=max_results)


def query_graph(repo_name: str, query: str) -> str:
    """Executa uma query local ao graph backend configurado."""
    return get_graph_backend().query(repo_name, query)


def shortest_path(repo_name: str, source_label: str, target_label: str) -> list[str]:
    """Devolve o caminho mais curto entre dois nós no grafo."""
    paths = get_graph_backend().shortest_paths(repo_name, source_label, target_label, limit=1)
    return paths[0] if paths else []


def list_repos() -> list[dict[str, Any]]:
    """Lista todas as roots configuradas com status do grafo."""
    backend = get_graph_backend()
    result = []
    for repo_path in _configured_graph_roots():
        p = Path(repo_path)
        name = p.name
        stats = backend.stats(name)
        report_path = get_report_path(name)

        result.append({
            "name": name,
            "path": str(p),
            "exists": p.exists(),
            "graph_built": stats.graph_built,
            "graph_path": stats.graph_path,
            "report_path": str(report_path) if report_path else None,
            "node_count": stats.node_count,
            "edge_count": stats.edge_count,
            "graph_backend": getattr(backend, "name", "unknown"),
        })
    return result
