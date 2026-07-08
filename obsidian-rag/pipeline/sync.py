"""Pipeline de sincronização: chunk → embed → store.

Suporta três fontes:
  1. Notas Obsidian (Markdown) — coleção "obsidian_vault"
  2. Roots em [repos].paths (Git ou pastas normais) — coleção "code_repos"
  3. Graphify — knowledge graph estrutural dos roots configurados (opcional)

Operational use goes through the RAG HTTP API:
  POST /admin/reprocess {"target": "local"}
  POST /admin/reprocess {"target": "sources", "sources": [...]}
  POST /admin/reprocess {"target": "graph"}
  POST /admin/reprocess {"target": "all"}
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from embeddings import get_embedder
from pipeline.ingest import IngestPipeline, IngestResult, IngestSource
from pipeline.manifest import IngestManifest
from pipeline.vault_sync import sync_vault
from rag_config import settings
from store import get_store


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
        from cag import get_pack_store
        from cag.packs import generate_eager_packs

        print("\n==> [CAG] A gerar packs de contexto...")
        store = get_pack_store()
        count = generate_eager_packs(store)
        print(f"    {count} packs gerados com sucesso.")
    except Exception as e:
        print(f"    ⚠ CAG pack generation failed (non-fatal): {e}")


def generate_cag_packs() -> None:
    """Public API-facing wrapper for CAG eager pack generation."""
    _generate_cag_packs()


def has_configured_sources() -> bool:
    """Return True when RAG has at least one configured vault/repo source."""
    return bool(settings.paths.vault_dirs or settings.repos.paths)


def _purge_path_children(path: Path, *, keep_names: set[str] | None = None) -> tuple[int, int]:
    keep = keep_names or set()
    files_removed = 0
    dirs_removed = 0
    if not path.exists():
        return files_removed, dirs_removed
    for child in path.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
            dirs_removed += 1
        else:
            child.unlink(missing_ok=True)
            files_removed += 1
    return files_removed, dirs_removed


def _purge_embedding_cache() -> int:
    removed = 0
    cache_path = Path(settings.paths.data_dir) / "embedding_cache.db"
    for path in (cache_path, Path(f"{cache_path}-wal"), Path(f"{cache_path}-shm")):
        if path.exists():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def purge_local_rag_artifacts_for_empty_sources() -> None:
    """Remove derived RAG/Graphify/CAG artifacts when no sources are configured."""
    graph_files, graph_dirs = _purge_path_children(Path(settings.graphify.output_dir))
    vault_files, vault_dirs = _purge_path_children(
        Path(settings.graphify.graph_vault_dir),
        keep_names={".obsidian"},
    )
    embedding_cache_files = _purge_embedding_cache()
    try:
        from cag import get_pack_store

        get_pack_store().invalidate_all()
        cag_status = "CAG packs invalidados"
    except Exception as exc:
        cag_status = f"CAG purge falhou: {exc}"

    print(
        "==> [Purge] Sem fontes configuradas: artefactos RAG locais removidos "
        f"(graphify: {graph_dirs} dirs/{graph_files} files, "
        f"vault: {vault_dirs} dirs/{vault_files} files, "
        f"embedding_cache: {embedding_cache_files} files; {cag_status})."
    )


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


def _reset_collection_and_manifest(
    *,
    label: str,
    collection_name: str,
    source_types: tuple[str, ...],
) -> None:
    """Clear persisted index state for a forced replacement with no sources."""
    manifest_path = settings.paths.data_dir / "manifest.db"
    manifest = IngestManifest(manifest_path, config_version=_compute_config_version())
    try:
        removed_manifest = manifest.delete_source_types(source_types)
        removed_runs = manifest.delete_run_history()
    finally:
        manifest.close()

    store = get_store()
    reset_collection = getattr(store, "reset_collection", None)
    if callable(reset_collection):
        deleted_vectors = reset_collection(collection=collection_name)
    else:
        existing_ids = store.get_existing_ids(collection=collection_name)
        deleted_vectors = store.delete_ids(list(existing_ids), collection=collection_name)
    _reset_bm25_state(collection_name)
    print(
        f"==> [{label}] force=true: estado anterior eliminado "
        f"({deleted_vectors} vectors, {removed_manifest['files']} manifest files, "
        f"{removed_manifest['chunks']} manifest chunks, {removed_runs} manifest runs)."
    )


def _reset_bm25_state(collection_name: str) -> None:
    model_path = settings.paths.data_dir / "bm25" / f"{collection_name}.json"
    try:
        model_path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"    ⚠ BM25 reset: não foi possível remover {model_path}: {exc}")
    try:
        from retrieval import rag as retrieval_rag

        retrieval_rag._bm25_cache.pop(collection_name, None)  # noqa: SLF001
    except Exception:
        pass


def _cancel_requested(cancel_event: threading.Event | None) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        print("✗ [Sync] Cancelamento pedido — fase atual abortada.")
        return True
    return False


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit_progress(progress_callback: ProgressCallback | None, event: str, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback({"event": event, **payload})
    except Exception:
        pass


def _guard_governor(
    gov: Any,
    *,
    label: str,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
    child_id: str | None = None,
    phase: str | None = None,
    attempt: int = 1,
) -> None:
    from pipeline.governor import GovernorAction, wait_for_resource_budget

    action = wait_for_resource_budget(
        gov,
        perf=settings.performance,
        label=label,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        child_id=child_id,
        phase=phase,
        attempt=attempt,
    )
    if action in (GovernorAction.THROTTLE, GovernorAction.REDUCE):
        print(f"    [{label}] Governor: {action.name} — limites reduzidos para esta fase.")


def _release_phase_memory(label: str, *, clear_embedder_cache: bool = False) -> dict[str, Any]:
    from pipeline.governor import release_process_memory

    clear_callback = get_embedder().clear_cache if clear_embedder_cache else None
    return release_process_memory(
        perf=settings.performance,
        label=label,
        clear_cache_callback=clear_callback,
    )


def _resource_error_payload(exc: BaseException, *, attempt: int) -> dict[str, Any]:
    payload_fn = getattr(exc, "payload", None)
    if callable(payload_fn):
        payload = dict(payload_fn())
    else:
        payload = {"resource_state": getattr(exc, "status", "failed_resource_pressure"), "reason": str(exc)}
    payload.setdefault("attempt", attempt)
    payload.setdefault("error", str(exc)[:1000])
    return payload


def _ingest_result_payload(result: IngestResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    payload = {
        "files_scanned": result.files_scanned,
        "files_parsed": result.files_parsed,
        "files_skipped": result.files_skipped,
        "chunks_produced": result.chunks_produced,
        "chunks_embedded": result.chunks_embedded,
        "chunks_stored": result.chunks_stored,
        "stale_deleted": result.stale_deleted,
        "errors": list(result.errors),
        "elapsed_seconds": result.elapsed_seconds,
        "stages_ms": {
            "scan": result.scan_ms,
            "parse": result.parse_ms,
            "embed": result.embed_ms,
            "write": result.write_ms,
        },
    }
    if result.resource_pressure:
        payload["resource_pressure"] = dict(result.resource_pressure)
    return payload


def _to_ingest_source(source: Any) -> IngestSource:
    return IngestSource(
        source_type=source.source_type,
        path=source.path,
        name=source.name,
        exclude_patterns=source.exclude_patterns,
    )


def _source_payload(source: IngestSource) -> dict[str, Any]:
    return {
        "name": source.name,
        "path": str(source.path),
        "source_type": source.source_type,
        "exclude_patterns": list(source.exclude_patterns),
    }


def _source_child_id(source: IngestSource, index: int) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in source.name).strip("-")
    safe_name = safe_name or "source"
    return f"repo-source-{index + 1:03d}-{source.source_type}-{safe_name}"


def _clear_selected_repo_sources(
    *,
    manifest: IngestManifest,
    store,
    sources: Iterable[IngestSource],
) -> int:
    """Delete only the selected source vectors before a partial forced ingest."""

    from metadata import stable_source_id

    removed = 0
    for source in sources:
        source_id = stable_source_id(source.name, source.path)
        chunk_ids = manifest.delete_stale_files(source.name, set(), source_id=source_id)
        if chunk_ids:
            removed += store.delete_ids(chunk_ids, collection=settings.repos.collection_name)
    return removed


def sync_notes(
    *,
    vault_filter: str | None = None,
    force: bool = False,
    embed_fn=None,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Sincroniza notas Obsidian → vector store (coleção obsidian_vault).

    Args:
        vault_filter: If set, sync only the vault whose directory name
                      matches (case-insensitive).
        force: If True, bypass manifest hash checks and reindex all files.
        embed_fn: Optional embedding callable for exceptional ingest modes.
    """
    from pipeline.governor import ResourceGovernor

    if _cancel_requested(cancel_event):
        return

    # --- Resource protection via governor ---
    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()
    try:
        _guard_governor(
            gov,
            label="Notas",
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            child_id="local-notes",
            phase="notes",
        )
    except Exception:
        gov.stop()
        raise

    get_embedder().clear_cache()

    from pipeline.adhoc_sources import registered_sources

    # Resolve vault directories (multi-vault support)
    vault_dirs = settings.paths.vault_dirs
    if vault_filter:
        vault_dirs = tuple(
            vd for vd in vault_dirs
            if vd.name.lower() == vault_filter.lower()
        )
    runtime_vaults = tuple(registered_sources(source_types={"vault"}))
    if vault_filter:
        runtime_vaults = tuple(
            source for source in runtime_vaults
            if source.name.lower() == vault_filter.lower() or source.path.name.lower() == vault_filter.lower()
        )
        if not vault_dirs and not runtime_vaults:
            print(f"✗ Vault '{vault_filter}' não encontrado nas fontes disponíveis.")
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
    for source in runtime_vaults:
        effective_dir = sync_vault(
            vault_dir=source.path,
            cfg=settings.sync,
        )
        sources.append(
            IngestSource(
                source_type="vault",
                path=effective_dir,
                name=source.name,
                exclude_patterns=source.exclude_patterns,
            ),
        )

    if not sources:
        print("⚠ [Notas] Nenhum vault configurado.")
        if force and not vault_filter:
            _reset_collection_and_manifest(
                label="Notas",
                collection_name="obsidian_vault",
                source_types=("vault",),
            )
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
        reset_source_types=("vault",),
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        progress_child_id="local-notes",
        progress_phase="notes",
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


