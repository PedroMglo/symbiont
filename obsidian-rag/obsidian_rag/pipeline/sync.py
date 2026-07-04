"""Pipeline de sincronização: chunk → embed → store.

Suporta três fontes:
  1. Notas Obsidian (Markdown) — coleção "obsidian_vault"
  2. Roots em [repos].paths (Git ou pastas normais) — coleção "code_repos"
  3. Graphify — knowledge graph estrutural dos roots configurados (opcional)

Operational use goes through the RAG HTTP API:
  POST /admin/reprocess {"target": "local"}
  POST /admin/reprocess {"target": "graph"}
  POST /admin/reprocess {"target": "all"}
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from obsidian_rag.config import settings
from obsidian_rag.embeddings import get_embedder
from obsidian_rag.pipeline.ingest import IngestPipeline, IngestSource
from obsidian_rag.pipeline.manifest import IngestManifest
from obsidian_rag.pipeline.vault_sync import sync_vault
from obsidian_rag.store import get_store


def _compute_config_version() -> str:
    """Fingerprint of chunking-relevant settings; changes force full reindex."""
    c = settings.chunking
    raw = (
        "source-v2:obsidian-meta-v1:"
        f"{c.max_chars}:{c.overlap_chars}:{c.min_chars}:{c.strip_frontmatter}:{c.contextual_prefix}"
    )
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:8]

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CAG eager pack generation
# ---------------------------------------------------------------------------

def _generate_cag_packs() -> None:
    """Generate eager CAG packs after sync (if enabled)."""
    if not settings.cag.enabled or not settings.cag.generate_on_sync:
        return
    try:
        from obsidian_rag.cag import get_pack_store
        from obsidian_rag.cag.packs import generate_eager_packs

        print("\n==> [CAG] A gerar packs de contexto...")
        store = get_pack_store()
        count = generate_eager_packs(store)
        print(f"    {count} packs gerados com sucesso.")
    except Exception as e:
        print(f"    ⚠ CAG pack generation failed (non-fatal): {e}")


def generate_cag_packs() -> None:
    """Public API-facing wrapper for CAG eager pack generation."""
    _generate_cag_packs()


_REPO_DISCOVERY_IGNORE = {
    ".cache", ".local", ".venv", "venv", "env", "node_modules",
    "dist", "build", "__pycache__", ".Trash", "Trash",
}


def _is_git_repo(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def _discover_git_repos(paths: list[Path]) -> list[Path]:
    """Resolve configured repo paths into concrete Git repo roots."""
    seen: set[Path] = set()
    repos: list[Path] = []

    for root in paths:
        root = root.expanduser().resolve()
        if not root.exists():
            print(f"    [AVISO] Repo/root não encontrado: {root} — skipping.")
            continue
        if _is_git_repo(root):
            if root not in seen:
                repos.append(root)
                seen.add(root)
            continue

        for current, dirs, _files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [d for d in dirs if d not in _REPO_DISCOVERY_IGNORE and not d.endswith(".egg-info")]
            if _is_git_repo(current_path):
                if current_path not in seen:
                    repos.append(current_path)
                    seen.add(current_path)
                dirs[:] = []

    return sorted(repos)


def _split_configured_sources(paths: tuple[Path, ...]) -> tuple[list[Path], list[Path]]:
    """Split [repos].paths into Git repositories and non-Git document roots."""
    seen_git: set[Path] = set()
    seen_docs: set[Path] = set()
    git_roots: list[Path] = []
    document_roots: list[Path] = []

    for raw_root in paths:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            print(f"    [AVISO] Repo/root não encontrado: {root} — skipping.")
            continue
        if root.is_file():
            parent = root.parent
            if parent not in seen_docs:
                document_roots.append(parent)
                seen_docs.add(parent)
            continue

        repos = _discover_git_repos([root])
        if repos:
            for repo in repos:
                if repo not in seen_git:
                    git_roots.append(repo)
                    seen_git.add(repo)
            continue

        if root not in seen_docs:
            document_roots.append(root)
            seen_docs.add(root)

    return sorted(git_roots), sorted(document_roots)


# ---------------------------------------------------------------------------
# Sync functions
# ---------------------------------------------------------------------------

def _gpu_first_embed_fn():
    """Return the exceptional sync-only embedding function.

    This stays out of the process-wide embedder defaults so query/API calls keep
    their normal runtime policy while a full forced rebuild is running.
    """
    embedder = get_embedder()
    embed_fn = getattr(embedder, "embed_texts_cached_gpu_first", None)
    if callable(embed_fn):
        return embed_fn
    embed_fn = getattr(embedder, "embed_texts_gpu_first", None)
    if callable(embed_fn):
        return embed_fn
    return getattr(embedder, "embed_texts_cached", None) or embedder.embed_texts


def sync_notes(*, vault_filter: str | None = None, force: bool = False, embed_fn=None) -> None:
    """Sincroniza notas Obsidian → vector store (coleção obsidian_vault).

    Args:
        vault_filter: If set, sync only the vault whose directory name
                      matches (case-insensitive).
        force: If True, bypass manifest hash checks and reindex all files.
        embed_fn: Optional embedding callable for exceptional ingest modes.
    """
    from obsidian_rag.pipeline.governor import GovernorAction, ResourceGovernor

    # --- Resource protection via governor ---
    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()

    action = gov.check()
    if action is GovernorAction.ABORT:
        snap = gov.snapshot()
        reasons = []
        if snap:
            if snap.disk_free_gb < 1.0:
                reasons.append(f"Disco: {snap.disk_free_gb:.1f} GB livres")
            else:
                reasons.append(f"RAM {snap.ram_percent:.0f}%")
                if snap.swap_percent > 0:
                    reasons.append(f"Swap {snap.swap_percent:.0f}%")
        reason = ", ".join(reasons) if reasons else "recursos críticos"
        print(f"✗ [Notas] {reason} — sync abortado.")
        gov.stop()
        return
    if action is GovernorAction.PAUSE:
        snap = gov.snapshot()
        detail = ""
        if snap:
            detail = f" (RAM {snap.ram_percent:.0f}%, Swap {snap.swap_percent:.0f}%)"
        print(f"⚠ [Notas] Sistema sob pressão{detail} — a aguardar recursos...")
        action = gov.wait_until_safe(timeout=15)
        if action is GovernorAction.ABORT:
            print("✗ [Notas] Recursos críticos após espera — sync abortado.")
            gov.stop()
            return
        if action is GovernorAction.PAUSE:
            print("    Pressão mantém-se — a continuar com precaução.")

    get_embedder().clear_cache()

    # Resolve vault directories (multi-vault support)
    vault_dirs = settings.paths.vault_dirs
    if vault_filter:
        vault_dirs = tuple(
            vd for vd in vault_dirs
            if vd.name.lower() == vault_filter.lower()
        )
        if not vault_dirs:
            print(f"✗ Vault '{vault_filter}' não encontrado em vault_dirs.")
            gov.stop()
            return

    # Build IngestSource per vault
    sources: list[IngestSource] = []
    for vault_dir in vault_dirs:
        effective_dir = sync_vault(
            vault_dir=vault_dir,
            cfg=settings.sync,
        )
        vault_name = vault_dir.name
        sources.append(
            IngestSource(source_type="vault", path=effective_dir, name=vault_name),
        )

    if not sources:
        print("⚠ [Notas] Nenhum vault configurado.")
        gov.stop()
        return

    vault_names = ", ".join(s.name for s in sources)
    print(f"==> [Notas] A processar {len(sources)} vault(s): {vault_names}")

    manifest_path = settings.paths.data_dir / "manifest.db"
    manifest = IngestManifest(manifest_path, config_version=_compute_config_version())

    store = get_store()

    pipeline = IngestPipeline(
        manifest=manifest,
        perf=settings.performance,
        store=store,
        collection_name="obsidian_vault",
        embed_fn=embed_fn,
        governor=gov,
        pipeline_config=settings.pipeline,
        max_run_seconds=float(settings.performance.pipeline_timeout),
        force=force,
        mtime_shortcircuit=settings.graphify.mtime_shortcircuit,
    )

    try:
        result = pipeline.run(sources)
    finally:
        manifest.close()
        gov.stop()

    # --- Report ---
    print(f"\n==> [Notas] Pipeline concluído em {result.elapsed_seconds:.1f}s")
    print(f"    Ficheiros: {result.files_scanned} scanned, {result.files_parsed} parsed, {result.files_skipped} skipped")
    print(f"    Chunks: {result.chunks_produced} produced, {result.chunks_embedded} embedded, {result.chunks_stored} stored")
    print(f"    Stages (ms): scan={result.scan_ms:.0f} parse={result.parse_ms:.0f} embed={result.embed_ms:.0f} write={result.write_ms:.0f}")
    if result.stale_deleted:
        print(f"    Removidos: {result.stale_deleted} chunks obsoletos")
    if result.errors:
        print(f"    Erros: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"      - {err}")
        if len(result.errors) > 5:
            print(f"      ... e mais {len(result.errors) - 5}")

    final_count = store.count(collection="obsidian_vault")
    print(f"==> [Notas] Store: {final_count} chunks na coleção 'obsidian_vault'")


def sync_repos(*, force: bool = False, embed_fn=None) -> None:
    """Sincroniza [repos].paths → vector store (coleção code_repos).

    Each configured root is classified at runtime:
    - Git roots or roots containing Git repos use code-aware chunking.
    - Non-Git roots use heterogeneous document chunking (text/config/code,
      PDF/Office/table files through extrator, and audio through the
      transcription service before embeddings are created).

    The bounded ingest pipeline keeps the work incremental and resource-aware.
    """
    if not settings.repos.paths:
        print("==> [Repos] Sem roots configuradas em config/rag/user.toml [repos] paths. Skipping.")
        return

    git_paths, document_paths = _split_configured_sources(settings.repos.paths)

    if not git_paths and not document_paths:
        print("==> [Repos] Nenhuma root válida encontrada.")
        return

    # --- Resource protection via governor ---
    from obsidian_rag.pipeline.governor import GovernorAction, ResourceGovernor

    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()

    action = gov.check()
    if action is GovernorAction.ABORT:
        snap = gov.snapshot()
        reasons = []
        if snap:
            if snap.disk_free_gb < 1.0:
                reasons.append(f"Disco: {snap.disk_free_gb:.1f} GB livres")
            else:
                reasons.append(f"RAM {snap.ram_percent:.0f}%")
                if snap.swap_percent > 0:
                    reasons.append(f"Swap {snap.swap_percent:.0f}%")
        reason = ", ".join(reasons) if reasons else "recursos críticos"
        print(f"✗ [Repos] {reason} — sync abortado.")
        gov.stop()
        return
    if action is GovernorAction.PAUSE:
        snap = gov.snapshot()
        detail = ""
        if snap:
            detail = f" (RAM {snap.ram_percent:.0f}%, Swap {snap.swap_percent:.0f}%)"
        print(f"⚠ [Repos] Sistema sob pressão{detail} — a aguardar recursos...")
        action = gov.wait_until_safe(timeout=15)
        if action is GovernorAction.ABORT:
            print("✗ [Repos] Recursos críticos após espera — sync abortado.")
            gov.stop()
            return
        if action is GovernorAction.PAUSE:
            print("    Pressão mantém-se — a continuar com precaução.")

    # --- Bounded ingest pipeline ---
    print(
        "==> [Repos] A processar "
        f"{len(git_paths)} repo(s) Git e {len(document_paths)} pasta(s) não-Git via bounded pipeline..."
    )

    manifest_path = settings.paths.data_dir / "manifest.db"
    manifest = IngestManifest(manifest_path, config_version=_compute_config_version())

    store = get_store()

    sources = [IngestSource(source_type="code", path=p, name=p.name) for p in git_paths]
    sources.extend(IngestSource(source_type="document", path=p, name=p.name) for p in document_paths)

    pipeline = IngestPipeline(
        manifest=manifest,
        perf=settings.performance,
        store=store,
        collection_name=settings.repos.collection_name,
        embed_fn=embed_fn,
        governor=gov,    # pass the already-running governor
        pipeline_config=settings.pipeline,
        max_run_seconds=float(settings.performance.pipeline_timeout),
        force=force,
        mtime_shortcircuit=settings.graphify.mtime_shortcircuit,
    )

    try:
        result = pipeline.run(sources)
    finally:
        manifest.close()
        gov.stop()

    # --- Report ---
    print(f"\n==> [Repos] Pipeline concluído em {result.elapsed_seconds:.1f}s")
    print(f"    Ficheiros: {result.files_scanned} scanned, {result.files_parsed} parsed, {result.files_skipped} skipped")
    print(f"    Chunks: {result.chunks_produced} produced, {result.chunks_embedded} embedded, {result.chunks_stored} stored")
    print(f"    Stages (ms): scan={result.scan_ms:.0f} parse={result.parse_ms:.0f} embed={result.embed_ms:.0f} write={result.write_ms:.0f}")
    if result.stale_deleted:
        print(f"    Removidos: {result.stale_deleted} chunks obsoletos")
    if result.errors:
        print(f"    Erros: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"      - {err}")
        if len(result.errors) > 5:
            print(f"      ... e mais {len(result.errors) - 5}")

    final_count = store.count(collection=settings.repos.collection_name)
    print(f"==> [Repos] Store: {final_count} chunks na coleção '{settings.repos.collection_name}'")


def _wait_for_resources(label: str) -> bool:
    """Aguarda recursos disponíveis entre fases. Retorna False se recursos críticos."""
    from obsidian_rag.pipeline.governor import GovernorAction, ResourceGovernor

    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()
    try:
        action = gov.check()
        if action is GovernorAction.ABORT:
            snap = gov.snapshot()
            reasons = []
            if snap:
                if snap.disk_free_gb < 1.0:
                    reasons.append(f"Disco: {snap.disk_free_gb:.1f} GB livres")
                else:
                    reasons.append(f"RAM {snap.ram_percent:.0f}%")
                    if snap.swap_percent > 0:
                        reasons.append(f"Swap {snap.swap_percent:.0f}%")
            reason = ", ".join(reasons) if reasons else "recursos críticos"
            print(f"✗ [{label}] {reason} — fase seguinte abortada.")
            return False
        if action is GovernorAction.PAUSE:
            snap = gov.snapshot()
            detail = ""
            if snap:
                detail = f" (RAM {snap.ram_percent:.0f}%, Swap {snap.swap_percent:.0f}%)"
            print(f"⚠ [{label}] Sistema sob pressão{detail} — a aguardar...")
            action = gov.wait_until_safe(timeout=15)
            if action is GovernorAction.ABORT:
                print(f"✗ [{label}] Recursos críticos — fase seguinte abortada.")
                return False
            if action is GovernorAction.PAUSE:
                print(f"    [{label}] Pressão mantém-se — a continuar com precaução.")
    finally:
        gov.stop()
    return True


def sync_local(*, vault_filter: str | None = None, force: bool = False, gpu_first_embeddings: bool = False) -> None:
    """Embeddings: notas Obsidian + repos Git (só deltas — sync incremental)."""
    import gc

    embed_fn = _gpu_first_embed_fn() if gpu_first_embeddings else None
    if gpu_first_embeddings:
        print("==> [Sync] all --force: embeddings em modo GPU-first (fallback CPU activo)")

    sync_notes(vault_filter=vault_filter, force=force, embed_fn=embed_fn)
    gc.collect()  # free chunk lists, ASTs, source code from notes phase
    print()
    if not _wait_for_resources("Transição notas→repos"):
        return
    sync_repos(force=force, embed_fn=embed_fn)
    gc.collect()  # free pipeline objects

    # CAG: generate eager packs after sync
    _generate_cag_packs()

    # Stale graph alert: warn if graphify ran but auto_update is disabled
    if settings.repos.paths and not settings.graphify.auto_update:
        print()
        print("⚠  [Graph] auto_update=false — o graph pode estar desactualizado.")
        print("   Chama POST /admin/reprocess {\"target\":\"graph\"} para actualizar o graph estrutural.")

    # Webhook: notify external consumers that sync completed
    from obsidian_rag.pipeline.webhook import notify_sync_complete
    notify_sync_complete({"event_source": "sync_local"})


def sync_all(*, vault_filter: str | None = None, force: bool = False) -> None:
    """Run the full admin sync.

    The forced all-target rebuild is the only path that opts into GPU-first
    embeddings. All other admin targets and query-time API paths stay unchanged.
    """
    sync_local(
        vault_filter=vault_filter,
        force=force,
        gpu_first_embeddings=force,
    )
    sync_graphify(force=force)
    generate_cag_packs()


def sync_graphify(*, force: bool = False) -> None:
    """Grafos: constrói/actualiza grafos para todos os repos.

    Se *force* é True, apaga o manifest.json de cada repo antes de extrair,
    forçando um rebuild completo (AST + LLM) mesmo que o grafo já exista.
    Após build, exporta para o vault Obsidian configurado em graph_vault_dir.
    """
    if not settings.graphify.enabled:
        print("==> [Graphify] Desabilitado em config/rag/internal.toml [graphify] enabled = false. Skipping.")
        return
    try:
        from obsidian_rag.pipeline.graph.builder import build_graphs
        build_graphs(force=force)
    except ImportError:
        print("==> [Graphify] graphifyy não está instalado. Instala com: pip install graphifyy")
        return
    except FileNotFoundError:
        print("==> [Graphify] Comando 'graphify' não encontrado. Instala com: pip install graphifyy")
        return

    # Exportar grafos para o vault Obsidian
    print()
    try:
        from obsidian_rag.pipeline.graph.obsidian_export import export_all
        export_all(force=force)
    except Exception as e:
        print(f"==> [Obsidian] Erro na exportação para o vault (não fatal): {e}")
