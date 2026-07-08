"""Exportação de grafos Graphify para um vault Obsidian dedicado.

Gera notas Markdown estruturadas com wikilinks para navegação visual
no Obsidian e para injecção de contexto estrutural em LLMs locais.

Estrutura gerada:
  {vault}/
  ├── index.md                      ← índice global de todos os repos
  └── {repo_name}/
      ├── Overview.md               ← god nodes, surpresas, índice de comunidades
      ├── Community-{id}.md         ← membros, sumário, cross-links, Mermaid
      └── nodes/
          └── {symbol}.md           ← top god nodes com vizinhos

Executado automaticamente em reprocessamentos de grafo via API admin.
Override: RAG_GRAPHIFY_GRAPH_VAULT_DIR=/outro/caminho antes de chamar POST /admin/reprocess.

Supports incremental export (export_incremental=True in config):
tracks content hashes per community/overview to avoid rewriting unchanged notes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_config import settings

# ---------------------------------------------------------------------------
# Vault init
# ---------------------------------------------------------------------------

def init_vault(vault_dir: Path) -> None:
    """Garante que o vault Obsidian existe e tem a configuração mínima."""
    vault_dir.mkdir(parents=True, exist_ok=True)
    obsidian_dir = vault_dir / ".obsidian"
    obsidian_dir.mkdir(exist_ok=True)

    app_json = obsidian_dir / "app.json"
    if not app_json.exists():
        app_json.write_text(
            '{"defaultViewMode":"source","livePreview":true}\n',
            encoding="utf-8",
        )


def _purge_generated_vault(vault_dir: Path) -> tuple[int, int]:
    """Remove generated graph notes while preserving Obsidian app config."""
    files_removed = 0
    dirs_removed = 0
    if not vault_dir.exists():
        return files_removed, dirs_removed
    for child in vault_dir.iterdir():
        if child.name == ".obsidian":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
            dirs_removed += 1
        else:
            child.unlink(missing_ok=True)
            files_removed += 1
    return files_removed, dirs_removed


def purge_generated_vault(vault_dir: Path | None = None) -> tuple[int, int]:
    """Clear generated graph notes from the configured Obsidian vault."""
    target = Path(vault_dir or settings.graphify.graph_vault_dir)
    init_vault(target)
    return _purge_generated_vault(target)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:  # type: ignore[type-arg]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
        return dict(data)


def _node_label(node_id: str, nodes_by_id: dict[str, dict]) -> str:  # type: ignore[type-arg]
    """Devolve o label legível de um node_id."""
    n = nodes_by_id.get(node_id, {})
    return str(n.get("label", node_id))


def _safe_wikilink(text: str) -> str:
    """Remove caracteres inválidos para wikilinks Obsidian."""
    return text.replace("[", "").replace("]", "").replace("|", "-").replace("#", "")


def _yaml_str(value: str) -> str:
    """Escapa valor para frontmatter YAML (entre aspas se tiver caracteres especiais)."""
    if any(c in value for c in (':', '#', '[', ']', '{', '}', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`')):
        return f'"{value}"'
    return value


# ---------------------------------------------------------------------------
# Incremental export state
# ---------------------------------------------------------------------------

def _load_export_state(repo_dir: Path) -> dict[str, str]:
    """Load export_state.json: {note_key: content_hash}."""
    state_path = repo_dir / ".export_state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_export_state(repo_dir: Path, state: dict[str, str]) -> None:
    """Persist export_state.json."""
    state_path = repo_dir / ".export_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(state_path)


def _content_hash(content: str) -> str:
    """SHA256[:16] of content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _should_write(key: str, content: str, state: dict[str, str], new_state: dict[str, str]) -> bool:
    """Check if a note needs writing (content changed from last export)."""
    h = _content_hash(content)
    new_state[key] = h
    if not settings.graphify.export_incremental:
        return True
    return state.get(key) != h


# ---------------------------------------------------------------------------
# Community notes
# ---------------------------------------------------------------------------