def _run_repo_document_pipeline(
    sources: list[IngestSource],
    *,
    force: bool = False,
    embed_fn=None,
    cancel_event: threading.Event | None = None,
    cleanup_stale_global: bool = True,
    label: str = "Repos",
    progress_callback: ProgressCallback | None = None,
    child_id: str | None = None,
    phase: str = "repo_document",
    attempt: int = 1,
) -> IngestResult | None:
    if _cancel_requested(cancel_event):
        return None
    if not sources:
        print(f"==> [{label}] Sem roots de repos/documentos disponíveis. Skipping.")
        return None

    from pipeline.governor import ResourceGovernor

    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()
    try:
        _guard_governor(
            gov,
            label=label,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            child_id=child_id,
            phase=phase,
            attempt=attempt,
        )
    except Exception:
        gov.stop()
        raise

    git_count = sum(1 for source in sources if source.source_type == "code")
    document_count = sum(1 for source in sources if source.source_type == "document")
    print(
        f"==> [{label}] A processar "
        f"{git_count} repo(s) Git e {document_count} pasta(s) não-Git via bounded pipeline..."
    )

    manifest_path = settings.paths.data_dir / "manifest.db"
    manifest = IngestManifest(manifest_path, config_version=_compute_config_version())
    store = get_store()

    pipeline_force = force
    if force and not cleanup_stale_global:
        removed = _clear_selected_repo_sources(manifest=manifest, store=store, sources=sources)
        pipeline_force = False
        if removed:
            print(f"    [{label}] force=true: {removed} vector(s) removidos apenas das fontes pedidas.")

    pipeline = IngestPipeline(
        manifest=manifest,
        perf=settings.performance,
        store=store,
        collection_name=settings.repos.collection_name,
        embed_fn=embed_fn,
        governor=gov,
        pipeline_config=settings.pipeline,
        max_run_seconds=float(settings.performance.pipeline_timeout),
        force=pipeline_force,
        mtime_shortcircuit=settings.graphify.mtime_shortcircuit,
        reset_source_types=("code", "document"),
        cleanup_stale_global=cleanup_stale_global,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        progress_child_id=child_id,
        progress_phase=phase,
        progress_attempt=attempt,
    )

    try:
        result = pipeline.run(sources)
    finally:
        manifest.close()
        gov.stop()

    print(f"\n==> [{label}] Pipeline concluído em {result.elapsed_seconds:.1f}s")
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
    print(f"==> [{label}] Store: {final_count} chunks na coleção '{settings.repos.collection_name}'")
    return result


