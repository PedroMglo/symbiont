"""Wrapper para execução do graphify CLI.

Invoca `graphify extract` via subprocess para cada raiz configurada.
O graphify processa:
  - Código Python via AST (tree-sitter) — local, sem LLM
  - Markdown/docs via Ollama — local, sem API key externa

Os grafos ficam persistidos em:
  {settings.graphify.output_dir}/{root_name}/graphify-out/graph.json
"""

from __future__ import annotations

import fnmatch
import gc
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from obsidian_rag.config import settings

log = logging.getLogger(__name__)

# File extensions that require LLM semantic extraction (vs AST-only).
_DOC_EXTENSIONS = frozenset({
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".env",
    ".sh",
    ".zsh",
    ".bash",
    ".ps1",
    ".sql",
    ".xml",
    ".csv",
    ".tsv",
})
_CODE_EXTENSIONS = frozenset({
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".cxx",
    ".cc",
    ".hpp",
    ".hxx",
    ".cs",
    ".rb",
})
_GRAPH_STAGE_DOC_EXTENSIONS = _DOC_EXTENSIONS - {".csv", ".tsv"}
_GRAPHABLE_EXTENSIONS = _GRAPH_STAGE_DOC_EXTENSIONS | _CODE_EXTENSIONS
_REPO_DISCOVERY_IGNORE = {
    ".cache", ".local", ".venv", "venv", "env", "node_modules",
    "dist", "build", "__pycache__", ".Trash", "Trash",
}
_GRAPH_STAGE_DIRS_SKIP = _REPO_DISCOVERY_IGNORE | {".git", "graphify-out"}


