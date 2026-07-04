"""Configuração centralizada — carrega config/rag/internal.toml + user.toml com suporte a env overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _find_project_root() -> Path:
    """Locate the obsidian-rag project root."""
    current = Path(__file__).resolve().parent
    # Verify it's an actual project checkout (not a site-packages install)
    if (current / "pyproject.toml").exists():
        return current
    # Installed package (e.g. inside Docker) — use /app (container WORKDIR)
    app_dir = Path("/app")
    if app_dir.is_dir():
        return app_dir
    rag_project_root = os.environ.get("AI_RAG_PROJECT_ROOT")
    if rag_project_root:
        return Path(rag_project_root).expanduser().resolve()
    ai_local_root = os.environ.get("AI_LOCAL_ROOT")
    if ai_local_root:
        return Path(ai_local_root).expanduser() / "obsidian-rag"
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "obsidian-rag"
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current


def _find_config_dir() -> Path:
    """Locate the config/rag/ directory.

    Search order:
      1. AI_RAG_SETTINGS_DIR env var
      2. <workspace_root>/config/rag
      3. $AI_LOCAL_ROOT/config/rag
      4. ./config/rag from the current working tree
    """
    env_dir = os.environ.get("AI_RAG_SETTINGS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.is_dir():
            return p

    here = Path(__file__).resolve()
    for parent in here.parents:
        if not (parent / "config" / "main.yaml").exists():
            continue
        candidate = parent / "config" / "rag"
        if candidate.is_dir():
            return candidate

    for parent in here.parents:
        candidate = parent / "config" / "rag"
        if candidate.is_dir():
            return candidate

    ai_local_root = os.environ.get("AI_LOCAL_ROOT")
    if ai_local_root:
        candidate = Path(ai_local_root).expanduser() / "config" / "rag"
        if candidate.is_dir():
            return candidate

    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "config" / "rag"
        if candidate.is_dir():
            return candidate

    return cwd / "config" / "rag"


PROJECT_ROOT = _find_project_root()
CONFIG_DIR = _find_config_dir()


def _find_ai_local_root() -> Path | None:
    env_root = os.environ.get("AI_LOCAL_ROOT") or os.environ.get("AI_LOCAL_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "main.yaml").is_file():
            return parent
    return None


def _rag_storage_root() -> Path:
    if PROJECT_ROOT == Path("/app"):
        return PROJECT_ROOT / "data"
    explicit = os.environ.get("RAG_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    storage_root = os.environ.get("AI_LOCAL_STORAGE_ROOT")
    if storage_root:
        return Path(storage_root).expanduser().resolve() / "data" / "rag"
    ai_local_root = _find_ai_local_root()
    if ai_local_root is not None:
        return ai_local_root / ".local" / "data" / "rag"
    return PROJECT_ROOT / "data"


def _graphify_storage_root() -> Path:
    if PROJECT_ROOT == Path("/app"):
        return PROJECT_ROOT / "data" / "graphify"
    explicit = os.environ.get("GRAPHIFY_OUT_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    storage_root = os.environ.get("AI_LOCAL_STORAGE_ROOT")
    if storage_root:
        return Path(storage_root).expanduser().resolve() / "data" / "graphify"
    ai_local_root = _find_ai_local_root()
    if ai_local_root is not None:
        return ai_local_root / ".local" / "data" / "graphify"
    return PROJECT_ROOT / "data" / "graphify"


_RAG_PATH_DEFAULTS = {
    "data_dir": str(_rag_storage_root() / "qdrant"),
    "vault_dir": str(_rag_storage_root() / "vault"),
}

_RAG_RETRIEVAL_DEFAULTS = {
    "top_k": 10,
    "score_threshold": 0.45,
    "dynamic_threshold_ratio": 0.75,
}

_RAG_API_DEFAULTS = {
    "query_top_k": 10,
    "rate_limit": 60,
    "chat_rate_limit": 20,
}

_RAG_RERANKER_DEFAULTS = {
    "top_k_candidates": 30,
}

_RAG_GRAPHIFY_DEFAULTS = {
    "output_dir": str(_graphify_storage_root()),
    "graph_vault_dir": str(_rag_storage_root() / "graphify-vault"),
    "query_backend": "json",
    "candidate_score_threshold": 0.4,
    "allowed_node_types_file": "",
    "allowed_relation_types_file": "",
    "falkor_host": "localhost",
    "falkor_port": 6379,
    "falkor_graph": "obsidian_rag",
}

_RAG_PERFORMANCE_DEFAULTS = {
    "pause_memory_percent": 80,
    "abort_memory_percent": 90,
    "max_swap_percent": 40,
    "pause_swap_percent": 60,
    "abort_swap_percent": 80,
}

_RAG_CAG_DEFAULTS = {
    "db_path": "cag.db",
    "default_ttl": 3600,
    "system_ttl": 300,
    "response_cache_ttl": 600,
}

_RAG_OBSERVABILITY_DEFAULTS = {
    "batch_size": 500,
    "flush_interval_seconds": 2.0,
    "retention_days": 90,
    "resource_sample_interval": 5.0,
}

_RAG_WORKFLOWS_DEFAULTS = {
    "backend": "temporal",
    "job_store_path": str(_rag_storage_root() / "workflows" / "admin_jobs.json"),
    "temporal_address": "localhost:7233",
    "temporal_namespace": "default",
    "temporal_task_queue": "rag-reprocess",
    "temporal_workflow_timeout_seconds": 7200,
}

_RAG_DEBUG_DEFAULTS = {
    "log_to_file": False,
}


def _auto_discover_secrets():
    """Auto-discover infra/docker/secrets/ dir for local CLI usage.

    When running outside Docker (no /run/secrets/), populate *_FILE env vars
    pointing to the workspace's infra/docker/secrets/ directory. This keeps the
    single source of truth in infra/docker/secrets/ without hardcoding values.
    """
    if Path("/run/secrets").is_dir():
        return  # Inside Docker — secrets mounted natively

    workspace_root = Path(__file__).resolve().parent.parent.parent
    secrets_dir = workspace_root / "infra" / "docker" / "secrets"
    if not secrets_dir.is_dir():
        return

    # Map: env var suffix → secret file name
    mappings = {
        "RAG_STORE_QDRANT_API_KEY_FILE": "qdrant_api_key",
        "RAG_API_API_KEY_FILE": "rag_api_key",
        "OLLAMA_API_KEY_FILE": "ollama_api_key",
        "RAG_SYNC_EXTRATOR_API_KEY_FILE": "internal_api_key",
        "RAG_SYNC_AUDIO_TRANSCRIBE_API_KEY_FILE": "audio_transcribe_api_key",
        "RAG_SYNC_LIFECYCLE_API_KEY_FILE": "orc_api_key",
        "AI_RESOURCE_GOVERNOR_TOKEN_FILE": "internal_api_key",
    }
    for env_var, filename in mappings.items():
        if not os.environ.get(env_var):
            secret_file = secrets_dir / filename
            if secret_file.is_file():
                os.environ[env_var] = str(secret_file)

    # Also set direct env vars for tools that read them without _FILE convention
    # (e.g. graphify/litellm reads OLLAMA_API_KEY directly)
    direct_mappings = {
        "OLLAMA_API_KEY": "ollama_api_key",
    }
    for env_var, filename in direct_mappings.items():
        if not os.environ.get(env_var):
            secret_file = secrets_dir / filename
            if secret_file.is_file():
                os.environ[env_var] = secret_file.read_text().strip()


_auto_discover_secrets()


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base; override wins at every key level."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_toml() -> dict:
    """Load config by merging config/rag/internal.toml + user.toml.

    Priority (highest to lowest): env vars > user.toml > internal.toml > code defaults.
    """
    internal_path = CONFIG_DIR / "internal.toml"
    user_path = CONFIG_DIR / "user.toml"

    base: dict = {}
    if internal_path.exists():
        with open(internal_path, "rb") as f:
            base = tomllib.load(f)

    if user_path.exists():
        with open(user_path, "rb") as f:
            user = tomllib.load(f)
        return _deep_merge(base, user)

    if base:
        return base

    raise FileNotFoundError(
        f"Config não encontrado em {CONFIG_DIR}. "
        "Cria config/rag/user.toml antes de iniciar o serviço."
    )


def _env_override(section: str, key: str, default):
    """Check for env var RAG_{SECTION}_{KEY} (uppercase).

    Also supports Docker secrets convention: if RAG_{SECTION}_{KEY}_FILE
    is set, reads the secret from that file path.
    """
    env_key = f"RAG_{section.upper()}_{key.upper()}"
    # Docker secrets: _FILE convention takes priority
    file_env = f"{env_key}_FILE"
    file_path = os.environ.get(file_env)
    if file_path:
        p = Path(file_path)
        if p.is_file():
            return p.read_text().strip()
    val = os.environ.get(env_key)
    if val is None:
        return default
    # Coerce to same type as default
    if isinstance(default, bool):
        return val.lower() in ("true", "1", "yes")
    if isinstance(default, int):
        return int(val)
    if isinstance(default, float):
        return float(val)
    return val


def _env_configured(section: str, key: str) -> bool:
    env_key = f"RAG_{section.upper()}_{key.upper()}"
    return env_key in os.environ or f"{env_key}_FILE" in os.environ


def _resolve_path(raw: str) -> Path:
    """Resolve ~ and relative paths (relative to PROJECT_ROOT)."""
    raw_text = str(raw)
    host_home = os.environ.get("AI_RAG_HOST_HOME")
    if host_home and raw_text in {"~", "~/"}:
        return Path(host_home).resolve()
    if host_home and raw_text.startswith("~/"):
        host_home_path = Path(host_home)
        if host_home_path.is_dir():
            return (host_home_path / raw_text[2:]).resolve()
    if raw_text == "data" or raw_text.startswith("data/"):
        suffix = raw_text[5:] if raw_text.startswith("data/") else ""
        return (_rag_storage_root() / suffix).resolve()
    p = Path(os.path.expanduser(raw_text))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _require_https_url(value: str, field_name: str) -> str:
    clean = (value or "").rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme == "http":
        raise ValueError(f"{field_name} uses insecure HTTP; configure an HTTPS URL")
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{field_name} must not contain credentials, query, or fragment")
    return clean


def _default_qdrant_url() -> str:
    """Local CLI default; Docker gets RAG_STORE_QDRANT_URL from config resolver."""
    return _require_https_url(os.environ.get("ORC_SERVICES_QDRANT_HTTP_URL") or "https://localhost:16333", "qdrant.url")


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path
    vault_dir: Path
    vault_dirs: tuple[Path, ...]  # multi-vault support (includes vault_dir)


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    embedding_model: str


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int
    overlap_chars: int
    min_chars: int
    strip_frontmatter: bool
    contextual_prefix: bool


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int
    score_threshold: float
    dynamic_threshold_ratio: float
    embedding_cache_size: int
    embedding_cache_persistent: bool  # reuse embeddings across runs via on-disk cache
    context_mode: str              # "auto" | "rag_only" | "graph_only" | "both" | "none"
    token_budget: int              # max tokens estimados no contexto
    graph_max_neighbors: int       # vizinhos por nó no graph context
    graph_max_communities: int     # max comunidades a injectar
    semantic_dedup_threshold: float  # cosine threshold for semantic deduplication (0–1)


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    query_top_k: int
    api_key: str
    rate_limit: int          # requests per minute (0 = disabled)
    chat_rate_limit: int     # /chat requests per minute


@dataclass(frozen=True)
class RepoChunkingConfig:
    strategy: str       # "ast" | "text"
    max_chars: int
    overlap_chars: int
    min_chars: int
    contextual_prefix: bool


@dataclass(frozen=True)
class ReposConfig:
    paths: tuple[Path, ...]   # Git repos or plain folders to index
    collection_name: str      # coleção Qdrant separada
    chunking: RepoChunkingConfig


@dataclass(frozen=True)
class GraphifyConfig:
    enabled: bool
    backend: str        # "ollama" | "gemini" | "claude" | "openai"
    model: str          # modelo LLM; "" = usar default do backend
    output_dir: Path
    graph_vault_dir: Path
    auto_update: bool
    query_backend: str = "json"  # "json" | "falkordb"
    import_on_build: bool = True
    falkor_host: str = "localhost"
    falkor_port: int = 6379
    falkor_graph: str = "obsidian_rag"
    falkor_username: str = ""
    falkor_password: str = ""
    falkor_ssl: bool = False
    extract_mode: str = ""          # "" = default, "deep" = aggressive INFERRED-edge semantic
    max_concurrency: int = 1        # parallel semantic chunks in flight (1 for local LLMs)
    token_budget: int = 8000        # per semantic chunk token cap
    api_timeout: int = 0            # per LLM request timeout; 0 derives from performance.enrich_timeout
    # --- Incremental processing ---
    extraction_cache_db: str = "data/graph/extraction_cache.db"
    mtime_shortcircuit: bool = True
    # --- Pre-filter ---
    prefilter_enabled: bool = True
    candidate_score_threshold: float = 0.4
    prefilter_min_chars: int = 200
    prefilter_max_llm_chunks_per_doc: int = 20
    # --- Schema lock ---
    schema_locked: bool = False
    allowed_node_types_file: str = ""
    allowed_relation_types_file: str = ""
    # --- Skip patterns ---
    skip_patterns: tuple[str, ...] = ()
    # --- Community summaries ---
    community_min_members: int = 5
    community_max_workers: int = 3
    community_incremental: bool = True
    # --- Incremental export ---
    export_incremental: bool = True


@dataclass(frozen=True)
class RouterConfig:
    enabled: bool           # use LLM router or keyword-only heuristic
    model: str              # fast model for classification
    timeout: float          # max seconds for LLM call


@dataclass(frozen=True)
class RerankerConfig:
    enabled: bool
    model: str                  # LLM model (Ollama)
    cross_encoder_model: str    # sentence-transformers cross-encoder model
    top_k_candidates: int       # how many candidates to evaluate
    min_score: float            # minimum reranker score


@dataclass(frozen=True)
class ContextPolicyConfig:
    min_relevance_score: float   # min best-chunk score to accept context
    min_relevant_chunks: int     # min chunks above threshold
    log_weak_context: bool


@dataclass(frozen=True)
class DebugConfig:
    enabled: bool           # show router decisions in output
    log_to_file: bool       # log to obsidian_rag.log
    log_level: str          # DEBUG | INFO | WARNING
    log_format: str         # "text" | "json"


# Default patterns excluded from vault sync/scan.
# Aggressive skip rules: caches, VCS, build artefacts, binary blobs, media,
# vector-store internals and on-disk model weights must never be indexed.
_DEFAULT_EXCLUDE_PATTERNS = (
    # VCS / editor / OS
    ".obsidian", ".trash", ".git", ".DS_Store", "Thumbs.db",
    # Python / JS envs and caches
    "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    # Build / output dirs
    "dist", "build", "target", "outputs", "logs",
    # Vector-store internals
    ".qdrant", "qdrant_storage", "chroma",
    # Embedded DBs
    "*.db", "*.sqlite", "*.sqlite3",
    # Columnar / array dumps
    "*.parquet", "*.arrow", "*.npy", "*.npz",
    # Model weights / binaries
    "*.pt", "*.safetensors", "*.gguf", "*.bin",
    # Media
    "*.mp3", "*.wav", "*.mp4", "*.mkv", "*.avi",
)


@dataclass(frozen=True)
class FileLimitsConfig:
    """Hard limits applied during scanning/chunking to bound work per file."""
    max_file_size_mb_text: int = 50
    max_file_size_mb_pdf: int = 200
    max_chunks_per_file: int = 2000
    min_chunk_chars: int = 200
    max_chunk_tokens: int = 900


@dataclass(frozen=True)
class SyncConfig:
    backend: str = "direct"         # only "direct" is supported
    exclude_patterns: tuple[str, ...] = _DEFAULT_EXCLUDE_PATTERNS
    limits: FileLimitsConfig = field(default_factory=FileLimitsConfig)
    auto_reprocess: bool = False
    watch_interval_seconds: int = 60
    startup_delay_seconds: int = 10
    idle_after_changes_seconds: int = 5
    agent_wakeup_enabled: bool = True
    lifecycle_url: str = ""
    lifecycle_api_key_file: str = ""
    lifecycle_start_timeout_seconds: int = 60
    extrator_url: str = ""
    extrator_api_key_file: str = ""
    extrator_timeout_seconds: int = 120
    audio_transcribe_url: str = ""
    audio_transcribe_api_key_file: str = ""
    audio_transcribe_output_dir: str = ""
    audio_transcribe_timeout_seconds: int = 30


@dataclass(frozen=True)
class StoreConfig:
    backend: str             # "qdrant"
    qdrant_url: str          # Qdrant server URL — required (e.g. "https://localhost:6333")
    qdrant_api_key: str      # Qdrant Cloud API key (empty = none)
    # --- Vector index tuning ---
    on_disk: bool = True              # store full-precision vectors on disk (saves RAM)
    hnsw_m: int = 16                  # HNSW graph degree (final value after ingest)
    hnsw_ef_construct: int = 100      # HNSW build-time search width
    defer_hnsw_on_bulk: bool = True   # build HNSW with m=0 during bulk load, finalize after
    # --- Bulk ingestion ---
    bulk_upload_threshold: int = 10000  # use upload_collection above this many new points
    bulk_upload_parallel: int = 4       # parallel upload workers for bulk path
    # --- Payload ---
    externalize_text: bool = False    # keep chunk text out of Qdrant payload (store ref only)


@dataclass(frozen=True)
class PipelineConfig:
    engine: str = "local"   # "local" (ProcessPoolExecutor) | "dask" (Dask distributed)
    dask_scheduler: str = ""  # Dask scheduler address (empty = local cluster)


@dataclass(frozen=True)
class PerformanceConfig:
    auto_tune: bool              # auto-detect resources and override limits
    max_cpu_percent: int         # throttle sync when CPU% exceeds this
    max_memory_percent: int      # throttle sync when RAM% exceeds this
    max_parallel_jobs: int       # effective cap on workers
    embedding_batch_size: int    # batch size for embedding calls
    embedding_timeout: int       # max seconds for embedding HTTP calls
    query_timeout_seconds: int   # max seconds for a single query
    graph_timeout: int = 600     # max seconds for a single graphify subprocess
    enrich_timeout: int = 180     # max seconds for LLM calls in graph enrichment
    pipeline_timeout: int = 3600  # max seconds for embedding pipeline watchdog (default 1h)
    graph_parallel_jobs: int = 1  # parallel graphify subprocesses (1 = sequential)
    # --- Bounded pipeline fields ---
    parser_workers: int = 3              # concurrent file-parsing processes
    embedding_batch_max_chars: int = 48000  # close embedding batch when total chars exceed this
    chunks_queue_max: int = 128          # max pending chunks between parser and embedder
    files_queue_max: int = 256           # max pending files between scanner and parser
    pause_memory_percent: int = 80       # pause pipeline when RAM% exceeds this
    abort_memory_percent: int = 90       # abort pipeline when RAM% exceeds this
    # --- Swap protection ---
    max_swap_percent: int = 40           # reduce when swap% exceeds this
    pause_swap_percent: int = 60         # pause when swap% exceeds this
    abort_swap_percent: int = 80         # abort when swap% exceeds this
    # --- Embedding parallelism ---
    embedding_concurrency: int = 1       # concurrent embedding threads (max 3 for local Ollama)
    # --- Manifest batching ---
    manifest_batch_size: int = 50        # records to accumulate before flush


@dataclass(frozen=True)
class CagConfig:
    enabled: bool = True
    db_path: str = "data/cag.db"
    default_ttl: int = 3600           # 1 hour — eager packs
    system_ttl: int = 300             # 5 min — live state packs
    response_cache_enabled: bool = False
    response_cache_ttl: int = 600     # 10 min
    max_pack_tokens: int = 2000       # token budget cap per pack
    generate_on_sync: bool = True     # trigger eager packs after sync


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool = False
    clickhouse_url: str = "https://localhost:8123"
    clickhouse_database: str = "obsidian_rag"
    clickhouse_username: str = "default"
    clickhouse_password_env: str = "CLICKHOUSE_PASSWORD"
    batch_size: int = 500
    flush_interval_seconds: float = 2.0
    queue_max_size: int = 10_000
    retention_days: int = 90
    fail_silent: bool = True
    resource_sampling: bool = True
    resource_sample_interval: float = 5.0


@dataclass(frozen=True)
class WebhookConfig:
    urls: tuple[str, ...] = ()        # URLs to notify on sync_complete
    timeout: int = 5                  # HTTP timeout for webhook calls


@dataclass(frozen=True)
class WorkflowsConfig:
    backend: str = "direct"           # "direct" | "temporal"
    job_store_path: Path = Path("data/workflows/admin_jobs.json")
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "rag-reprocess"
    temporal_workflow_timeout_seconds: int = 7200


@dataclass(frozen=True)
class Settings:
    paths: PathsConfig
    ollama: OllamaConfig
    chunking: ChunkingConfig
    retrieval: RetrievalConfig
    api: ApiConfig
    repos: ReposConfig
    graphify: GraphifyConfig
    router: RouterConfig
    reranker: RerankerConfig
    context_policy: ContextPolicyConfig
    debug: DebugConfig
    store: StoreConfig
    pipeline: PipelineConfig
    performance: PerformanceConfig
    sync: SyncConfig
    cag: CagConfig = field(default_factory=CagConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    workflows: WorkflowsConfig = field(default_factory=WorkflowsConfig)


def load_settings() -> Settings:
    """Load settings from rag.internal.toml + rag.user.toml with env var overrides."""
    raw = _load_toml()

    p = raw.get("paths", {})
    vault_dir_configured = "vault_dir" in p or _env_configured("paths", "vault_dir")
    vault_dir = _resolve_path(
        _env_override("paths", "vault_dir", p.get("vault_dir", _RAG_PATH_DEFAULTS["vault_dir"]))
    )

    # Multi-vault: vault_dirs list (if present) takes precedence;
    # otherwise fall back to [vault_dir] for backward compat.
    vault_dirs_configured = "vault_dirs" in p or _env_configured("paths", "vault_dirs")
    raw_vault_dirs = _env_override("paths", "vault_dirs", p.get("vault_dirs", []))
    if isinstance(raw_vault_dirs, str):
        raw_vault_dirs = [vd.strip() for vd in raw_vault_dirs.split(",") if vd.strip()]
    if raw_vault_dirs:
        vault_dirs = tuple(_resolve_path(vd) for vd in raw_vault_dirs)
    elif vault_dirs_configured:
        vault_dirs = ()
    elif vault_dir_configured:
        vault_dirs = (vault_dir,)
    else:
        vault_dirs = ()

    paths = PathsConfig(
        data_dir=_resolve_path(_env_override("paths", "data_dir", p.get("data_dir", _RAG_PATH_DEFAULTS["data_dir"]))),
        vault_dir=vault_dir,
        vault_dirs=vault_dirs,
    )

    from registry import get_rag_model

    def _model_setting(section: str, key: str, values: dict, role: str):
        env_value = _env_override(section, key, None)
        if env_value is not None:
            return env_value
        if key in values:
            return values[key]
        return get_rag_model(role)

    o = raw.get("ollama", {})
    ollama = OllamaConfig(
        base_url=_require_https_url(
            _env_override("ollama", "base_url", o.get("base_url", "https://localhost:11434")),
            "ollama.base_url",
        ),
        embedding_model=_model_setting("ollama", "embedding_model", o, "embedding"),
    )

    c = raw.get("chunking", {})
    chunking = ChunkingConfig(
        max_chars=_env_override("chunking", "max_chars", c.get("max_chars", 2000)),
        overlap_chars=_env_override("chunking", "overlap_chars", c.get("overlap_chars", 200)),
        min_chars=_env_override("chunking", "min_chars", c.get("min_chars", 50)),
        strip_frontmatter=_env_override("chunking", "strip_frontmatter", c.get("strip_frontmatter", True)),
        contextual_prefix=_env_override("chunking", "contextual_prefix", c.get("contextual_prefix", True)),
    )

    r = raw.get("retrieval", {})
    retrieval = RetrievalConfig(
        top_k=_env_override("retrieval", "top_k", r.get("top_k", _RAG_RETRIEVAL_DEFAULTS["top_k"])),
        score_threshold=_env_override(
            "retrieval",
            "score_threshold",
            r.get("score_threshold", _RAG_RETRIEVAL_DEFAULTS["score_threshold"]),
        ),
        dynamic_threshold_ratio=_env_override(
            "retrieval",
            "dynamic_threshold_ratio",
            r.get("dynamic_threshold_ratio", _RAG_RETRIEVAL_DEFAULTS["dynamic_threshold_ratio"]),
        ),
        embedding_cache_size=_env_override("retrieval", "embedding_cache_size", r.get("embedding_cache_size", 1024)),
        embedding_cache_persistent=_env_override("retrieval", "embedding_cache_persistent", r.get("embedding_cache_persistent", True)),
        context_mode=_env_override("retrieval", "context_mode", r.get("context_mode", "auto")),
        token_budget=_env_override("retrieval", "token_budget", r.get("token_budget", 4000)),
        graph_max_neighbors=_env_override("retrieval", "graph_max_neighbors", r.get("graph_max_neighbors", 5)),
        graph_max_communities=_env_override("retrieval", "graph_max_communities", r.get("graph_max_communities", 3)),
        semantic_dedup_threshold=_env_override("retrieval", "semantic_dedup_threshold", r.get("semantic_dedup_threshold", 0.95)),
    )

    a = raw.get("api", {})
    api = ApiConfig(
        host=_env_override("api", "host", a.get("host", "127.0.0.1")),
        port=_env_override("api", "port", a.get("port", 8484)),
        query_top_k=_env_override("api", "query_top_k", a.get("query_top_k", _RAG_API_DEFAULTS["query_top_k"])),
        api_key=_env_override("api", "api_key", a.get("api_key", "")),
        rate_limit=_env_override("api", "rate_limit", a.get("rate_limit", _RAG_API_DEFAULTS["rate_limit"])),
        chat_rate_limit=_env_override(
            "api",
            "chat_rate_limit",
            a.get("chat_rate_limit", _RAG_API_DEFAULTS["chat_rate_limit"]),
        ),
    )

    rp = raw.get("repos", {})
    rc = rp.get("chunking", {})
    repo_chunking = RepoChunkingConfig(
        strategy=_env_override("repos", "strategy", rc.get("strategy", "ast")),
        max_chars=_env_override("repos", "max_chars", rc.get("max_chars", 2000)),
        overlap_chars=_env_override("repos", "overlap_chars", rc.get("overlap_chars", 200)),
        min_chars=_env_override("repos", "min_chars", rc.get("min_chars", 80)),
        contextual_prefix=_env_override("repos", "contextual_prefix", rc.get("contextual_prefix", True)),
    )
    raw_repo_paths = _env_override("repos", "paths", rp.get("paths", []))
    if isinstance(raw_repo_paths, str):
        raw_repo_paths = [p.strip() for p in raw_repo_paths.split(",") if p.strip()]
    repos = ReposConfig(
        paths=tuple(_resolve_path(p) for p in raw_repo_paths),
        collection_name=_env_override("repos", "collection_name", rp.get("collection_name", "code_repos")),
        chunking=repo_chunking,
    )

    gf = raw.get("graphify", {})
    raw_skip_patterns = _env_override("graphify", "skip_patterns", gf.get("skip_patterns", []))
    if isinstance(raw_skip_patterns, str):
        raw_skip_patterns = [p.strip() for p in raw_skip_patterns.split(",") if p.strip()]
    graphify = GraphifyConfig(
        enabled=_env_override("graphify", "enabled", gf.get("enabled", False)),
        backend=_env_override("graphify", "backend", gf.get("backend", "ollama")),
        model=_model_setting("graphify", "model", gf, "graph-enrichment"),
        output_dir=_resolve_path(
            _env_override("graphify", "output_dir", gf.get("output_dir", _RAG_GRAPHIFY_DEFAULTS["output_dir"]))
        ),
        graph_vault_dir=_resolve_path(
            _env_override(
                "graphify",
                "graph_vault_dir",
                gf.get("graph_vault_dir", _RAG_GRAPHIFY_DEFAULTS["graph_vault_dir"]),
            )
        ),
        auto_update=_env_override("graphify", "auto_update", gf.get("auto_update", False)),
        query_backend=str(_env_override(
            "graphify",
            "query_backend",
            gf.get("query_backend", _RAG_GRAPHIFY_DEFAULTS["query_backend"]),
        )).strip().lower(),
        import_on_build=_env_override("graphify", "import_on_build", gf.get("import_on_build", True)),
        falkor_host=_env_override("graphify", "falkor_host", gf.get("falkor_host", _RAG_GRAPHIFY_DEFAULTS["falkor_host"])),
        falkor_port=_env_override("graphify", "falkor_port", gf.get("falkor_port", _RAG_GRAPHIFY_DEFAULTS["falkor_port"])),
        falkor_graph=_env_override("graphify", "falkor_graph", gf.get("falkor_graph", _RAG_GRAPHIFY_DEFAULTS["falkor_graph"])),
        falkor_username=_env_override("graphify", "falkor_username", gf.get("falkor_username", "")),
        falkor_password=_env_override("graphify", "falkor_password", gf.get("falkor_password", "")),
        falkor_ssl=_env_override("graphify", "falkor_ssl", gf.get("falkor_ssl", False)),
        extract_mode=_env_override("graphify", "extract_mode", gf.get("extract_mode", "")),
        max_concurrency=_env_override("graphify", "max_concurrency", gf.get("max_concurrency", 1)),
        token_budget=_env_override("graphify", "token_budget", gf.get("token_budget", 8000)),
        api_timeout=_env_override("graphify", "api_timeout", gf.get("api_timeout", 0)),
        extraction_cache_db=str(
            _resolve_path(
                _env_override(
                    "graphify",
                    "extraction_cache_db",
                    gf.get("extraction_cache_db", "data/graph/extraction_cache.db"),
                )
            )
        ),
        mtime_shortcircuit=_env_override("graphify", "mtime_shortcircuit", gf.get("mtime_shortcircuit", True)),
        prefilter_enabled=_env_override("graphify", "prefilter_enabled", gf.get("prefilter_enabled", True)),
        candidate_score_threshold=_env_override(
            "graphify",
            "candidate_score_threshold",
            gf.get("candidate_score_threshold", _RAG_GRAPHIFY_DEFAULTS["candidate_score_threshold"]),
        ),
        prefilter_min_chars=_env_override("graphify", "prefilter_min_chars", gf.get("prefilter_min_chars", 200)),
        prefilter_max_llm_chunks_per_doc=_env_override("graphify", "prefilter_max_llm_chunks_per_doc", gf.get("prefilter_max_llm_chunks_per_doc", 20)),
        schema_locked=_env_override("graphify", "schema_locked", gf.get("schema_locked", False)),
        allowed_node_types_file=_env_override(
            "graphify",
            "allowed_node_types_file",
            gf.get("allowed_node_types_file", _RAG_GRAPHIFY_DEFAULTS["allowed_node_types_file"]),
        ),
        allowed_relation_types_file=_env_override(
            "graphify",
            "allowed_relation_types_file",
            gf.get("allowed_relation_types_file", _RAG_GRAPHIFY_DEFAULTS["allowed_relation_types_file"]),
        ),
        skip_patterns=tuple(raw_skip_patterns),
        community_min_members=_env_override("graphify", "community_min_members", gf.get("community_min_members", 5)),
        community_max_workers=_env_override("graphify", "community_max_workers", gf.get("community_max_workers", 3)),
        community_incremental=_env_override("graphify", "community_incremental", gf.get("community_incremental", True)),
        export_incremental=_env_override("graphify", "export_incremental", gf.get("export_incremental", True)),
    )

    rt = raw.get("router", {})
    router = RouterConfig(
        enabled=_env_override("router", "enabled", rt.get("enabled", True)),
        model=_model_setting("router", "model", rt, "router"),
        timeout=_env_override("router", "timeout", rt.get("timeout", 15.0)),
    )

    rr = raw.get("reranker", {})
    reranker = RerankerConfig(
        enabled=_env_override("reranker", "enabled", rr.get("enabled", False)),
        model=_model_setting("reranker", "model", rr, "reranker"),
        cross_encoder_model=_env_override("reranker", "cross_encoder_model", rr.get("cross_encoder_model", "BAAI/bge-reranker-v2-m3")),
        top_k_candidates=_env_override(
            "reranker",
            "top_k_candidates",
            rr.get("top_k_candidates", _RAG_RERANKER_DEFAULTS["top_k_candidates"]),
        ),
        min_score=_env_override("reranker", "min_score", rr.get("min_score", 0.3)),
    )

    cp = raw.get("context_policy", {})
    context_policy = ContextPolicyConfig(
        min_relevance_score=_env_override("context_policy", "min_relevance_score", cp.get("min_relevance_score", 0.50)),
        min_relevant_chunks=_env_override("context_policy", "min_relevant_chunks", cp.get("min_relevant_chunks", 1)),
        log_weak_context=_env_override("context_policy", "log_weak_context", cp.get("log_weak_context", True)),
    )

    db = raw.get("debug", {})
    debug = DebugConfig(
        enabled=_env_override("debug", "enabled", db.get("enabled", False)),
        log_to_file=_env_override("debug", "log_to_file", db.get("log_to_file", _RAG_DEBUG_DEFAULTS["log_to_file"])),
        log_level=_env_override("debug", "log_level", db.get("log_level", "INFO")),
        log_format=_env_override("debug", "log_format", db.get("log_format", "text")),
    )

    st = raw.get("store", {})
    store = StoreConfig(
        backend=_env_override("store", "backend", st.get("backend", "qdrant")),
        qdrant_url=_env_override("store", "qdrant_url", st.get("qdrant_url", _default_qdrant_url())),
        qdrant_api_key=_env_override("store", "qdrant_api_key", st.get("qdrant_api_key", "")),
        on_disk=_env_override("store", "on_disk", st.get("on_disk", True)),
        hnsw_m=_env_override("store", "hnsw_m", st.get("hnsw_m", 16)),
        hnsw_ef_construct=_env_override("store", "hnsw_ef_construct", st.get("hnsw_ef_construct", 100)),
        defer_hnsw_on_bulk=_env_override("store", "defer_hnsw_on_bulk", st.get("defer_hnsw_on_bulk", True)),
        bulk_upload_threshold=_env_override("store", "bulk_upload_threshold", st.get("bulk_upload_threshold", 10000)),
        bulk_upload_parallel=_env_override("store", "bulk_upload_parallel", st.get("bulk_upload_parallel", 4)),
        externalize_text=_env_override("store", "externalize_text", st.get("externalize_text", False)),
    )

    pl = raw.get("pipeline", {})
    pipeline = PipelineConfig(
        engine=_env_override("pipeline", "engine", pl.get("engine", "local")),
        dask_scheduler=_env_override("pipeline", "dask_scheduler", pl.get("dask_scheduler", "")),
    )

    # Sync — optional section, defaults to backend="direct" (cross-platform)
    sy = raw.get("sync", {})
    raw_excludes = _env_override("sync", "exclude_patterns", sy.get("exclude_patterns", list(_DEFAULT_EXCLUDE_PATTERNS)))
    if isinstance(raw_excludes, str):
        raw_excludes = [e.strip() for e in raw_excludes.split(",") if e.strip()]
    sync = SyncConfig(
        backend="direct",
        exclude_patterns=tuple(raw_excludes),
        limits=FileLimitsConfig(
            max_file_size_mb_text=_env_override("sync", "max_file_size_mb_text", sy.get("max_file_size_mb_text", 50)),
            max_file_size_mb_pdf=_env_override("sync", "max_file_size_mb_pdf", sy.get("max_file_size_mb_pdf", 200)),
            max_chunks_per_file=_env_override("sync", "max_chunks_per_file", sy.get("max_chunks_per_file", 2000)),
            min_chunk_chars=_env_override("sync", "min_chunk_chars", sy.get("min_chunk_chars", 200)),
            max_chunk_tokens=_env_override("sync", "max_chunk_tokens", sy.get("max_chunk_tokens", 900)),
        ),
        auto_reprocess=_env_override("sync", "auto_reprocess", sy.get("auto_reprocess", False)),
        watch_interval_seconds=_env_override("sync", "watch_interval_seconds", sy.get("watch_interval_seconds", 60)),
        startup_delay_seconds=_env_override("sync", "startup_delay_seconds", sy.get("startup_delay_seconds", 10)),
        idle_after_changes_seconds=_env_override("sync", "idle_after_changes_seconds", sy.get("idle_after_changes_seconds", 5)),
        agent_wakeup_enabled=_env_override("sync", "agent_wakeup_enabled", sy.get("agent_wakeup_enabled", True)),
        lifecycle_url=str(
            _env_override(
                "sync",
                "lifecycle_url",
                sy.get("lifecycle_url", os.environ.get("AI_RESOURCE_GOVERNOR_URL", "")),
            )
        ).rstrip("/"),
        lifecycle_api_key_file=_env_override(
            "sync",
            "lifecycle_api_key_file",
            sy.get("lifecycle_api_key_file", os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN_FILE", "")),
        ),
        lifecycle_start_timeout_seconds=_env_override(
            "sync",
            "lifecycle_start_timeout_seconds",
            sy.get("lifecycle_start_timeout_seconds", 60),
        ),
        extrator_url=str(_env_override("sync", "extrator_url", sy.get("extrator_url", ""))).rstrip("/"),
        extrator_api_key_file=_env_override(
            "sync",
            "extrator_api_key_file",
            sy.get("extrator_api_key_file", ""),
        ),
        extrator_timeout_seconds=_env_override(
            "sync",
            "extrator_timeout_seconds",
            sy.get("extrator_timeout_seconds", 120),
        ),
        audio_transcribe_url=str(_env_override("sync", "audio_transcribe_url", sy.get("audio_transcribe_url", ""))).rstrip("/"),
        audio_transcribe_api_key_file=_env_override(
            "sync",
            "audio_transcribe_api_key_file",
            sy.get("audio_transcribe_api_key_file", ""),
        ),
        audio_transcribe_output_dir=_env_override(
            "sync",
            "audio_transcribe_output_dir",
            sy.get("audio_transcribe_output_dir", ""),
        ),
        audio_transcribe_timeout_seconds=_env_override(
            "sync",
            "audio_transcribe_timeout_seconds",
            sy.get("audio_transcribe_timeout_seconds", 30),
        ),
    )

    pf = raw.get("performance", {})
    performance = PerformanceConfig(
        auto_tune=_env_override("performance", "auto_tune", pf.get("auto_tune", True)),
        max_cpu_percent=_env_override("performance", "max_cpu_percent", pf.get("max_cpu_percent", 75)),
        max_memory_percent=_env_override("performance", "max_memory_percent", pf.get("max_memory_percent", 70)),
        max_parallel_jobs=_env_override("performance", "max_parallel_jobs", pf.get("max_parallel_jobs", 4)),
        embedding_batch_size=_env_override("performance", "embedding_batch_size", pf.get("embedding_batch_size", 30)),
        embedding_timeout=_env_override("performance", "embedding_timeout", pf.get("embedding_timeout", 120)),
        query_timeout_seconds=_env_override("performance", "query_timeout_seconds", pf.get("query_timeout_seconds", 30)),
        graph_timeout=_env_override("performance", "graph_timeout", pf.get("graph_timeout", 600)),
        enrich_timeout=_env_override("performance", "enrich_timeout", pf.get("enrich_timeout", 180)),
        pipeline_timeout=_env_override("performance", "pipeline_timeout", pf.get("pipeline_timeout", 3600)),
        graph_parallel_jobs=_env_override("performance", "graph_parallel_jobs", pf.get("graph_parallel_jobs", 1)),
        parser_workers=_env_override("performance", "parser_workers", pf.get("parser_workers", 1)),
        embedding_batch_max_chars=_env_override("performance", "embedding_batch_max_chars", pf.get("embedding_batch_max_chars", 48000)),
        chunks_queue_max=_env_override("performance", "chunks_queue_max", pf.get("chunks_queue_max", 64)),
        files_queue_max=_env_override("performance", "files_queue_max", pf.get("files_queue_max", 128)),
        pause_memory_percent=_env_override(
            "performance",
            "pause_memory_percent",
            pf.get("pause_memory_percent", _RAG_PERFORMANCE_DEFAULTS["pause_memory_percent"]),
        ),
        abort_memory_percent=_env_override(
            "performance",
            "abort_memory_percent",
            pf.get("abort_memory_percent", _RAG_PERFORMANCE_DEFAULTS["abort_memory_percent"]),
        ),
        max_swap_percent=_env_override(
            "performance",
            "max_swap_percent",
            pf.get("max_swap_percent", _RAG_PERFORMANCE_DEFAULTS["max_swap_percent"]),
        ),
        pause_swap_percent=_env_override(
            "performance",
            "pause_swap_percent",
            pf.get("pause_swap_percent", _RAG_PERFORMANCE_DEFAULTS["pause_swap_percent"]),
        ),
        abort_swap_percent=_env_override(
            "performance",
            "abort_swap_percent",
            pf.get("abort_swap_percent", _RAG_PERFORMANCE_DEFAULTS["abort_swap_percent"]),
        ),
        embedding_concurrency=_env_override("performance", "embedding_concurrency", pf.get("embedding_concurrency", 1)),
        manifest_batch_size=_env_override("performance", "manifest_batch_size", pf.get("manifest_batch_size", 50)),
    )

    # Auto-tune: adjust limits based on detected hardware
    if performance.auto_tune:
        from tuning import auto_tune
        performance = auto_tune(performance)

    # CAG — optional section
    cg = raw.get("cag", {})
    cag_db_raw = _env_override("cag", "db_path", cg.get("db_path", _RAG_CAG_DEFAULTS["db_path"]))
    cag_db_path = str(Path(paths.data_dir) / cag_db_raw) if not os.path.isabs(cag_db_raw) else cag_db_raw
    cag = CagConfig(
        enabled=_env_override("cag", "enabled", cg.get("enabled", True)),
        db_path=cag_db_path,
        default_ttl=_env_override("cag", "default_ttl", cg.get("default_ttl", _RAG_CAG_DEFAULTS["default_ttl"])),
        system_ttl=_env_override("cag", "system_ttl", cg.get("system_ttl", _RAG_CAG_DEFAULTS["system_ttl"])),
        response_cache_enabled=_env_override("cag", "response_cache_enabled", cg.get("response_cache_enabled", False)),
        response_cache_ttl=_env_override(
            "cag",
            "response_cache_ttl",
            cg.get("response_cache_ttl", _RAG_CAG_DEFAULTS["response_cache_ttl"]),
        ),
        max_pack_tokens=_env_override("cag", "max_pack_tokens", cg.get("max_pack_tokens", 2000)),
        generate_on_sync=_env_override("cag", "generate_on_sync", cg.get("generate_on_sync", True)),
    )

    # Webhook — optional section
    wh = raw.get("webhook", {})
    raw_webhook_urls = _env_override("webhook", "urls", wh.get("urls", []))
    if isinstance(raw_webhook_urls, str):
        raw_webhook_urls = [u.strip() for u in raw_webhook_urls.split(",") if u.strip()]
    webhook = WebhookConfig(
        urls=tuple(raw_webhook_urls),
        timeout=_env_override("webhook", "timeout", wh.get("timeout", 5)),
    )

    # Observability — optional section
    ob = raw.get("observability", {})
    observability = ObservabilityConfig(
        enabled=_env_override("observability", "enabled", ob.get("enabled", False)),
        clickhouse_url=_require_https_url(
            _env_override("observability", "clickhouse_url", ob.get("clickhouse_url", "https://localhost:8123")),
            "observability.clickhouse_url",
        ),
        clickhouse_database=_env_override("observability", "clickhouse_database", ob.get("clickhouse_database", "obsidian_rag")),
        clickhouse_username=_env_override("observability", "clickhouse_username", ob.get("clickhouse_username", "default")),
        clickhouse_password_env=_env_override("observability", "clickhouse_password_env", ob.get("clickhouse_password_env", "CLICKHOUSE_PASSWORD")),
        batch_size=_env_override(
            "observability",
            "batch_size",
            ob.get("batch_size", _RAG_OBSERVABILITY_DEFAULTS["batch_size"]),
        ),
        flush_interval_seconds=_env_override(
            "observability",
            "flush_interval_seconds",
            ob.get("flush_interval_seconds", _RAG_OBSERVABILITY_DEFAULTS["flush_interval_seconds"]),
        ),
        queue_max_size=_env_override("observability", "queue_max_size", ob.get("queue_max_size", 10_000)),
        retention_days=_env_override(
            "observability",
            "retention_days",
            ob.get("retention_days", _RAG_OBSERVABILITY_DEFAULTS["retention_days"]),
        ),
        fail_silent=_env_override("observability", "fail_silent", ob.get("fail_silent", True)),
        resource_sampling=_env_override("observability", "resource_sampling", ob.get("resource_sampling", True)),
        resource_sample_interval=_env_override(
            "observability",
            "resource_sample_interval",
            ob.get("resource_sample_interval", _RAG_OBSERVABILITY_DEFAULTS["resource_sample_interval"]),
        ),
    )

    wf = raw.get("workflows", {})
    workflows_backend = str(
        _env_override("workflows", "backend", wf.get("backend", _RAG_WORKFLOWS_DEFAULTS["backend"]))
    ).strip().lower()
    if workflows_backend not in {"direct", "temporal"}:
        raise ValueError("workflows.backend must be one of: direct, temporal")
    workflows = WorkflowsConfig(
        backend=workflows_backend,
        job_store_path=_resolve_path(
            _env_override(
                "workflows",
                "job_store_path",
                wf.get("job_store_path", _RAG_WORKFLOWS_DEFAULTS["job_store_path"]),
            )
        ),
        temporal_address=_env_override(
            "workflows",
            "temporal_address",
            wf.get("temporal_address", _RAG_WORKFLOWS_DEFAULTS["temporal_address"]),
        ),
        temporal_namespace=_env_override(
            "workflows",
            "temporal_namespace",
            wf.get("temporal_namespace", _RAG_WORKFLOWS_DEFAULTS["temporal_namespace"]),
        ),
        temporal_task_queue=_env_override(
            "workflows",
            "temporal_task_queue",
            wf.get("temporal_task_queue", _RAG_WORKFLOWS_DEFAULTS["temporal_task_queue"]),
        ),
        temporal_workflow_timeout_seconds=_env_override(
            "workflows",
            "temporal_workflow_timeout_seconds",
            wf.get(
                "temporal_workflow_timeout_seconds",
                _RAG_WORKFLOWS_DEFAULTS["temporal_workflow_timeout_seconds"],
            ),
        ),
    )

    return Settings(
        paths=paths,
        ollama=ollama,
        chunking=chunking,
        retrieval=retrieval,
        api=api,
        repos=repos,
        graphify=graphify,
        router=router,
        reranker=reranker,
        context_policy=context_policy,
        debug=debug,
        store=store,
        pipeline=pipeline,
        performance=performance,
        sync=sync,
        cag=cag,
        webhook=webhook,
        observability=observability,
        workflows=workflows,
    )


def config_exists() -> bool:
    """Check if config/rag/user.toml exists without loading it."""
    return (CONFIG_DIR / "user.toml").exists()


class _LazySettings:
    """Proxy that defers load_settings() until first attribute access.

    Allows ``from rag_config import settings`` without crashing
    when config/rag/user.toml does not exist yet.
    """

    _instance: Settings | None = None

    def _load(self) -> Settings:
        if self._instance is None:
            self._instance = load_settings()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._load(), name)

    def __repr__(self) -> str:
        if self._instance is None:
            return "<LazySettings: not loaded>"
        return repr(self._instance)


# Module-level singleton — lazy-loaded on first attribute access
settings = _LazySettings()