def collect_repo_document_sources(*, include_runtime_sources: bool = False) -> list[IngestSource]:
    """Return configured repo-document sources as concrete ingest roots."""
    configured_source_paths = tuple(settings.repos.paths)
    git_paths, document_paths = _split_configured_sources(configured_source_paths)
    sources = [IngestSource(source_type="code", path=p, name=p.name) for p in git_paths]
    sources.extend(IngestSource(source_type="document", path=p, name=p.name) for p in document_paths)
    if include_runtime_sources:
        from pipeline.adhoc_sources import registered_sources

        runtime_sources = registered_sources(source_types={"code", "document"})
        sources.extend(_to_ingest_source(source) for source in runtime_sources)
    return sources


def reset_repo_document_state(*, label: str = "Repos") -> None:
    """Reset the shared repo/document collection once before a forced parent run."""
    _reset_collection_and_manifest(
        label=label,
        collection_name=settings.repos.collection_name,
        source_types=("code", "document"),
    )


def cleanup_repo_document_stale(
    sources: list[IngestSource],
    *,
    label: str = "Repos",
) -> int:
    """Delete vectors absent from the union of all source manifests after child runs."""
    if not sources:
        return 0
    from metadata import stable_source_id

    manifest_path = settings.paths.data_dir / "manifest.db"
    manifest = IngestManifest(manifest_path, config_version=_compute_config_version())
    store = get_store()
    try:
        all_manifest_ids: set[str] = set()
        for source in sources:
            source_id = stable_source_id(source.name, source.path)
            all_manifest_ids |= manifest.get_chunk_ids_for_repo(source.name, source_id=source_id)
        if not all_manifest_ids:
            return 0
        existing_in_store = store.get_existing_ids(collection=settings.repos.collection_name)
        stale_ids = existing_in_store - all_manifest_ids
        if not stale_ids:
            return 0
        deleted = store.delete_ids(list(stale_ids), collection=settings.repos.collection_name)
        if deleted:
            print(f"==> [{label}] Cleanup global: {deleted} chunks obsoletos removidos.")
            _reset_bm25_state(settings.repos.collection_name)
        return deleted
    finally:
        manifest.close()