def _is_git_repo(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def _configured_graph_roots() -> list[Path]:
    """Return graphable roots from [repos].paths.

    A configured Git checkout is graphified as-is. A configured folder that
    contains Git repos expands to those repos. A configured folder without Git
    repos is graphified as a plain document/code root.
    """
    seen: set[Path] = set()
    roots: list[Path] = []

    def _add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            roots.append(resolved)
            seen.add(resolved)

    for raw_root in settings.repos.paths:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            continue
        if root.is_file():
            _add(root.parent)
            continue
        if _is_git_repo(root):
            _add(root)
            continue
        found_git = False
        for current, dirs, _files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [d for d in dirs if d not in _REPO_DISCOVERY_IGNORE and not d.endswith(".egg-info")]
            if _is_git_repo(current_path):
                found_git = True
                _add(current_path)
                dirs[:] = []
        if not found_git:
            _add(root)
    return sorted(roots)


def _configured_git_repos() -> list[Path]:
    """Compatibility alias for callers/tests that still use the old name."""
    return _configured_graph_roots()


def _graphify_bin() -> str:
    """Resolve graphify binary: prefer the one in the current venv/scripts dir."""
    # Check alongside the running Python interpreter first
    scripts_dir = Path(sys.executable).parent
    candidate = scripts_dir / "graphify"
    if candidate.exists():
        return str(candidate)
    # Fallback to PATH lookup
    found = shutil.which("graphify")
    if found:
        return found
    raise FileNotFoundError(
        "Comando 'graphify' não encontrado. "
        "Instala com: pip install graphifyy"
    )


def _graphify_output_dir(repo_path: Path) -> Path:
    """Directório de output do graphify para um repo.

    graphify extract --out DIR escreve em DIR/graphify-out/,
    portanto apontamos --out para {output_dir}/{repo_name}.
    """
    return Path(settings.graphify.output_dir) / repo_path.name / "graphify-out"


def _graphify_out_parent(repo_path: Path) -> Path:
    """O valor a passar a --out (o pai de graphify-out/)."""
    return Path(settings.graphify.output_dir) / repo_path.name


def _graph_json_path(repo_path: Path) -> Path:
    return _graphify_output_dir(repo_path) / "graph.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_graph_query_backend(repo_path: Path, graph_json: Path) -> bool:
    """Import graph.json into the configured service-backed query backend."""
    if not getattr(settings.graphify, "import_on_build", True):
        return True
    if str(getattr(settings.graphify, "query_backend", "json")).strip().lower() == "json":
        return True
    if not graph_json.exists():
        log.error("[Graphify] graph.json não existe para importar: %s", graph_json)
        return False
    try:
        graph_data = json.loads(graph_json.read_text(encoding="utf-8"))
        from obsidian_rag.pipeline.graph.backend import get_graph_backend

        result = get_graph_backend().import_graph(repo_path.name, graph_data, source_hash=_file_sha256(graph_json))
        status = "skip" if result.skipped else "import"
        log.info(
            "[GraphBackend] %s %s: %d nós, %d edges via %s",
            status,
            repo_path.name,
            result.node_count,
            result.edge_count,
            result.backend,
        )
        return True
    except Exception as exc:
        log.error("[GraphBackend] Falha ao importar '%s': %s", repo_path.name, exc)
        return False


def _report_path(repo_path: Path) -> Path:
    return _graphify_output_dir(repo_path) / "GRAPH_REPORT.md"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _should_stage_graph_root(repo_path: Path) -> bool:
    """Use a writable filtered copy for read-only host/document roots."""
    if _is_git_repo(repo_path):
        return False
    host_home = os.environ.get("AI_RAG_HOST_HOME", "").strip()
    if host_home and _is_under(repo_path, Path(host_home)):
        return True
    return not os.access(repo_path, os.W_OK)


def _graph_stage_source_dir(repo_path: Path) -> Path:
    return Path(settings.graphify.output_dir) / repo_path.name / "source"


def _file_size_limit_for_graph(path: Path) -> int:
    try:
        return int(settings.sync.limits.max_file_size_mb_text) * 1024 * 1024
    except Exception:
        return 50 * 1024 * 1024


def _prepare_graph_input(repo_path: Path) -> Path:
    """Return a graphify input root, staging non-Git/read-only folders if needed."""
    if not _should_stage_graph_root(repo_path):
        return repo_path

    stage_dir = _graph_stage_source_dir(repo_path)
    tmp_dir = stage_dir.with_name(f"{stage_dir.name}.tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    skip_patterns = settings.graphify.skip_patterns
    size_limit = _file_size_limit_for_graph(repo_path)
    copied = 0
    for current, dirs, files in os.walk(repo_path):
        current_path = Path(current)
        try:
            rel_dir = current_path.relative_to(repo_path)
        except ValueError:
            continue
        dirs[:] = [
            d for d in dirs
            if d not in _GRAPH_STAGE_DIRS_SKIP
            and not d.endswith(".egg-info")
            and not d.startswith(".")
        ]
        for name in files:
            src = current_path / name
            if not src.is_file() or src.is_symlink():
                continue
            if src.suffix.lower() not in _GRAPHABLE_EXTENSIONS:
                continue
            rel = (rel_dir / name).as_posix() if rel_dir != Path(".") else name
            if skip_patterns and _matches_skip_patterns(rel, skip_patterns):
                continue
            try:
                if src.stat().st_size > size_limit:
                    continue
            except OSError:
                continue
            dst = tmp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    tmp_dir.rename(stage_dir)
    log.info("[Graphify] %s — staged %d ficheiros para %s", repo_path.name, copied, stage_dir)
    return stage_dir


# ---------------------------------------------------------------------------
# Skip pattern matching
# ---------------------------------------------------------------------------

def _matches_skip_patterns(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Check if a relative path matches any configured skip pattern (fnmatch glob)."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        # Also check against just the filename for simple patterns
        if "/" not in pattern and fnmatch.fnmatch(Path(rel_path).name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Incremental change detection
# ---------------------------------------------------------------------------

def _file_md5(path: Path) -> str:
    """Compute MD5 hex digest of a file (matches graphify's manifest hash)."""
    h = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _detect_changes(repo_path: Path, manifest_path: Path) -> tuple[bool, bool]:
    """Compare graphify manifest.json against current repo files.

    Returns:
        (has_changes, has_doc_changes) — *has_doc_changes* is True when
        at least one changed/new file has a doc extension (.md, .txt, …).
    """
    if not manifest_path.exists():
        return True, True  # no manifest → full build needed

    try:
        manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True, True

    has_changes = False
    has_doc_changes = False
    skip_patterns = settings.graphify.skip_patterns

    # Check existing manifest entries for changed/deleted files
    for file_path_str, info in manifest.items():
        fp = Path(file_path_str)
        # Skip files matching skip patterns
        try:
            rel = str(fp.relative_to(repo_path))
        except ValueError:
            rel = file_path_str
        if skip_patterns and _matches_skip_patterns(rel, skip_patterns):
            continue

        if not fp.exists():
            has_changes = True
            if fp.suffix.lower() in _DOC_EXTENSIONS:
                has_doc_changes = True
            continue
        stored_hash = info.get("hash", "")
        if stored_hash and _file_md5(fp) != stored_hash:
            has_changes = True
            if fp.suffix.lower() in _DOC_EXTENSIONS:
                has_doc_changes = True

    # Early exit if we already know docs changed
    if has_doc_changes:
        return True, True

    # Check for new files not in the manifest (walk repo)
    manifest_paths = set(manifest.keys())
    try:
        for child in repo_path.rglob("*"):
            if not child.is_file():
                continue
            # Skip hidden dirs and common non-source paths
            parts = child.relative_to(repo_path).parts
            if any(p.startswith(".") or p in ("node_modules", "__pycache__", ".git", "venv", ".venv") for p in parts):
                continue
            # Apply configured skip patterns
            rel_path = str(child.relative_to(repo_path))
            if skip_patterns and _matches_skip_patterns(rel_path, skip_patterns):
                continue
            if str(child) not in manifest_paths:
                has_changes = True
                if child.suffix.lower() in _DOC_EXTENSIONS:
                    has_doc_changes = True
                    return True, True
    except OSError:
        pass

    return has_changes, has_doc_changes


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_graph(repo_path: Path | str, *, force: bool = False) -> bool:
    """Executa graphify extract/update para um único repo.

    Incremental logic (when *force* is False):
      1. Read graphify's manifest.json and compare file hashes.
      2. If nothing changed → skip subprocess entirely.
      3. If only code files changed → ``graphify update`` (AST-only, no LLM).
      4. If doc files also changed → ``graphify extract`` (AST + LLM).

    Returns True if successful (or no changes), False on error.
    """
    repo_path = Path(repo_path).expanduser().resolve()
    if not repo_path.exists():
        log.warning("[Graphify] Repo não encontrado: %s — skipping.", repo_path)
        return False

    output_dir = _graphify_output_dir(repo_path)
    graph_json = _graph_json_path(repo_path)
    manifest_json = output_dir / "manifest.json"

    # Criar directório de output se necessário
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    # force=True: apagar manifest para trigger rebuild completo (AST + LLM)
    if force and manifest_json.exists():
        manifest_json.unlink()
        log.info("[Graphify] Manifest removido — rebuild completo forçado.")

    if graph_json.exists() and not force and not settings.graphify.auto_update:
        log.info("[Graphify] Grafo já existe para '%s' e auto_update=false — skipping.", repo_path.name)
        return _sync_graph_query_backend(repo_path, graph_json)

    graph_input_path = _prepare_graph_input(repo_path)

    # --- Incremental change detection ---
    use_update = False  # True → graphify update (AST-only), False → graphify extract
    if not force and manifest_json.exists() and graph_json.exists():
        has_changes, has_doc_changes = _detect_changes(graph_input_path, manifest_json)
        if not has_changes:
            log.info("[Graphify] Sem alterações em '%s' — skipping.", repo_path.name)
            print(f"  [graphify] {repo_path.name}: sem alterações — skip")
            return _sync_graph_query_backend(repo_path, graph_json)
        if not has_doc_changes:
            use_update = True
            log.info("[Graphify] Apenas código alterado em '%s' — graphify update (sem LLM).", repo_path.name)

    if force:
        mode = "rebuild completo"
    elif use_update:
        mode = "update (AST-only)"
    elif manifest_json.exists():
        mode = "incremental"
    else:
        mode = "build inicial"

    # --- Build command ---
    graphify_cmd = _graphify_bin()
    if use_update:
        cmd = [graphify_cmd, "update", str(graph_input_path)]
    else:
        cmd = [
            graphify_cmd, "extract", str(graph_input_path),
            "--backend", settings.graphify.backend,
            "--out", str(_graphify_out_parent(repo_path)),
            "--max-concurrency", str(settings.graphify.max_concurrency),
            "--token-budget", str(settings.graphify.token_budget),
            "--api-timeout", str(settings.graphify.api_timeout or settings.performance.enrich_timeout),
        ]
        if settings.graphify.extract_mode:
            cmd += ["--mode", settings.graphify.extract_mode]

    # Modelo específico (configurado em models.json, role graph-enrichment)
    if settings.graphify.model and not use_update:
        cmd += ["--model", settings.graphify.model]

    log.info("[Graphify] %s — %s", repo_path.name, mode)
    log.debug("[Graphify] Comando: %s", " ".join(cmd))

    # Graphify exige OLLAMA_BASE_URL para o backend ollama.
    # Injectar a partir do base_url configurado em config/rag/internal.toml [ollama].
    env = os.environ.copy()
    # graphify writes AST/semantic caches below GRAPHIFY_OUT. Keep this
    # absolute and inside the configured output dir so read-only document roots
    # (for example host ~/Downloads mounts) are never mutated.
    env["GRAPHIFY_OUT"] = str(_graphify_output_dir(repo_path).resolve())
    if settings.graphify.backend == "ollama":
        ollama_base = settings.ollama.base_url.rstrip("/")
        # Force-set with /v1 — graphify uses the OpenAI client which requires
        # the base_url to end in /v1. setdefault() would not override a pre-set
        # OLLAMA_BASE_URL (e.g. from docker-compose) that lacks /v1.
        target_url = f"{ollama_base}/v1" if not ollama_base.endswith("/v1") else ollama_base
        env["OLLAMA_BASE_URL"] = target_url
        # Graphify/litellm requires OLLAMA_API_KEY in env.
        # Security policy: no placeholders/fallbacks; user must provide a real key.
        # Also support the Docker secrets pattern (OLLAMA_API_KEY_FILE).
        if not env.get("OLLAMA_API_KEY", "").strip():
            key_file = env.get("OLLAMA_API_KEY_FILE", "").strip()
            if key_file:
                try:
                    with open(key_file) as _f:
                        env["OLLAMA_API_KEY"] = _f.read().strip()
                except OSError:
                    pass
        if not env.get("OLLAMA_API_KEY", "").strip():
            raise ValueError(
                "OLLAMA_API_KEY is required for graphify backend 'ollama'. "
                "Set OLLAMA_API_KEY in the environment/secrets before running graph build."
            )

    try:
        timeout = settings.performance.graph_timeout or None
        result = subprocess.run(
            cmd,
            cwd=str(graph_input_path),
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.stderr:
            log.debug("[Graphify] stderr: %s", result.stderr.strip())
        log.info("[Graphify] Concluído. Grafo em: %s", graph_json)

        # --- Post-build: schema lock filtering ---
        if settings.graphify.schema_locked and graph_json.exists():
            _apply_schema_filter(graph_json)

        return _sync_graph_query_backend(repo_path, graph_json)
    except subprocess.TimeoutExpired:
        log.error(
            "[Graphify] TIMEOUT (%ds) para '%s' — skipping. "
            "Ajusta graph_timeout em config/rag/internal.toml se necessário.",
            settings.performance.graph_timeout, repo_path.name,
        )
        return False
    except subprocess.CalledProcessError as e:
        log.error(
            "[Graphify] ERRO (exit code %d): %s\nstdout: %s\nstderr: %s",
            e.returncode, e, (e.stdout or "").strip(), (e.stderr or "").strip(),
        )
        return False
    except FileNotFoundError:
        raise FileNotFoundError(
            "Comando 'graphify' não encontrado. "
            "Instala com: pip install graphifyy"
        )


# ---------------------------------------------------------------------------
# Post-build: schema filter
# ---------------------------------------------------------------------------

def _apply_schema_filter(graph_json: Path) -> None:
    """Apply schema lock to graph.json — remove unknown node/relation types."""
    from obsidian_rag.pipeline.graph.schema import (
        filter_graph,
        get_allowed_node_types,
        get_allowed_relation_types,
    )

    try:
        graph_data = json.loads(graph_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[Graphify] Não foi possível ler graph.json para schema filter: %s", e)
        return

    node_types = get_allowed_node_types(settings.graphify.allowed_node_types_file)
    rel_types = get_allowed_relation_types(settings.graphify.allowed_relation_types_file)

    filtered, stats = filter_graph(
        graph_data,
        allowed_node_types=node_types,
        allowed_relation_types=rel_types,
    )

    if stats["nodes_removed"] > 0 or stats["links_removed"] > 0:
        log.info(
            "[Graphify] Schema filter: removidos %d nós e %d edges (mantidos %d nós, %d edges).",
            stats["nodes_removed"], stats["links_removed"],
            stats["nodes_kept"], stats["links_kept"],
        )
        # Write filtered graph back
        tmp = graph_json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(graph_json)
    else:
        log.debug("[Graphify] Schema filter: sem tipos desconhecidos.")


# ---------------------------------------------------------------------------
# Pre-filter: score doc files before LLM extraction
# ---------------------------------------------------------------------------

def get_prefilter_stats(repo_path: Path) -> dict[str, int]:
    """Run prefilter on repo doc files and return pass/skip counts (for diagnostics)."""
    if not settings.graphify.prefilter_enabled:
        return {"prefilter_disabled": 1}

    from obsidian_rag.pipeline.graph.prefilter import filter_files_for_llm

    skip_patterns = settings.graphify.skip_patterns
    doc_files: list[Path] = []

    for child in repo_path.rglob("*"):
        if not child.is_file():
            continue
        if child.suffix.lower() not in _DOC_EXTENSIONS:
            continue
        rel_path = str(child.relative_to(repo_path))
        if skip_patterns and _matches_skip_patterns(rel_path, skip_patterns):
            continue
        doc_files.append(child)

    pass_list, skip_list = filter_files_for_llm(
        doc_files,
        threshold=settings.graphify.candidate_score_threshold,
        min_chars=settings.graphify.prefilter_min_chars,
    )
    return {"pass": len(pass_list), "skip": len(skip_list), "total_docs": len(doc_files)}


def build_graphs(*, force: bool = False) -> None:
    """Executa graphify extract para todas as roots configuradas em config/rag/user.toml.

    Se *force* for True, faz update mesmo que auto_update=false.
    Usa ThreadPoolExecutor quando graph_parallel_jobs > 1 (subprocess.run é
    thread-safe — cada worker espera por um processo isolado).
    """
    graph_roots = _configured_graph_roots()
    if not graph_roots:
        log.info("[Graphify] Sem roots configuradas. Skipping.")
        return

    model_info = f" | modelo: {settings.graphify.model}" if settings.graphify.model else ""
    parallel = settings.performance.graph_parallel_jobs
    log.info(
        "[Graphify] Backend: %s%s | force: %s | parallel: %d",
        settings.graphify.backend, model_info, force, parallel,
    )

    from obsidian_rag.tuning import should_throttle

    def _build_one(repo_path: str) -> bool:
        """Build a single repo with throttle check and optional VRAM guard."""
        lease = None
        advice = should_throttle(settings.performance, str(settings.paths.data_dir))
        if advice.low_disk:
            log.error("[Graphify] Disco quase cheio — skipping '%s'. %s", Path(repo_path).name, advice.reason)
            return False
        if advice.pause_sync:
            import time as _time
            log.warning("[Graphify] Pressão antes de '%s': %s", Path(repo_path).name, advice.reason)
            for _attempt in range(1, 4):
                _time.sleep(5)
                advice = should_throttle(settings.performance, str(settings.paths.data_dir))
                if not advice.pause_sync:
                    break
            else:
                log.warning("[Graphify] Pressão mantém-se — adiado '%s'.", Path(repo_path).name)
                return False
        if advice.reduce_workers and not force:
            log.info("[Graphify] adiado por pressão de recursos para '%s': %s", Path(repo_path).name, advice.reason)
            return False

        # VRAM guard: ensure enough free VRAM before launching LLM-intensive subprocess
        if parallel > 1:
            try:
                from obsidian_rag.pipeline.governor import _read_vram
                _used, total, _pct = _read_vram()
                free_gb = total - _used if total > 0 else 0
                if total > 0 and free_gb < 1.5:
                    import time as _time
                    log.info(
                        "[Graphify] VRAM baixa (%.1fGB livre) — aguardando antes de '%s'...",
                        free_gb, Path(repo_path).name,
                    )
                    _time.sleep(10)
            except Exception:
                pass  # pynvml unavailable — proceed without guard

        try:
            from obsidian_rag.integrations.resource_governor_client import request_lease

            lease = request_lease(
                component="graphify",
                lane="background",
                lease_scope="batch",
                resource_class="model_runtime",
                capability="graph_llm",
                estimated_duration_seconds=settings.performance.graph_timeout or 900,
                estimated_vram_mb=2048,
                preemptible=True,
                quality_policy="degrade_allowed",
                estimated_quality_impact="medium",
                idempotency_suffix=Path(repo_path).name,
            )
            if not lease.granted:
                log.info("[Graphify] adiado pelo Resource Governor para '%s': %s", Path(repo_path).name, lease.decision.reason)
                return False
            return build_graph(repo_path, force=force)
        finally:
            if lease is not None:
                lease.release()

    if parallel > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        log.info("[Graphify] A processar %d roots em paralelo (max %d workers)...",
                 len(graph_roots), parallel)
        successes = 0
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_build_one, rp): rp
                for rp in graph_roots
            }
            for future in as_completed(futures):
                if future.result():
                    successes += 1
                gc.collect()
    else:
        successes = 0
        for repo_path in graph_roots:
            if _build_one(repo_path):
                successes += 1
            gc.collect()

    log.info("[Graphify] %d/%d roots processadas.", successes, len(graph_roots))


def graph_exists(repo_name: str) -> bool:
    """True se o graph.json existe para um repo."""
    for repo_path in _configured_graph_roots():
        if Path(repo_path).name == repo_name:
            return _graph_json_path(repo_path).exists()
    return False


def get_graph_json_path(repo_name: str) -> Path | None:
    """Devolve o path para o graph.json de um repo, ou None se não existir."""
    for repo_path in _configured_graph_roots():
        p = Path(repo_path)
        if p.name == repo_name:
            gp = _graph_json_path(p)
            return gp if gp.exists() else None
    return None


def get_report_path(repo_name: str) -> Path | None:
    """Devolve o path para o GRAPH_REPORT.md de um repo, ou None se não existir."""
    for repo_path in _configured_graph_roots():
        p = Path(repo_path)
        if p.name == repo_name:
            rp = _report_path(p)
            return rp if rp.exists() else None
    return None