def _write_community_note(
    community_id: str,
    member_ids: list[str],
    nodes_by_id: dict[str, dict],
    repo_dir: Path,
    repo_name: str,
    *,
    summary: str | None = None,
    cross_links: list[dict] | None = None,
    mermaid: str | None = None,
    tags: list[str] | None = None,
    surprises_for_community: list[dict] | None = None,
) -> Path | None:
    """Escreve Community-{id}.md. Retorna o path ou None se skipped (<5 membros)."""
    if len(member_ids) < 5:
        return None

    # Agrupar por source_file para visualização estruturada
    by_file: dict[str, list[str]] = {}
    for mid in member_ids:
        node = nodes_by_id.get(mid, {})
        src = node.get("source_file", "unknown")
        label = node.get("label", mid)
        by_file.setdefault(src, []).append(label)

    tag_list = tags or []
    tag_yaml = ", ".join(tag_list) if tag_list else ""

    lines = [
        "---",
        f"repo: {repo_name}",
        f"community_id: {community_id}",
        f"members_count: {len(member_ids)}",
    ]
    if tag_yaml:
        lines.append(f"tags: [{tag_yaml}]")
    lines += [
        "---",
        "",
        f"# Community {community_id} — {repo_name}",
        "",
        f"> [[Overview]] | {len(member_ids)} membros",
        "",
    ]

    # Summary (LLM-generated)
    if summary:
        lines += [
            "> [!abstract] Propósito",
            f"> {summary}",
            "",
        ]

    # Mermaid diagram
    if mermaid:
        lines += [
            "## Fluxo de Chamadas",
            "",
            "```mermaid",
            mermaid,
            "```",
            "",
        ]

    # Cross-community links
    if cross_links:
        lines += [
            "## Comunidades Relacionadas",
            "",
        ]
        for cl in cross_links[:5]:  # top 5
            other = cl["other_id"]
            shared = cl["shared_files"]
            files_str = ", ".join(f"`{f}`" for f in shared[:3])
            extra = f" (+{len(shared)-3})" if len(shared) > 3 else ""
            lines.append(f"- [[Community-{other}]] — {len(shared)} ficheiro(s) partilhado(s): {files_str}{extra}")
        lines.append("")

    # Surprises involving this community
    if surprises_for_community:
        lines += [
            "## Conexões Surpreendentes",
            "",
        ]
        for s in surprises_for_community:
            src_label = s.get("source", "?")
            tgt_label = s.get("target", "?")
            why = s.get("why", "")
            lines.append(f"> [!tip] `{src_label}` → `{tgt_label}`")
            if why:
                lines.append(f"> {why}")
            lines.append("")

    # Members by file
    lines += [
        "## Membros por ficheiro",
        "",
    ]
    for src_file in sorted(by_file.keys()):
        lines.append(f"### `{src_file}`")
        for lbl in sorted(by_file[src_file]):
            lines.append(f"- {lbl}")
        lines.append("")

    note_path = repo_dir / f"Community-{community_id}.md"
    note_path.write_text("\n".join(lines), encoding="utf-8")
    return note_path


# ---------------------------------------------------------------------------
# Overview note
# ---------------------------------------------------------------------------