def sync_repo_document_sources(
    sources: list[IngestSource],
    *,
    force: bool = False,
    embed_fn=None,
    cancel_event: threading.Event | None = None,
    cleanup_stale_global: bool = True,
    label: str = "Repos",
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run repo/document sources as coordinated child jobs under one parent."""
    if _cancel_requested(cancel_event):
        return {"sources": [], "stale_deleted": 0}
    if not sources:
        print(f"==> [{label}] Sem roots de repos/documentos disponíveis. Skipping.")
        return {"sources": [], "stale_deleted": 0}

    child_force = force
    if force and cleanup_stale_global:
        reset_repo_document_state(label=label)
        child_force = False
    elif force:
        manifest_path = settings.paths.data_dir / "manifest.db"
        manifest = IngestManifest(manifest_path, config_version=_compute_config_version())
        store = get_store()
        try:
            removed = _clear_selected_repo_sources(manifest=manifest, store=store, sources=sources)
        finally:
            manifest.close()
        if removed:
            print(f"    [{label}] force=true: {removed} vector(s) removidos apenas das fontes pedidas.")
        child_force = False

    perf = getattr(settings, "performance", None)
    max_workers = max(1, int(getattr(perf, "source_scan_parallel_jobs", getattr(perf, "max_parallel_jobs", 1))))
    max_workers = min(max_workers, len(sources))
    print(f"==> [{label}] Parent job: {len(sources)} fonte(s), {max_workers} child worker(s).")

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    results_lock = threading.Lock()

    def _run_child(index: int, source: IngestSource) -> dict[str, Any]:
        from pipeline.governor import ResourcePressureError

        child_id = _source_child_id(source, index)
        source_info = _source_payload(source)
        max_attempts = max(1, int(getattr(getattr(settings, "performance", None), "resource_retry_max_attempts", 3)))
        last_payload: dict[str, Any] = {}

        for attempt in range(1, max_attempts + 1):
            started_at = time.time()
            _emit_progress(
                progress_callback,
                "child_started",
                child_id=child_id,
                phase="repo_document",
                source=source_info,
                started_at=started_at,
                attempt=attempt,
                resource_state="running",
            )
            try:
                result = _run_repo_document_pipeline(
                    [source],
                    force=child_force,
                    embed_fn=embed_fn,
                    cancel_event=cancel_event,
                    cleanup_stale_global=False,
                    label=f"{label}:{source.name}",
                    progress_callback=progress_callback,
                    child_id=child_id,
                    phase="repo_document",
                    attempt=attempt,
                )
                resource_error = _resource_pressure_error_from_result(result, attempt=attempt)
                if resource_error is not None:
                    raise resource_error
                status = "completed" if not _cancel_requested(cancel_event) else "cancelled"
                payload = {
                    "child_id": child_id,
                    "status": status,
                    "resource_state": status,
                    "source": source_info,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "attempt": attempt,
                    "result": _ingest_result_payload(result),
                }
                _emit_progress(
                    progress_callback,
                    "child_completed" if status == "completed" else "child_failed",
                    **payload,
                )
                return payload
            except ResourcePressureError as exc:
                payload = {
                    "child_id": child_id,
                    "source": source_info,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    **_resource_error_payload(exc, attempt=attempt),
                }
                status = str(payload.get("resource_state") or exc.status)
                payload["status"] = status
                payload["resource_state"] = status
                last_payload = payload
                if status == "cancelled":
                    _emit_progress(progress_callback, "child_failed", **payload)
                    return payload
                if status == "deferred_resource_pressure" and attempt < max_attempts:
                    retry_after = int(payload.get("retry_after_seconds") or min(300, 30 * attempt))
                    payload["status"] = "retry_scheduled"
                    payload["resource_state"] = "retry_scheduled"
                    payload["retry_at"] = time.time() + retry_after
                    _emit_progress(progress_callback, "child_deferred", **payload)
                    if cancel_event is not None:
                        if cancel_event.wait(retry_after):
                            payload = {
                                **payload,
                                "status": "cancelled",
                                "resource_state": "cancelled",
                                "finished_at": time.time(),
                                "error": "cancel requested while retry was scheduled",
                            }
                            _emit_progress(progress_callback, "child_failed", **payload)
                            return payload
                    else:
                        time.sleep(retry_after)
                    continue
                payload["status"] = "failed_resource_pressure"
                payload["resource_state"] = "failed_resource_pressure"
                _emit_progress(progress_callback, "child_failed", **payload)
                raise
            except Exception as exc:
                payload = {
                    "child_id": child_id,
                    "status": "failed",
                    "resource_state": "failed",
                    "source": source_info,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "attempt": attempt,
                    "error": str(exc)[:1000],
                }
                _emit_progress(progress_callback, "child_failed", **payload)
                raise

        if last_payload:
            return last_payload
        try:
            raise RuntimeError("child did not produce a result")
        except Exception as exc:
            payload = {
                "child_id": child_id,
                "status": "failed",
                "resource_state": "failed",
                "source": source_info,
                "started_at": time.time(),
                "finished_at": time.time(),
                "error": str(exc)[:1000],
            }
            _emit_progress(progress_callback, "child_failed", **payload)
            raise

    if max_workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-source-child") as executor:
            futures = {
                executor.submit(_run_child, index, source): (index, source)
                for index, source in enumerate(sources)
            }
            for future in as_completed(futures):
                if _cancel_requested(cancel_event):
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    child_result = future.result()
                    with results_lock:
                        results.append(child_result)
                except Exception as exc:
                    _index, source = futures[future]
                    errors.append(f"{source.name}: {exc}")
    else:
        for index, source in enumerate(sources):
            if _cancel_requested(cancel_event):
                break
            try:
                results.append(_run_child(index, source))
            except Exception as exc:
                errors.append(f"{source.name}: {exc}")

    stale_deleted = 0
    if cleanup_stale_global and not _cancel_requested(cancel_event):
        stale_deleted = cleanup_repo_document_stale(sources, label=label)

    if errors:
        raise RuntimeError(f"{label} child job failures: {'; '.join(errors[:5])}")

    return {
        "sources": sorted(results, key=lambda item: item.get("child_id", "")),
        "stale_deleted": stale_deleted,
    }


def _resource_pressure_error_from_result(result: IngestResult, *, attempt: int) -> BaseException | None:
    rich_pressure = result.resource_pressure or {}
    pressure_errors = [
        error for error in result.errors
        if "deferred_resource_pressure" in error or "failed_resource_pressure" in error
    ]
    if not pressure_errors and not rich_pressure:
        return None
    from pipeline.governor import GovernorAction, ResourcePressureError

    first = pressure_errors[0] if pressure_errors else str(rich_pressure.get("reason") or rich_pressure.get("error") or "")
    status = str(rich_pressure.get("resource_state") or rich_pressure.get("status") or "")
    if status == "retry_scheduled":
        status = "deferred_resource_pressure"
    if status not in {"deferred_resource_pressure", "failed_resource_pressure"}:
        status = "deferred_resource_pressure" if "deferred_resource_pressure" in first else "failed_resource_pressure"
    reason = str(rich_pressure.get("reason") or rich_pressure.get("error") or first)
    retry_after = rich_pressure.get("retry_after_seconds")
    try:
        retry_after_seconds = int(retry_after) if retry_after is not None else 10
    except (TypeError, ValueError):
        retry_after_seconds = 10
    return ResourcePressureError(
        status,
        reason,
        action=GovernorAction.PAUSE if status == "deferred_resource_pressure" else GovernorAction.ABORT,
        retry_after_seconds=retry_after_seconds if status == "deferred_resource_pressure" else None,
        attempt=attempt,
    )


def sync_repos(
    *,
    force: bool = False,
    embed_fn=None,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Sincroniza [repos].paths → vector store (coleção code_repos)."""
    if _cancel_requested(cancel_event):
        return

    sources = collect_repo_document_sources()
    if not sources:
        print("==> [Repos] Sem roots de repos/documentos disponíveis. Skipping.")
        if force:
            _reset_collection_and_manifest(
                label="Repos",
                collection_name=settings.repos.collection_name,
                source_types=("code", "document"),
            )
        return

    sync_repo_document_sources(
        sources,
        force=force,
        embed_fn=embed_fn,
        cancel_event=cancel_event,
        cleanup_stale_global=True,
        label="Repos",
        progress_callback=progress_callback,
    )


def sync_requested_sources(
    sources: list[dict[str, str]],
    *,
    force: bool = False,
    embed_fn=None,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Process only the runtime sources supplied with the current admin job."""

    from pipeline.adhoc_sources import registered_sources_from_records
    from pipeline.webhook import notify_sync_complete

    runtime_repo_sources = registered_sources_from_records(sources, source_types={"code", "document"})
    runtime_vault_sources = registered_sources_from_records(sources, source_types={"vault"})

    for source in runtime_vault_sources:
        sync_notes(
            vault_filter=source.name,
            force=force,
            embed_fn=embed_fn,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        if _cancel_requested(cancel_event):
            return
        _release_phase_memory(f"sources:vault:{source.name}")

    repo_sources = [_to_ingest_source(source) for source in runtime_repo_sources]
    if repo_sources:
        sync_repo_document_sources(
            repo_sources,
            force=force,
            embed_fn=embed_fn,
            cancel_event=cancel_event,
            cleanup_stale_global=False,
            label="Requested Sources",
            progress_callback=progress_callback,
        )
        if _cancel_requested(cancel_event):
            return
        _release_phase_memory("sources:repos")
    elif not runtime_vault_sources:
        print("==> [Requested Sources] Nenhuma fonte pedida válida encontrada.")
        return

    _generate_cag_packs()
    notify_sync_complete({"event_source": "sync_requested_sources"})


def _wait_for_resources(
    label: str,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
    child_id: str | None = None,
    phase: str | None = None,
    attempt: int = 1,
) -> bool:
    """Aguarda recursos disponíveis entre fases com orçamento finito."""
    from pipeline.governor import ResourceGovernor

    gov = ResourceGovernor(settings.performance, data_dir=str(settings.paths.data_dir))
    gov.start()
    try:
        _guard_governor(
            gov,
            label=label,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            child_id=child_id,
            phase=phase,
            attempt=attempt,
        )
    finally:
        gov.stop()
    return True


def sync_local(
    *,
    vault_filter: str | None = None,
    force: bool = False,
    gpu_first_embeddings: bool = False,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Embeddings: notas Obsidian + repos Git (só deltas — sync incremental)."""
    embed_fn = _gpu_first_embed_fn() if gpu_first_embeddings else None
    if gpu_first_embeddings:
        print("==> [Sync] all --force: embeddings em modo GPU-first (fallback CPU activo)")

    notes_kwargs = {"vault_filter": vault_filter, "force": force, "embed_fn": embed_fn}
    if cancel_event is not None:
        notes_kwargs["cancel_event"] = cancel_event
    if progress_callback is not None:
        notes_kwargs["progress_callback"] = progress_callback
    child_id = "local-notes"
    source_info = {"name": vault_filter or "all-vaults", "source_type": "vault"}
    started_at = time.time()
    _emit_progress(
        progress_callback,
        "child_started",
        child_id=child_id,
        phase="notes",
        source=source_info,
        started_at=started_at,
    )
    try:
        sync_notes(**notes_kwargs)
    except Exception as exc:
        resource_payload = (
            _resource_error_payload(exc, attempt=int(getattr(exc, "attempt", 1)))
            if hasattr(exc, "status")
            else {"status": "failed", "resource_state": "failed", "error": str(exc)[:1000]}
        )
        status = str(resource_payload.get("resource_state") or resource_payload.get("status") or "failed")
        resource_payload["status"] = status
        resource_payload["resource_state"] = status
        _emit_progress(
            progress_callback,
            "child_deferred" if status == "deferred_resource_pressure" else "child_failed",
            child_id=child_id,
            phase="notes",
            source=source_info,
            started_at=started_at,
            finished_at=time.time(),
            **resource_payload,
        )
        raise
    _emit_progress(
        progress_callback,
        "child_completed",
        child_id=child_id,
        phase="notes",
        source=source_info,
        started_at=started_at,
        finished_at=time.time(),
        result={},
    )
    if _cancel_requested(cancel_event):
        return
    _release_phase_memory("local:notes")  # free chunk lists, ASTs, source code from notes phase
    if force and not has_configured_sources():
        purge_local_rag_artifacts_for_empty_sources()
        return

    print()
    if not _wait_for_resources(
        "Transição notas→repos",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        child_id="transition-notes-repos",
        phase="transition",
    ):
        return
    repos_kwargs = {"force": force, "embed_fn": embed_fn}
    if cancel_event is not None:
        repos_kwargs["cancel_event"] = cancel_event
    if progress_callback is not None:
        repos_kwargs["progress_callback"] = progress_callback
    sync_repos(**repos_kwargs)
    if _cancel_requested(cancel_event):
        return
    _release_phase_memory("local:repos")  # free pipeline objects

    if not has_configured_sources():
        return

    # CAG: generate eager packs after sync
    _generate_cag_packs()

    # Stale graph alert: warn if configured graph roots exist but auto_update is disabled.
    graph_source_paths = tuple(settings.repos.paths)
    if graph_source_paths and not settings.graphify.auto_update:
        print()
        print("⚠  [Graph] auto_update=false — o graph pode estar desactualizado.")
        print("   Chama POST /admin/reprocess {\"target\":\"graph\"} para actualizar o graph estrutural.")

    # Webhook: notify external consumers that sync completed
    from pipeline.webhook import notify_sync_complete
    notify_sync_complete({"event_source": "sync_local"})


def sync_all(
    *,
    vault_filter: str | None = None,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Run the full admin sync.

    The forced all-target rebuild is the only path that opts into GPU-first
    embeddings. All other admin targets and query-time API paths stay unchanged.
    """
    sync_local(
        vault_filter=vault_filter,
        force=force,
        gpu_first_embeddings=force,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
    if force and not has_configured_sources():
        return
    if _cancel_requested(cancel_event):
        return
    sync_graphify(force=force, cancel_event=cancel_event, progress_callback=progress_callback)
    if _cancel_requested(cancel_event):
        return
    child_id = "cag-packs"
    started_at = time.time()
    _emit_progress(
        progress_callback,
        "child_started",
        child_id=child_id,
        phase="cag",
        source={"name": "cag-packs", "source_type": "cag"},
        started_at=started_at,
    )
    try:
        generate_cag_packs()
    except Exception as exc:
        _emit_progress(
            progress_callback,
            "child_failed",
            child_id=child_id,
            phase="cag",
            source={"name": "cag-packs", "source_type": "cag"},
            started_at=started_at,
            finished_at=time.time(),
            error=str(exc)[:1000],
        )
        raise
    _emit_progress(
        progress_callback,
        "child_completed",
        child_id=child_id,
        phase="cag",
        source={"name": "cag-packs", "source_type": "cag"},
        started_at=started_at,
        finished_at=time.time(),
        result={},
    )


def sync_graphify(
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Grafos: constrói/actualiza grafos para todos os repos.

    Se *force* é True, apaga o manifest.json de cada repo antes de extrair,
    forçando um rebuild completo (AST + LLM) mesmo que o grafo já exista.
    Após build, exporta para o vault Obsidian configurado em graph_vault_dir.
    """
    if not settings.graphify.enabled:
        print("==> [Graphify] Desabilitado em config/rag/internal.toml [graphify] enabled = false. Skipping.")
        return
    if _cancel_requested(cancel_event):
        return
    child_id = "graphify"
    started_at = time.time()
    _emit_progress(
        progress_callback,
        "child_started",
        child_id=child_id,
        phase="graphify",
        source={"name": "configured-graph-roots", "source_type": "graph"},
        started_at=started_at,
    )
    if force:
        from pipeline.graph.obsidian_export import purge_generated_vault

        files_removed, dirs_removed = purge_generated_vault()
        print(
            "==> [Obsidian] force=true: vault derivado limpo antes do rebuild "
            f"({dirs_removed} dirs/{files_removed} files removidos)."
        )
    try:
        from pipeline.graph.builder import build_graphs
        build_graphs(force=force, cancel_event=cancel_event)
    except ImportError:
        print("==> [Graphify] graphifyy não está instalado. Instala com: pip install graphifyy")
        _emit_progress(
            progress_callback,
            "child_completed",
            child_id=child_id,
            phase="graphify",
            source={"name": "configured-graph-roots", "source_type": "graph"},
            started_at=started_at,
            finished_at=time.time(),
            result={"skipped": "graphify_missing"},
        )
        return
    except FileNotFoundError:
        print("==> [Graphify] Comando 'graphify' não encontrado. Instala com: pip install graphifyy")
        _emit_progress(
            progress_callback,
            "child_completed",
            child_id=child_id,
            phase="graphify",
            source={"name": "configured-graph-roots", "source_type": "graph"},
            started_at=started_at,
            finished_at=time.time(),
            result={"skipped": "graphify_command_missing"},
        )
        return

    # Exportar grafos para o vault Obsidian
    print()
    try:
        from pipeline.graph.obsidian_export import export_all
        export_all(force=force)
    except Exception as e:
        print(f"==> [Obsidian] Erro na exportação para o vault (não fatal): {e}")
    _emit_progress(
        progress_callback,
        "child_completed",
        child_id=child_id,
        phase="graphify",
        source={"name": "configured-graph-roots", "source_type": "graph"},
        started_at=started_at,
        finished_at=time.time(),
        result={},
    )
