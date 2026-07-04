"""Extractive pack generators for CAG context packs.

All generators are EXTRACTIVE — they compile existing information
from the filesystem, config, and runtime state. No LLM calls.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import httpx

from obsidian_rag.cag.freshness import compute_config_version, compute_source_hash
from obsidian_rag.cag.store import PackStore

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE: dict[str, str] = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text

log = logging.getLogger(__name__)


class PackType(str, Enum):
    """Registered CAG pack types."""
    PROJECT_ARCHITECTURE = "project_architecture"
    REPO_STATE = "repo_state"
    VAULT_SUMMARY = "vault_summary"
    RECURRING_ERRORS = "recurring_errors"
    CONFIG_ENVIRONMENT = "config_environment"
    SYSTEM_STATE = "system_state"
    LOCAL_SERVICES = "local_services"
    LOCAL_MODELS = "local_models"
    SECURITY_EXCLUSIONS = "security_exclusions"
    RAG_INDEX_STATE = "rag_index_state"
    PENDING_TASKS = "pending_tasks"
    KNOWLEDGE_GRAPH_SUMMARY = "knowledge_graph_summary"


@dataclass(frozen=True)
class PackSpec:
    """Specification for a pack type."""
    pack_type: PackType
    generator: Callable[[PackStore, str], str | None]
    default_scope: str
    eager: bool          # generate after sync
    ttl_seconds: int     # 0 = use config default


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: list[PackSpec] = []


def _register(
    pack_type: PackType,
    *,
    scope: str = "global",
    eager: bool = True,
    ttl: int = 0,
) -> Callable:
    """Decorator to register a pack generator."""
    def decorator(fn: Callable[[PackStore, str], str | None]) -> Callable:
        _REGISTRY.append(PackSpec(
            pack_type=pack_type,
            generator=fn,
            default_scope=scope,
            eager=eager,
            ttl_seconds=ttl,
        ))
        return fn
    return decorator


def get_registry() -> list[PackSpec]:
    """Return the full pack registry."""
    return list(_REGISTRY)


def get_eager_specs() -> list[PackSpec]:
    """Return only specs that should generate eagerly (after sync)."""
    return [s for s in _REGISTRY if s.eager]


def get_lazy_specs() -> list[PackSpec]:
    """Return specs generated on-demand."""
    return [s for s in _REGISTRY if not s.eager]


# ---------------------------------------------------------------------------
# Helper: truncate to max tokens
# ---------------------------------------------------------------------------

def _truncate(text: str, max_tokens: int = 2000) -> str:
    """Truncate text to approximate token budget (by lines)."""
    from obsidian_rag.retrieval.budget import estimate_tokens
    if estimate_tokens(text) <= max_tokens:
        return text
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    used = 0
    for line in lines:
        t = estimate_tokens(line)
        if used + t > max_tokens and result:
            result.append("... [truncated]\n")
            break
        result.append(line)
        used += t
    return "".join(result)


# ---------------------------------------------------------------------------
# 1. project_architecture — repo structure overview
# ---------------------------------------------------------------------------

@_register(PackType.PROJECT_ARCHITECTURE, eager=True, ttl=3600)
def gen_project_architecture(store: PackStore, scope: str) -> str | None:
    """Extract project structure from the workspace."""
    from obsidian_rag.config import PROJECT_ROOT

    lines = ["# Project Architecture\n"]

    # Key directories
    src = PROJECT_ROOT / "obsidian_rag"
    if src.is_dir():
        modules = sorted(
            p.name for p in src.iterdir()
            if p.is_dir() and not p.name.startswith("__")
        )
        lines.append(f"## Modules ({len(modules)})\n")
        for m in modules:
            subfiles = sorted(
                f.stem for f in (src / m).glob("*.py")
                if f.stem != "__init__"
            )
            lines.append(f"- **{m}/** — {', '.join(subfiles) or '(empty)'}")
        lines.append("")

    # Key config files
    configs = [f for f in ["rag.internal.toml", "rag.user.toml", "pyproject.toml",
                           "docker-compose.yml", "Makefile"]
               if (PROJECT_ROOT / f).exists()]
    if configs:
        lines.append("## Config files")
        for c in configs:
            lines.append(f"- {c}")
        lines.append("")

    content = "\n".join(lines)
    return _truncate(content) if content.strip() else None


# ---------------------------------------------------------------------------
# 2. repo_state — per-repo git status
# ---------------------------------------------------------------------------

@_register(PackType.REPO_STATE, scope="per-repo", eager=True, ttl=3600)
def gen_repo_state(store: PackStore, scope: str) -> str | None:
    """Extract git state for configured repos."""
    from obsidian_rag.config import settings

    lines = ["# Repository State\n"]
    repos = settings.repos.paths
    if not repos:
        return None

    for repo_path in repos:
        repo_path = Path(repo_path)
        name = repo_path.name
        if not repo_path.is_dir():
            lines.append(f"## {name}\n- **Status:** not found\n")
            continue

        lines.append(f"## {name}")

        # Git branch
        branch = _git_cmd(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        if branch:
            lines.append(f"- **Branch:** {branch}")

        # Last commit
        last_commit = _git_cmd(repo_path, "log", "-1", "--format=%h %s (%cr)")
        if last_commit:
            lines.append(f"- **Last commit:** {last_commit}")

        # Dirty state
        status = _git_cmd(repo_path, "status", "--porcelain", "--short")
        if status:
            changed = len(status.strip().splitlines())
            lines.append(f"- **Uncommitted changes:** {changed} files")
        else:
            lines.append("- **Working tree:** clean")

        lines.append("")

    content = "\n".join(lines)
    return _truncate(content) if content.strip() else None


# ---------------------------------------------------------------------------
# 3. vault_summary — vault statistics
# ---------------------------------------------------------------------------

@_register(PackType.VAULT_SUMMARY, eager=True, ttl=3600)
def gen_vault_summary(store: PackStore, scope: str) -> str | None:
    """Count notes and folders in configured vaults."""
    from obsidian_rag.config import settings

    lines = ["# Vault Summary\n"]
    for vault_dir in settings.paths.vault_dirs:
        vault_dir = Path(vault_dir)
        if not vault_dir.is_dir():
            continue

        md_files = list(vault_dir.rglob("*.md"))
        folders = [p for p in vault_dir.rglob("*") if p.is_dir() and not p.name.startswith(".")]
        lines.append(f"## {vault_dir.name}")
        lines.append(f"- **Notes:** {len(md_files)}")
        lines.append(f"- **Folders:** {len(folders)}")

        # Top-level folder listing
        top = sorted(
            p.name for p in vault_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        if top:
            lines.append(f"- **Top folders:** {', '.join(top[:15])}")
        lines.append("")

    content = "\n".join(lines)
    return _truncate(content) if content.strip() else None


# ---------------------------------------------------------------------------
# 4. recurring_errors — recent error patterns from logs
# ---------------------------------------------------------------------------

@_register(PackType.RECURRING_ERRORS, eager=True, ttl=3600)
def gen_recurring_errors(store: PackStore, scope: str) -> str | None:
    """Scan recent log files for error patterns."""
    from obsidian_rag.config import settings

    data_dir = Path(settings.paths.data_dir)
    log_files = list(data_dir.glob("*.log")) + list(data_dir.glob("logs/*.log"))

    if not log_files:
        return None

    error_lines: list[str] = []
    for lf in log_files[-3:]:  # last 3 log files
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines()[-200:]:  # last 200 lines
                low = line.lower()
                if "error" in low or "exception" in low or "traceback" in low:
                    error_lines.append(line.strip())
        except OSError:
            continue

    if not error_lines:
        return "# Recent Errors\n\nNo errors found in recent logs.\n"

    # Deduplicate by first 80 chars
    seen: set[str] = set()
    unique: list[str] = []
    for e in error_lines:
        key = e[:80]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    lines = ["# Recent Errors\n", f"Found {len(unique)} unique error patterns:\n"]
    for e in unique[:20]:  # cap at 20
        lines.append(f"- `{e[:120]}`")
    lines.append("")

    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# 5. config_environment — current config summary
# ---------------------------------------------------------------------------

@_register(PackType.CONFIG_ENVIRONMENT, eager=True, ttl=3600)
def gen_config_environment(store: PackStore, scope: str) -> str | None:
    """Summarise current RAG configuration."""
    from obsidian_rag.config import settings

    lines = ["# Configuration Environment\n"]
    s = settings

    lines.append("## Paths")
    lines.append(f"- vault_dir: {s.paths.vault_dir}")
    lines.append(f"- data_dir: {s.paths.data_dir}")
    lines.append("")

    lines.append("## Ollama")
    lines.append(f"- base_url: {s.ollama.base_url}")
    lines.append(f"- embedding_model: {s.ollama.embedding_model}")
    lines.append("")

    lines.append("## Retrieval")
    lines.append(f"- top_k: {s.retrieval.top_k}")
    lines.append(f"- score_threshold: {s.retrieval.score_threshold}")
    lines.append(f"- token_budget: {s.retrieval.token_budget}")
    lines.append(f"- context_mode: {s.retrieval.context_mode}")
    lines.append("")

    lines.append("## Pipeline")
    lines.append(f"- engine: {s.pipeline.engine}")
    lines.append(f"- parser_workers: {s.performance.parser_workers}")
    lines.append("")

    lines.append("## Router")
    lines.append(f"- enabled: {s.router.enabled}")
    lines.append(f"- model: {s.router.model}")
    lines.append("")

    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# 6. system_state — live hardware snapshot (short TTL)
# ---------------------------------------------------------------------------

@_register(PackType.SYSTEM_STATE, eager=False, ttl=300)
def gen_system_state(store: PackStore, scope: str) -> str | None:
    """Quick hardware snapshot."""
    import shutil

    lines = ["# System State\n"]

    try:
        import psutil
        mem = psutil.virtual_memory()
        lines.append(f"- **RAM:** {mem.used / 1e9:.1f}/{mem.total / 1e9:.1f} GB ({mem.percent}%)")
        cpu = psutil.cpu_percent(interval=0.3)
        lines.append(f"- **CPU:** {cpu}% ({psutil.cpu_count()} cores)")
        swap = psutil.swap_memory()
        if swap.total > 0:
            lines.append(f"- **Swap:** {swap.used / 1e9:.1f}/{swap.total / 1e9:.1f} GB ({swap.percent}%)")
    except ImportError:
        lines.append("- psutil not available")

    disk = shutil.disk_usage("/")
    lines.append(f"- **Disk /:** {disk.free / 1e9:.1f} GB free / {disk.total / 1e9:.1f} GB total")

    # GPU (nvidia-smi)
    gpu = _run_cmd("nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu",
                   "--format=csv,noheader,nounits")
    if gpu:
        lines.append(f"- **GPU:** {gpu.strip()}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. local_services — Docker, Qdrant, Ollama
# ---------------------------------------------------------------------------

@_register(PackType.LOCAL_SERVICES, eager=False, ttl=300)
def gen_local_services(store: PackStore, scope: str) -> str | None:
    """Check status of local services."""
    lines = ["# Local Services\n"]

    # Ollama
    from obsidian_rag.config import settings
    ollama_url = settings.ollama.base_url
    try:
        resp = httpx.get(f"{ollama_url}/api/version", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        lines.append(f"- **Ollama:** running (v{data.get('version', '?')}) at {ollama_url}")
    except Exception:
        lines.append(f"- **Ollama:** not reachable at {ollama_url}")

    # Qdrant
    try:
        qdrant_url = settings.store.qdrant_url
        resp = httpx.get(f"{qdrant_url}/collections", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        cols = data.get("result", {}).get("collections", [])
        lines.append(f"- **Qdrant:** running at {qdrant_url} ({len(cols)} collections)")
    except Exception:
        lines.append("- **Qdrant:** not reachable (may be embedded mode)")

    # Docker
    docker = _run_cmd("docker", "ps", "--format", "{{.Names}}: {{.Status}}")
    if docker:
        containers = docker.strip().splitlines()
        lines.append(f"- **Docker:** {len(containers)} running containers")
        for c in containers[:8]:
            lines.append(f"  - {c}")
    else:
        lines.append("- **Docker:** not running or not installed")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. local_models — installed Ollama models
# ---------------------------------------------------------------------------

@_register(PackType.LOCAL_MODELS, eager=True, ttl=3600)
def gen_local_models(store: PackStore, scope: str) -> str | None:
    """List installed Ollama models."""
    from obsidian_rag.config import settings

    try:
        resp = httpx.get(f"{settings.ollama.base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return "# Local Models\n\nOllama not reachable.\n"

    models = data.get("models", [])
    if not models:
        return "# Local Models\n\nNo models installed.\n"

    lines = ["# Local Models\n", f"{len(models)} models installed:\n"]
    for m in models:
        name = m.get("name", "?")
        size = m.get("size", 0)
        size_gb = size / 1e9 if size else 0
        lines.append(f"- **{name}** ({size_gb:.1f} GB)")
    lines.append("")

    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# 9. security_exclusions — patterns excluded from indexing
# ---------------------------------------------------------------------------

@_register(PackType.SECURITY_EXCLUSIONS, eager=True, ttl=86400)
def gen_security_exclusions(store: PackStore, scope: str) -> str | None:
    """Document current security exclusion patterns."""
    from obsidian_rag.config import _DEFAULT_EXCLUDE_PATTERNS, settings

    lines = ["# Security Exclusions\n"]
    lines.append("## Vault sync exclusions")
    for p in settings.sync.exclude_patterns:
        lines.append(f"- `{p}`")
    lines.append("")

    lines.append("## Default patterns")
    for p in _DEFAULT_EXCLUDE_PATTERNS:
        lines.append(f"- `{p}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10. rag_index_state — collection stats
# ---------------------------------------------------------------------------

@_register(PackType.RAG_INDEX_STATE, eager=True, ttl=3600)
def gen_rag_index_state(store: PackStore, scope: str) -> str | None:
    """Summarise Qdrant collection stats and manifest state."""
    from obsidian_rag.config import settings

    lines = ["# RAG Index State\n"]

    # Manifest stats
    manifest_db = Path(settings.paths.data_dir) / "manifest.db"
    if manifest_db.exists():
        from obsidian_rag.pipeline.manifest import IngestManifest
        m = IngestManifest(manifest_db)
        try:
            with m._lock:
                conn = m._get_conn()
                file_count = conn.execute(_sql("execute_479.sql")).fetchone()[0]
                chunk_count = conn.execute(_sql("execute_480.sql")).fetchone()[0]
                last_run = conn.execute(
                    _sql("execute_482.sql")
                ).fetchone()
            lines.append("## Manifest")
            lines.append(f"- **Files indexed:** {file_count}")
            lines.append(f"- **Chunks tracked:** {chunk_count}")
            if last_run:
                lines.append(f"- **Last run:** {last_run[0]} ({last_run[1]})")
            lines.append("")
        except Exception as e:
            lines.append(f"## Manifest\n- Error reading: {e}\n")
        finally:
            m.close()
    else:
        lines.append('## Manifest\n- Not yet created (call POST /admin/reprocess with target="local")\n')

    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# 11. pending_tasks — TODO/FIXME in codebase
# ---------------------------------------------------------------------------

@_register(PackType.PENDING_TASKS, eager=True, ttl=3600)
def gen_pending_tasks(store: PackStore, scope: str) -> str | None:
    """Scan project source for TODO/FIXME markers."""
    from obsidian_rag.config import PROJECT_ROOT

    src = PROJECT_ROOT / "obsidian_rag"
    if not src.is_dir():
        return None

    tasks: list[str] = []
    for pyfile in sorted(src.rglob("*.py")):
        try:
            for i, line in enumerate(pyfile.read_text(encoding="utf-8").splitlines(), 1):
                low = line.lower()
                if "todo" in low or "fixme" in low or "hack" in low:
                    rel = pyfile.relative_to(PROJECT_ROOT)
                    tasks.append(f"- {rel}:{i} — {line.strip()}")
        except OSError:
            continue

    if not tasks:
        return "# Pending Tasks\n\nNo TODO/FIXME/HACK found in source.\n"

    lines = ["# Pending Tasks\n", f"{len(tasks)} items:\n"]
    lines.extend(tasks[:30])
    if len(tasks) > 30:
        lines.append(f"... and {len(tasks) - 30} more")
    lines.append("")
    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# 12. knowledge_graph_summary — graphify output digest
# ---------------------------------------------------------------------------

@_register(PackType.KNOWLEDGE_GRAPH_SUMMARY, scope="per-repo", eager=True, ttl=3600)
def gen_knowledge_graph_summary(store: PackStore, scope: str) -> str | None:
    """Summarise graphify output for each repo."""
    from obsidian_rag.config import settings

    graph_dir = Path(settings.graphify.output_dir)
    if not graph_dir.is_dir():
        return None

    lines = ["# Knowledge Graph Summary\n"]

    for repo_dir in sorted(graph_dir.iterdir()):
        if not repo_dir.is_dir():
            continue
        name = repo_dir.name

        # Count JSON graph files
        json_files = list(repo_dir.rglob("*.json"))
        md_files = list(repo_dir.rglob("*.md"))

        lines.append(f"## {name}")
        lines.append(f"- **Graph files:** {len(json_files)} JSON, {len(md_files)} MD")

        # Try to read summary if present
        summary_file = repo_dir / "summary.md"
        if summary_file.exists():
            try:
                summary = summary_file.read_text(encoding="utf-8")[:500]
                lines.append(f"- **Summary:** {summary.splitlines()[0] if summary else '(empty)'}")
            except OSError:
                pass
        lines.append("")

    content = "\n".join(lines)
    return _truncate(content) if content.strip() else None


# ---------------------------------------------------------------------------
# Generation API
# ---------------------------------------------------------------------------

def generate_pack(
    store: PackStore,
    spec: PackSpec,
    scope: str | None = None,
    *,
    config_version: str = "",
    model_version: str = "",
    max_tokens: int = 2000,
) -> bool:
    """Generate a single pack and store it. Returns True if stored."""
    from obsidian_rag.config import settings

    effective_scope = scope or spec.default_scope
    ttl = spec.ttl_seconds or settings.cag.default_ttl

    try:
        content = spec.generator(store, effective_scope)
    except Exception as e:
        log.warning("CAG: generator %s failed: %s", spec.pack_type.value, e)
        return False

    if not content:
        return False

    content = _truncate(content, max_tokens)

    store.store_pack(
        pack_type=spec.pack_type.value,
        content=content,
        scope=effective_scope,
        source_hash=compute_source_hash(content),
        config_version=config_version,
        model_version=model_version,
        ttl_seconds=ttl,
    )
    return True


def generate_eager_packs(store: PackStore) -> int:
    """Generate all eager packs. Returns count generated."""
    from obsidian_rag.config import settings

    config_ver = compute_config_version({
        "retrieval": {"top_k": settings.retrieval.top_k,
                      "token_budget": settings.retrieval.token_budget},
        "ollama": {"embedding_model": settings.ollama.embedding_model},
    })
    model_ver = settings.ollama.embedding_model
    max_tokens = settings.cag.max_pack_tokens

    count = 0
    for spec in get_eager_specs():
        try:
            if generate_pack(store, spec, config_version=config_ver,
                             model_version=model_ver, max_tokens=max_tokens):
                count += 1
        except Exception as e:
            log.warning("CAG: eager pack %s failed: %s", spec.pack_type.value, e)

    log.info("CAG: generated %d eager packs", count)
    return count


def get_relevant_packs(
    store: PackStore,
    intent_mode: str,
    query: str,
) -> list[tuple[str, str]]:
    """Return list of (pack_type, content) for packs relevant to this query.

    Freshness is validated by TTL. Returns only non-expired packs.
    Relevance is determined by the intent mode and basic keyword matching.
    """
    from obsidian_rag.config import settings

    if not settings.cag.enabled:
        return []

    packs = store.list_packs()
    now = time.time()
    result: list[tuple[str, str]] = []

    # Always inject: config_environment, rag_index_state
    always_inject = {
        PackType.CONFIG_ENVIRONMENT.value,
        PackType.RAG_INDEX_STATE.value,
    }

    # System-related intents get system packs
    system_packs = {
        PackType.SYSTEM_STATE.value,
        PackType.LOCAL_SERVICES.value,
        PackType.LOCAL_MODELS.value,
    }

    # Code/architecture intents
    code_packs = {
        PackType.PROJECT_ARCHITECTURE.value,
        PackType.REPO_STATE.value,
        PackType.KNOWLEDGE_GRAPH_SUMMARY.value,
        PackType.PENDING_TASKS.value,
    }

    # Determine which packs are relevant based on intent
    query_lower = query.lower()
    relevant_types: set[str] = set(always_inject)

    # Intent-based selection
    system_keywords = {"system", "hardware", "gpu", "cpu", "ram", "memory",
                       "disk", "docker", "service", "ollama", "model"}
    code_keywords = {"code", "function", "class", "module", "architecture",
                     "repo", "git", "project", "structure", "graph"}
    security_keywords = {"security", "exclusion", "exclude", "pattern",
                         "ignore", "sensitive", "segurança"}

    if intent_mode in ("SYSTEM", "SYSTEM_AND_RAG") or any(k in query_lower for k in system_keywords):
        relevant_types |= system_packs

    if intent_mode in ("RAG_ONLY", "RAG_AND_GRAPH", "SYSTEM_AND_RAG") or any(k in query_lower for k in code_keywords):
        relevant_types |= code_packs

    if any(k in query_lower for k in security_keywords):
        relevant_types.add(PackType.SECURITY_EXCLUSIONS.value)

    # Vault-related
    if any(k in query_lower for k in {"vault", "note", "obsidian", "notas"}):
        relevant_types.add(PackType.VAULT_SUMMARY.value)

    # Error-related
    if any(k in query_lower for k in {"error", "erro", "bug", "crash", "fail"}):
        relevant_types.add(PackType.RECURRING_ERRORS.value)

    for pack in packs:
        if pack.pack_type not in relevant_types:
            continue
        if now >= pack.expires_at:
            log.debug("CAG: skipping expired pack %s/%s", pack.pack_type, pack.scope)
            continue
        result.append((pack.pack_type, pack.content))

    return result


# ---------------------------------------------------------------------------
# Shell helpers (safe, timeout-bounded)
# ---------------------------------------------------------------------------

def _git_cmd(repo: Path, *args: str) -> str:
    """Run a git command in a repo directory, return stdout or empty."""
    return _run_cmd("git", "-C", str(repo), *args)


def _run_cmd(*args: str, timeout: int = 5) -> str:
    """Run a command safely with timeout. Return stdout or empty."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