def _write_overview_note(
    repo_name: str,
    repo_source_path: Path,
    graph_path: Path,
    analysis: dict,
    graph_data: dict,
    community_ids_written: list[str],
    repo_dir: Path,
    *,
    god_node_names: list[str] | None = None,
) -> Path:
    """Escreve Overview.md com god nodes, surpresas e índice de comunidades."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    nodes: list[dict] = graph_data.get("nodes", [])
    links: list[dict] = graph_data.get("links", [])
    gods: list[dict] = analysis.get("gods", [])
    surprises: list[dict] = analysis.get("surprises", [])
    tokens: dict = analysis.get("tokens", {})
    god_node_set = set(god_node_names or [])

    lines = [
        "---",
        f"repo: {repo_name}",
        f"source_path: {_yaml_str(str(repo_source_path))}",
        f"graph_path: {_yaml_str(str(graph_path))}",
        f"generated_at: {now}",
        f"nodes: {len(nodes)}",
        f"edges: {len(links)}",
        f"communities: {len(community_ids_written)}",
        "---",
        "",
        f"# {repo_name} — Knowledge Graph",
        "",
        "> [!info] Projecto fonte",
        f"> **Path:** `{repo_source_path}`",
        f"> **Grafo:** `{graph_path}`",
        f"> **Gerado:** {now}",
        f"> **Nodes:** {len(nodes)} | **Edges:** {len(links)}",
        "",
    ]

    # God nodes
    if gods:
        lines += [
            "## God Nodes",
            "",
            "> Nós mais conectados — prováveis pontos de entrada no código",
            "",
            "| Símbolo | Ficheiro | Degree |",
            "|---------|----------|--------|",
        ]
        nodes_by_id = {n["id"]: n for n in nodes}
        for g in gods:
            node = nodes_by_id.get(g.get("id", ""), {})
            src = node.get("source_file", "")
            label = g.get("label", g.get("id", ""))
            degree = g.get("degree", 0)
            safe = _safe_wikilink(label)
            if safe in god_node_set:
                lines.append(f"| [[nodes/{safe}\\|{label}]] | `{src}` | {degree} |")
            else:
                lines.append(f"| `{label}` | `{src}` | {degree} |")
        lines.append("")

    # Surprises
    if surprises:
        lines += [
            "## Conexões Surpreendentes",
            "",
        ]
        for s in surprises:
            src_label = s.get("source", "?")
            tgt_label = s.get("target", "?")
            rel = s.get("relation", "?")
            why = s.get("why", "")
            src_files = s.get("source_files", [])
            lines.append(f"### `{src_label}` → `{tgt_label}`")
            lines.append(f"**Relação:** {rel}")
            if src_files:
                lines.append(f"**Ficheiros:** {', '.join(f'`{f}`' for f in src_files)}")
            if why:
                lines.append(f"**Porquê:** {why}")
            lines.append("")

    # Communities index
    if community_ids_written:
        lines += [
            "## Comunidades",
            "",
            "> Grupos de módulos fortemente relacionados (só comunidades com ≥ 5 membros)",
            "",
        ]
        for cid in sorted(community_ids_written, key=lambda x: int(x)):
            lines.append(f"- [[Community-{cid}]]")
        lines.append("")

    # Tokens
    if tokens:
        lines += [
            "## Tokens LLM",
            "",
            f"- Input: {tokens.get('input', 0):,}",
            f"- Output: {tokens.get('output', 0):,}",
            "",
        ]

    note_path = repo_dir / "Overview.md"
    note_path.write_text("\n".join(lines), encoding="utf-8")
    return note_path


# ---------------------------------------------------------------------------
# God Node notes
# ---------------------------------------------------------------------------

def _write_god_node_notes(
    gods: list[dict],
    graph_data: dict,
    analysis: dict,
    repo_dir: Path,
    repo_name: str,
    *,
    max_nodes: int = 10,
) -> list[str]:
    """Escreve notas individuais para os top god nodes.

    Cria {repo_dir}/nodes/{symbol}.md com vizinhos e metadata.
    Retorna lista de safe node names escritos (para wikilinks no Overview).
    """
    nodes_dir = repo_dir / "nodes"
    nodes_dir.mkdir(exist_ok=True)

    nodes_by_id = {n["id"]: n for n in graph_data.get("nodes", [])}
    links = graph_data.get("links", [])

    # Build adjacency
    neighbors: dict[str, list[dict]] = {}
    for link in links:
        s, t = link["source"], link["target"]
        rel = link.get("relation", "related_to")
        conf = link.get("confidence", "EXTRACTED")

        if s not in neighbors:
            neighbors[s] = []
        neighbors[s].append({"id": t, "relation": rel, "confidence": conf, "direction": "outgoing"})

        if t not in neighbors:
            neighbors[t] = []
        neighbors[t].append({"id": s, "relation": rel, "confidence": conf, "direction": "incoming"})

    written = []
    for g in gods[:max_nodes]:
        node_id = g.get("id", "")
        node = nodes_by_id.get(node_id, {})
        label = g.get("label", node_id)
        safe = _safe_wikilink(label)
        degree = g.get("degree", 0)
        src_file = node.get("source_file", "")
        src_loc = node.get("source_location", "")
        community = node.get("community", "?")

        # Get neighbors sorted by relation
        node_neighbors = neighbors.get(node_id, [])
        outgoing = [n for n in node_neighbors if n["direction"] == "outgoing"]
        incoming = [n for n in node_neighbors if n["direction"] == "incoming"]

        lines = [
            "---",
            f"repo: {repo_name}",
            f"symbol: {_yaml_str(label)}",
            f"source_file: {_yaml_str(src_file)}",
            f"degree: {degree}",
            f"community: {community}",
            "tags: [god-node]",
            "---",
            "",
            f"# `{label}`",
            "",
            f"> [[../Overview]] | [[../Community-{community}]] | Degree: {degree}",
            "",
            f"**Ficheiro:** `{src_file}` {src_loc}",
            "",
        ]

        if outgoing:
            lines += ["## Chamadas / Dependências (outgoing)", ""]
            for nb in outgoing[:15]:
                nb_label = nodes_by_id.get(nb["id"], {}).get("label", nb["id"])
                rel = nb["relation"]
                lines.append(f"- `{nb_label}` ← {rel}")
            lines.append("")

        if incoming:
            lines += ["## Usado por (incoming)", ""]
            for nb in incoming[:15]:
                nb_label = nodes_by_id.get(nb["id"], {}).get("label", nb["id"])
                rel = nb["relation"]
                lines.append(f"- `{nb_label}` → {rel}")
            lines.append("")

        note_path = nodes_dir / f"{safe}.md"
        note_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(safe)

    return written


# ---------------------------------------------------------------------------
# Index note
# ---------------------------------------------------------------------------

def _write_index_note(
    vault_dir: Path,
    repo_summaries: list[dict],
) -> Path:
    """Escreve index.md na raiz do vault com tabela de todos os repos."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "---",
        f"generated_at: {now}",
        f"repos_count: {len(repo_summaries)}",
        "tags: [knowledge-graph, rag, index]",
        "---",
        "",
        "# Knowledge Graphs — Índice",
        "",
        f"> Gerado por POST /admin/reprocess target=graph em {now}",
        "> Vault dedicado à visualização estrutural de repositórios de código.",
        "",
        "## Repositórios",
        "",
        "| Repo | Nodes | Edges | Comunidades | God Nodes | Fonte | Actualizado |",
        "|------|-------|-------|-------------|-----------|-------|-------------|",
    ]

    for s in repo_summaries:
        name = s["name"]
        nodes = s.get("nodes", 0)
        edges = s.get("edges", 0)
        communities = s.get("communities", 0)
        god_nodes = s.get("god_nodes", 0)
        source = s.get("source_path", "")
        updated = s.get("generated_at", now)
        lines.append(
            f"| [[{name}/Overview\\|{name}]] | {nodes} | {edges} | {communities} | {god_nodes} | `{source}` | {updated} |"
        )

    lines += [
        "",
        "## Navegação",
        "",
    ]
    for s in repo_summaries:
        lines.append(f"- [[{s['name']}/Overview]]")
    lines.append("")

    index_path = vault_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_repo(
    repo_name: str,
    repo_source_path: Path,
    graph_path: Path,
    analysis_path: Path,
    vault_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Exporta um repo para o vault Obsidian com enriquecimentos.

    Cria:
      {vault}/{repo_name}/Overview.md
      {vault}/{repo_name}/Community-{id}.md  (com sumários, cross-links, Mermaid)
      {vault}/{repo_name}/nodes/{symbol}.md  (god nodes)

    Retorna dict com stats para o index.
    """
    from pipeline.graph.enrich import (
        detect_cross_community_links,
        generate_community_mermaid,
        infer_repo_tags,
        summarize_communities,
    )

    repo_dir = vault_dir / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)

    new_state: dict[str, str] = {}

    graph_data = _load_json(graph_path)
    analysis = _load_json(analysis_path)

    nodes: list[dict] = graph_data.get("nodes", [])
    links: list[dict] = graph_data.get("links", [])
    communities: dict[str, list[str]] = analysis.get("communities", {})
    surprises: list[dict] = analysis.get("surprises", [])

    nodes_by_id = {n["id"]: n for n in nodes}

    # --- Enrichments ---
    cache_dir = graph_path.parent
    print("    [Enrich] A enriquecer comunidades...")

    # 1. Community summaries (LLM)
    summaries = summarize_communities(
        graph_data, analysis, repo_name, cache_dir, force=force,
    )

    # 2. Cross-community links (shared files)
    cross_links = detect_cross_community_links(graph_data, analysis)

    # 3. Tags
    repo_tags = infer_repo_tags(graph_data, analysis)

    # 4. Build community→surprises mapping
    community_surprises: dict[str, list[dict]] = {}
    for s in surprises:
        src_files = s.get("source_files", [])
        for cid, member_ids in communities.items():
            member_files = {nodes_by_id.get(m, {}).get("source_file", "") for m in member_ids}
            if any(sf in member_files for sf in src_files):
                community_surprises.setdefault(cid, []).append(s)

    # --- Write community notes ---
    community_ids_written = []
    for cid, member_ids in communities.items():
        # Generate Mermaid
        mermaid = generate_community_mermaid(cid, member_ids, graph_data)

        result = _write_community_note(
            cid, member_ids, nodes_by_id, repo_dir, repo_name,
            summary=summaries.get(cid),
            cross_links=cross_links.get(cid),
            mermaid=mermaid,
            tags=repo_tags.get(cid),
            surprises_for_community=community_surprises.get(cid),
        )
        if result is not None:
            community_ids_written.append(cid)

    # --- Write god node notes ---
    gods = analysis.get("gods", [])
    god_node_names = _write_god_node_notes(
        gods, graph_data, analysis, repo_dir, repo_name,
    )
    print(f"    [Enrich] {len(god_node_names)} god node notes criadas.")

    # --- Write Overview ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_overview_note(
        repo_name=repo_name,
        repo_source_path=repo_source_path,
        graph_path=graph_path,
        analysis=analysis,
        graph_data=graph_data,
        community_ids_written=community_ids_written,
        repo_dir=repo_dir,
        god_node_names=god_node_names,
    )

    n_communities = len(community_ids_written)

    # Save incremental export state
    _save_export_state(repo_dir, new_state)

    print(f"    [Obsidian] {repo_name} → {vault_dir}/{repo_name}/ "
          f"({len(nodes)} nodes, {len(links)} edges, {n_communities} comunidades, "
          f"{len(god_node_names)} god nodes)")

    return {
        "name": repo_name,
        "source_path": str(repo_source_path),
        "nodes": len(nodes),
        "edges": len(links),
        "communities": n_communities,
        "god_nodes": len(god_node_names),
        "generated_at": now,
    }


def export_all(*, force: bool = False) -> None:
    """Exporta todos os repos configurados para o vault Obsidian.

    Se force=True, regenera sumários LLM (ignora cache).
    Chamado automaticamente por sync_graphify() após build_graphs().
    """
    vault_dir = settings.graphify.graph_vault_dir
    print(f"==> [Obsidian] A exportar grafos para vault: {vault_dir}")

    if force:
        files_removed, dirs_removed = purge_generated_vault(vault_dir)
        print(
            "    [Obsidian] force=true: vault derivado limpo "
            f"({dirs_removed} dirs/{files_removed} files removidos)."
        )
    else:
        init_vault(vault_dir)

    repo_summaries = []

    from pipeline.graph.builder import _configured_graph_roots

    for repo_path in _configured_graph_roots():
        repo_path = Path(repo_path).expanduser().resolve()
        repo_name = repo_path.name

        # Localizar graph.json e .graphify_analysis.json
        graphify_out = settings.graphify.output_dir / repo_name / "graphify-out"
        graph_path = graphify_out / "graph.json"
        analysis_path = graphify_out / ".graphify_analysis.json"

        if not graph_path.exists():
            print(f"    [Obsidian] Grafo não encontrado para '{repo_name}' — skipping.")
            continue
        if not analysis_path.exists():
            print(f"    [Obsidian] Análise não encontrada para '{repo_name}' — skipping.")
            continue

        try:
            summary = export_repo(
                repo_name=repo_name,
                repo_source_path=repo_path,
                graph_path=graph_path,
                analysis_path=analysis_path,
                vault_dir=vault_dir,
                force=force,
            )
            repo_summaries.append(summary)
        except Exception as e:
            print(f"    [Obsidian] ERRO ao exportar '{repo_name}': {e}")

    if repo_summaries:
        index_path = _write_index_note(vault_dir, repo_summaries)
        print(f"==> [Obsidian] Índice actualizado: {index_path}")
        print(f"==> [Obsidian] {len(repo_summaries)} repo(s) exportado(s) para '{vault_dir}'")
    else:
        print("==> [Obsidian] Nenhum repo com grafo disponível para exportar.")
