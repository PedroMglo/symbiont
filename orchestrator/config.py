"""Configuração centralizada — carrega config/orc/*.toml com suporte a env overrides."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _find_project_root() -> Path:
    """Walk up from this file to find the ai-local project root."""
    current = Path(__file__).resolve().parent.parent
    if current.exists():
        return current
    ai_local_root = os.environ.get("AI_LOCAL_ROOT")
    if ai_local_root:
        return Path(ai_local_root).expanduser()
    default_root = Path.home() / "_projects" / "ai-local"
    if default_root.is_dir():
        return default_root
    return Path.home() / "ai-local"


def _find_config_dir() -> Path:
    """Locate the config/orc/ directory (workspace-level config).

    Search order:
      1. AI_ORC_SETTINGS_DIR env var
      2. <workspace_root>/config/orc
      3. $AI_LOCAL_ROOT/config/orc
      4. ~/_projects/ai-local/config/orc
      5. ~/ai-local/config/orc
    """
    env_dir = os.environ.get("AI_ORC_SETTINGS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.is_dir():
            return p

    # workspace root is typically one level above project root
    workspace_root = Path(__file__).resolve().parent.parent.parent
    candidate = workspace_root / "config" / "orc"
    if candidate.is_dir():
        return candidate

    ai_local_root = os.environ.get("AI_LOCAL_ROOT")
    if ai_local_root:
        candidate = Path(ai_local_root).expanduser() / "config" / "orc"
        if candidate.is_dir():
            return candidate

    default_config = Path.home() / "_projects" / "ai-local" / "config" / "orc"
    if default_config.is_dir():
        return default_config
    return Path.home() / "ai-local" / "config" / "orc"


def _find_security_dir(config_dir: Path) -> Path:
    """Locate the infra/security directory that owns permission policy."""
    env_dir = os.environ.get("AI_SECURITY_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.is_dir():
            return p

    workspace_root = config_dir.parent.parent
    candidate = workspace_root / "infra" / "security"
    if candidate.is_dir():
        return candidate

    ai_local_root = os.environ.get("AI_LOCAL_ROOT")
    if ai_local_root:
        candidate = Path(ai_local_root).expanduser() / "infra" / "security"
        if candidate.is_dir():
            return candidate

    default_security = Path.home() / "_projects" / "ai-local" / "infra" / "security"
    if default_security.is_dir():
        return default_security
    return Path.home() / "ai-local" / "infra" / "security"


PROJECT_ROOT = _find_project_root()
CONFIG_DIR = _find_config_dir()
SECURITY_DIR = _find_security_dir(CONFIG_DIR)


def _load_dotenv() -> None:
    """Load env files if present (does NOT override existing env vars).

    Search order:
      1. <workspace_root>/infra/docker/orc.env
    """
    workspace_root = CONFIG_DIR.parent.parent
    candidates = [
        workspace_root / "infra" / "docker" / "orc.env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            # Minimal fallback: parse KEY=VALUE lines manually
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip()
                    if key and key not in os.environ:
                        os.environ[key] = value


_load_dotenv()


def _load_toml() -> dict:
    """Load config/orc plus permission policy from infra/security."""
    merged: dict = {}
    if not CONFIG_DIR.is_dir():
        return merged
    for toml_file in sorted(CONFIG_DIR.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)
        _deep_merge(merged, data)
    security_toml = SECURITY_DIR / "orchestrator.toml"
    if security_toml.is_file():
        with open(security_toml, "rb") as f:
            _deep_merge(merged, tomllib.load(f))
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base dict. Lists are replaced, not appended."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _coerce_env_value(value: str, default):
    if isinstance(default, bool):
        return value.lower() in ("true", "1", "yes")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def _env_key(env_key: str, default):
    val = os.environ.get(env_key)
    if val is None:
        return default
    return _coerce_env_value(val, default)


def _env(section: str, key: str, default):
    """Check for env var ORC_{SECTION}_{KEY} (uppercase)."""
    env_key = f"ORC_{section.upper()}_{key.upper()}"
    val = os.environ.get(env_key)
    if val is None:
        return default
    return _coerce_env_value(val, default)


def _llm_backend_enabled(name: str, default: bool) -> bool:
    """Check ORC_LLM_BACKEND_<NAME>_ENABLED for optional serving backends."""
    key = name.upper().replace("-", "_")
    return bool(_env_key(f"ORC_LLM_BACKEND_{key}_ENABLED", default))


def _llm_backend_capabilities(name: str, capabilities: tuple[str, ...]) -> tuple[str, ...]:
    key = name.upper().replace("-", "_")
    values = list(capabilities)
    accelerator = str(_env_key(f"ORC_LLM_BACKEND_{key}_ACCELERATOR", "") or "").lower()
    if accelerator and accelerator not in {"none", "unknown"} and accelerator not in values:
        values.append(accelerator)
    return tuple(values)


_DEFAULT_SERVICE_URLS: dict[str, str] = {
    "reasoning_and_response_url": "https://reasoning-and-response:8000",
    "research_url": "https://research:8000",
    "personal_context_url": "https://personal-context:8000",
    "local_evidence_operator_url": "https://local-evidence-operator:8000",
    "execution_policy_operator_url": "https://execution-policy-operator:8000",
    "material_builder_url": "https://material-builder:8000",
    "material_execution_kernel_url": "https://material-execution-kernel:8000",
    "workspace_execution_url": "https://workspace-execution:8000",
    "extrator_url": "https://extrator:8000",
    "audio_transcribe_url": "https://audio-transcribe:8080",
    "audio_streaming_url": "https://audio-streaming:8087",
    "storage_guardian_url": "https://storage-guardian:8730",
    "translation_url": "https://translation:8590",
    "langfuse_url": "https://langfuse:3000",
    "clickhouse_url": "https://clickhouse:8123",
    "otel_endpoint": "https://otel-collector:4318",
}


def _require_https_url(value: str, field_name: str) -> str:
    clean = (value or "").rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme == "http":
        raise ValueError(f"{field_name} uses forbidden plain HTTP; configure an HTTPS URL")
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{field_name} must not contain credentials, query, or fragment")
    return clean


def _service_url(values: dict[str, Any], key: str) -> str:
    return _require_https_url(_env("services", key, values.get(key, _DEFAULT_SERVICE_URLS[key])), f"services.{key}")


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return ""


def _with_openai_v1(url: str) -> str:
    clean = url.rstrip("/")
    return clean if clean.endswith("/v1") else f"{clean}/v1"


def _ollama_base_url(configured: str = "") -> str:
    base = _first_env("ORC_OLLAMA_BASE_URL", "OLLAMA_BASE_URL") or configured.rstrip("/") or "https://host.docker.internal:11434"
    return _require_https_url(base, "ollama.base_url")


_DEFAULT_LLM_BACKEND_URLS: dict[str, str] = {
    "llama_cpp_aux": "https://llama-cpp-aux:8080",
    "llama_cpp_fast": "https://llama-cpp-fast:8080",
    "vllm": "https://vllm:8000",
}


def _llm_backend_base_url(name: str, configured: str = "", *, enabled: bool = True) -> str:
    key = name.upper().replace("-", "_")
    if name == "ollama":
        return _with_openai_v1(_ollama_base_url(configured))

    base = (
        _first_env(f"ORC_SERVICES_{key}_URL", f"{key}_URL")
        or configured.rstrip("/")
        or _DEFAULT_LLM_BACKEND_URLS.get(name, "")
    )
    if not base and enabled:
        raise ValueError(f"Backend {name!r} must have a 'base_url' or known generated service URL")
    return _with_openai_v1(_require_https_url(base, f"llm.backends.{name}.base_url")) if base else ""


def _resolve_path(raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymbiontConfig:
    host: str
    port: int
    api_key: str


@dataclass(frozen=True)
class ServicesConfig:
    """URLs for all feature/agent microservices — must be defined in config."""

    reasoning_and_response_url: str
    research_url: str
    personal_context_url: str
    local_evidence_operator_url: str
    execution_policy_operator_url: str
    material_builder_url: str
    material_execution_kernel_url: str
    workspace_execution_url: str
    extrator_url: str
    audio_transcribe_url: str
    audio_streaming_url: str
    storage_guardian_url: str
    translation_url: str
    langfuse_url: str
    clickhouse_url: str
    otel_endpoint: str


@dataclass(frozen=True)
class RAGConfig:
    url: str
    timeout: int
    health_interval: int
    circuit_breaker_threshold: int
    circuit_breaker_reset: int


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    max_concurrent_llm: int


# ---------------------------------------------------------------------------
# v0.7 — Multi-backend LLM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single OpenAI-compatible LLM backend."""

    name: str
    base_url: str
    api_key_env: str
    priority: int
    enabled: bool
    models: tuple[str, ...]
    capabilities: tuple[str, ...]
    privacy_level: str
    request_timeout: int
    stream_timeout: int


@dataclass(frozen=True)
class ModelProfileConfig:
    """Named collection of preferred models for a task type."""

    alias: str
    preferred_models: tuple[str, ...]
    fallback_model: str
    required_capabilities: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class LLMConfig:
    """Top-level LLM routing configuration."""

    default_model: str
    routing_strategy: str
    fallback_enabled: bool
    health_cache_seconds: int
    request_timeout_seconds: int
    stream_timeout_seconds: int
    backends: tuple[BackendConfig, ...]
    model_profiles: tuple[ModelProfileConfig, ...]


@dataclass(frozen=True)
class ModelsConfig:
    default: str
    fast: str
    code: str
    deep: str
    embedding: str


@dataclass(frozen=True)
class CAGConfig:
    db_path: str


@dataclass(frozen=True)
class ContextConfig:
    token_budget: int
    provider_timeout: int
    cag: CAGConfig


@dataclass(frozen=True)
class ReposConfig:
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class GraphConfig:
    output_dir: Path
    cache_ttl: int


@dataclass(frozen=True)
class SecurityConfig:
    allowed_commands: frozenset[str]
    max_command_timeout: int
    injection_scanning: bool
    agent_sandboxing: bool
    secrets_scanning: bool
    rate_limiting: bool
    audit_trail: bool
    opa_shadow_enabled: bool
    opa_enforce_enabled: bool
    injection_block_threshold: float
    per_agent_token_budget: int
    rate_limit_calls_per_minute: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    format: str


@dataclass(frozen=True)
class DynamicRoutingConfig:
    """Configuration for LLM-powered dynamic agent routing."""

    mode: str
    routing_model: str
    synthesis_model: str
    routing_timeout: float
    max_agents_per_request: int
    per_agent_timeout: float
    total_budget_tokens: int
    fallback_on_error: bool
    decomposition_enabled: bool
    negotiation_enabled: bool
    peer_review_enabled: bool
    decomposition_timeout: float
    negotiation_timeout: float
    max_subtasks: int


@dataclass(frozen=True)
class CollaborationConfig:
    """Agent collaboration settings."""

    enabled: bool
    max_rounds: int
    round_timeout_seconds: float
    max_memory_entries: int
    memory_ttl_seconds: int


@dataclass(frozen=True)
class AgenticConfig:
    enabled: bool
    max_iterations: int
    max_tool_calls: int
    timeout: int
    token_budget: int


@dataclass(frozen=True)
class AgenticRuntimeConfig:
    """Persistent agentic runtime settings."""

    enabled: bool
    shadow_ledger_enabled: bool
    default_mode: str
    autonomous_safe_enabled: bool
    policy_mode: str
    db_path: str
    approval_ttl_seconds: int
    recover_on_startup: bool
    runner_enabled: bool
    runner_poll_interval_seconds: float
    max_concurrent_tasks: int
    runner_execute_proposals: bool
    task_default_timeout_seconds: int
    material_decision_timeout_seconds: int
    task_max_retries: int
    event_loop_enabled: bool
    event_loop_poll_interval_seconds: float
    event_loop_min_repeat_interval_seconds: int
    event_loop_vllm_unhealthy_enabled: bool
    autonomous_maintenance_enabled: bool
    governed_improvement_enabled: bool
    actuator_enabled: bool
    actuator_poll_interval_seconds: float
    actuator_auto_apply_runtime_flags: bool
    actuator_max_auto_ttl_seconds: int
    actuator_min_confidence: float
    actuator_min_score: float
    actuator_impact_interval_seconds: int
    actuator_bypass_failure_threshold: int
    actuator_closed_loop_enabled: bool
    actuator_renew_enforced_flags: bool
    actuator_renewal_window_seconds: int
    actuator_max_renewals: int
    actuator_attention_ttl_seconds: int
    actuator_auto_rollback_missing_flags: bool
    actuator_escalation_ladder_enabled: bool
    actuator_escalation_window_seconds: int
    actuator_escalation_l2_threshold: int
    actuator_escalation_l3_threshold: int
    actuator_escalation_flag_ttl_seconds: int
    actuator_escalation_create_proposals: bool
    actuator_escalation_policy_router_enabled: bool
    actuator_escalation_policy_router_create_proposals: bool
    actuator_escalation_route_flag_ttl_seconds: int
    preapproval_windows_enabled: bool
    preapproval_window_default_ttl_seconds: int
    preapproval_window_max_ttl_seconds: int
    preapproval_window_max_uses: int
    preapproval_window_allowed_actions: str
    command_tool_enabled: bool
    command_tool_backend: str
    command_tool_default_context_profile: str
    command_tool_sandbox_image: str
    command_tool_timeout_seconds: int
    command_tool_max_output_bytes: int
    command_tool_session_ttl_seconds: int
    command_tool_max_commands_per_session: int
    command_tool_docker_memory_limit_mb: int
    command_tool_docker_pids_limit: int
    command_tool_allow_user_context_ro: bool
    command_tool_allow_host_context_ro: bool
    maintenance_report_interval_seconds: int
    improvement_review_interval_seconds: int
    agent_failure_threshold: int
    agent_failure_window_seconds: int
    rag_miss_threshold: int
    rag_miss_window_seconds: int
    runtime_flag_ttl_seconds: int


@dataclass(frozen=True)
class CalendarConfig:
    enabled: bool
    ics_paths: tuple[str, ...]
    window_days: int


@dataclass(frozen=True)
class RSSConfig:
    enabled: bool
    feeds: tuple[str, ...]
    cache_ttl: int
    max_entries: int
    cache_dir: str


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    mailbox_paths: tuple[str, ...]
    max_age_days: int
    max_results: int


@dataclass(frozen=True)
class SessionConfig:
    enabled: bool
    ttl_seconds: int
    db_path: str
    max_messages: int
    cli_default_session: bool
    cli_session_state_file: str


@dataclass(frozen=True)
class ClassifyConfig:
    """Heuristic classification + history-aware routing-confidence settings."""

    confidence_threshold: float
    history_aware_threshold: float
    history_window: int
    anaphora_confidence_penalty: float
    general_followup_penalty: float
    general_followup_max_words: int
    anaphora_words: frozenset[str]
    anaphora_patterns: tuple[str, ...]


@dataclass(frozen=True)
class MetricsStoreConfig:
    enabled: bool
    retention_days: int
    flush_interval_seconds: float
    db_path: str
    resource_monitor_enabled: bool
    resource_interval_seconds: float
    vram_warning_mb: int
    vram_critical_mb: int
    swap_warning_mb: int
    swap_critical_mb: int


@dataclass(frozen=True)
class DashboardSettingsConfig:
    enabled: bool


@dataclass(frozen=True)
class AdaptiveVramThresholdsConfig:
    high_mb: int
    mid_mb: int
    entry_mb: int
    quantize_below_mb: int
    full_warning_free_mb: int


@dataclass(frozen=True)
class AdaptiveVramProfileConfig:
    max_loaded_models: int
    max_concurrent_llm: int
    preferred_num_ctx: int
    keep_alive: str
    prefer_quantized: bool
    gpu_offload: bool


@dataclass(frozen=True)
class AdaptiveVramProfilesConfig:
    high: AdaptiveVramProfileConfig
    mid: AdaptiveVramProfileConfig
    entry: AdaptiveVramProfileConfig
    low: AdaptiveVramProfileConfig
    cpu_only: AdaptiveVramProfileConfig


@dataclass(frozen=True)
class AdaptiveRamThresholdsConfig:
    high_mb: int
    standard_mb: int
    low_mb: int
    swap_warning_ratio: float


@dataclass(frozen=True)
class AdaptiveRamProfileConfig:
    response_cache_max_size: int
    metrics_ring_buffer_size: int
    context_token_budget: int
    context_budget_multiplier: float


@dataclass(frozen=True)
class AdaptiveRamProfilesConfig:
    high: AdaptiveRamProfileConfig
    standard: AdaptiveRamProfileConfig
    low: AdaptiveRamProfileConfig
    minimal: AdaptiveRamProfileConfig


@dataclass(frozen=True)
class AdaptiveDiskProfileConfig:
    metrics_flush_batch_size: int
    enable_model_preloading: bool
    log_buffer_size: int


@dataclass(frozen=True)
class AdaptiveDiskProfilesConfig:
    nvme: AdaptiveDiskProfileConfig
    ssd: AdaptiveDiskProfileConfig
    hdd: AdaptiveDiskProfileConfig


@dataclass(frozen=True)
class AdaptivePolicyConfig:
    min_context_workers: int
    max_context_workers: int
    parallel_min_vram_mb: int
    parallel_min_physical_cores: int
    ollama_num_parallel_default: int
    ollama_num_parallel_gpu: int
    low_disk_free_gb: int
    vram_pressure_threshold: float
    ram_pressure_threshold: float
    cpu_pressure_threshold: float
    vram_thresholds: AdaptiveVramThresholdsConfig
    vram_profiles: AdaptiveVramProfilesConfig
    ram_thresholds: AdaptiveRamThresholdsConfig
    ram_profiles: AdaptiveRamProfilesConfig
    disk_profiles: AdaptiveDiskProfilesConfig


@dataclass(frozen=True)
class HardwareConfig:
    """Hardware auto-detection and adaptive optimization settings."""

    auto_detect: bool
    refresh_interval: int
    response_cache_enabled: bool
    response_cache_ttl: int
    adaptive_degradation: bool
    adaptive: AdaptivePolicyConfig


def _parse_adaptive_vram_profile(raw: dict, profile_name: str, section_name: str) -> AdaptiveVramProfileConfig:
    defaults = _DERIVED_HARDWARE_VRAM_PROFILE_DEFAULTS[profile_name]
    env_prefix = f"HARDWARE_ADAPTIVE_VRAM_PROFILES_{profile_name.upper()}"
    return AdaptiveVramProfileConfig(
        max_loaded_models=_derived_env_value(env_prefix, raw, defaults, "max_loaded_models"),
        max_concurrent_llm=_derived_env_value(env_prefix, raw, defaults, "max_concurrent_llm"),
        preferred_num_ctx=_derived_env_value(env_prefix, raw, defaults, "preferred_num_ctx"),
        keep_alive=_derived_env_value(env_prefix, raw, defaults, "keep_alive"),
        prefer_quantized=_derived_env_value(env_prefix, raw, defaults, "prefer_quantized"),
        gpu_offload=_derived_env_value(env_prefix, raw, defaults, "gpu_offload"),
    )


def _parse_adaptive_ram_profile(raw: dict, profile_name: str, section_name: str) -> AdaptiveRamProfileConfig:
    defaults = _DERIVED_HARDWARE_RAM_PROFILE_DEFAULTS[profile_name]
    env_prefix = f"HARDWARE_ADAPTIVE_RAM_PROFILES_{profile_name.upper()}"
    return AdaptiveRamProfileConfig(
        response_cache_max_size=_derived_env_value(env_prefix, raw, defaults, "response_cache_max_size"),
        metrics_ring_buffer_size=_derived_env_value(env_prefix, raw, defaults, "metrics_ring_buffer_size"),
        context_token_budget=_derived_env_value(env_prefix, raw, defaults, "context_token_budget"),
        context_budget_multiplier=_derived_env_value(env_prefix, raw, defaults, "context_budget_multiplier"),
    )


def _parse_adaptive_disk_profile(raw: dict, profile_name: str, section_name: str) -> AdaptiveDiskProfileConfig:
    defaults = _DERIVED_HARDWARE_DISK_PROFILE_DEFAULTS[profile_name]
    env_prefix = f"HARDWARE_ADAPTIVE_DISK_PROFILES_{profile_name.upper()}"
    return AdaptiveDiskProfileConfig(
        metrics_flush_batch_size=_derived_env_value(env_prefix, raw, defaults, "metrics_flush_batch_size"),
        enable_model_preloading=_derived_env_value(env_prefix, raw, defaults, "enable_model_preloading"),
        log_buffer_size=_derived_env_value(env_prefix, raw, defaults, "log_buffer_size"),
    )


@dataclass(frozen=True)
class InferenceProfileConfig:
    """Parameters for a single inference profile (fast/default/code/deep)."""

    num_ctx: int
    num_predict: int
    temperature: float
    top_p: float | None


@dataclass(frozen=True)
class PipelineConfig:
    """Pipeline parallelism and speculative execution settings."""

    speculative_prefetch: bool
    batch_inference: bool
    async_providers: bool
    connection_pool_size: int
    keepalive_expiry: int
    http2_enabled: bool
    streaming_events: bool


@dataclass(frozen=True)
class ContextBudgetConfig:
    """Context budget per profile."""

    max_context_tokens: int
    rag_top_k: int
    graph_enabled: bool
    system_snapshot_enabled: str


@dataclass(frozen=True)
class ContextBudgetScalingConfig:
    """Scaling of context budgets by the selected model's context window."""

    reference_context_window: int
    max_scale: float
    graph_min_context_window: int


@dataclass(frozen=True)
class FeatureEndpointMapping:
    """Maps a context source to a feature service endpoint."""

    feature: str
    method: str
    path: str
    policy_action: str = ""


@dataclass(frozen=True)
class DispatchFeatureEndpoint:
    """Primary dispatch endpoint metadata for a feature service."""

    method: str
    path: str
    auth_profile: str = "internal_api"
    tls_alias_profile: str = ""
    policy_action: str = ""

    def as_tuple(self) -> tuple[str, str]:
        return (self.method, self.path)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.as_tuple())

    def __getitem__(self, index: int) -> str:
        return self.as_tuple()[index]

    def __len__(self) -> int:
        return 2

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DispatchFeatureEndpoint):
            return (
                self.method,
                self.path,
                self.auth_profile,
                self.tls_alias_profile,
                self.policy_action,
            ) == (
                other.method,
                other.path,
                other.auth_profile,
                other.tls_alias_profile,
                other.policy_action,
            )
        if isinstance(other, tuple):
            return self.as_tuple() == other
        return False


@dataclass(frozen=True)
class DispatchStreamingConfig:
    """Budgets and sampling for the local streaming fast-path."""

    max_context_budget_tokens: int
    max_tokens: int
    temperature: float
    timeout_seconds: float
    unranked_source_priority: int
    source_priority: dict[str, int]


@dataclass(frozen=True)
class DispatchConfig:
    """Token budgets, timeouts and source routing for context/agent dispatch."""

    context_budget_tokens: int
    context_timeout_per_source: float
    feature_budget_tokens: int
    feature_timeout_seconds: float
    context_parallel_max_workers: int
    agent_budget_tokens: int
    agent_timeout_seconds: float
    streaming: DispatchStreamingConfig
    source_map: dict[str, FeatureEndpointMapping]
    feature_endpoints: dict[str, DispatchFeatureEndpoint]
    agent_endpoints: dict[str, tuple[str, str]]
    agent_timeouts: dict[str, float]


@dataclass(frozen=True)
class PerformanceConfig:
    """LLM warmup and keep_alive settings."""

    warmup_enabled: bool
    warmup_on_startup: bool
    keep_alive: str
    max_loaded_models: int
    primary_warm_models: tuple[str, ...]
    fallback_warm_models: tuple[str, ...]


@dataclass(frozen=True)
class LatencyRoutingConfig:
    """Latency-aware routing settings."""

    enabled: bool
    p95_threshold_ms: int
    prefer_warm: bool
    simple_task_max_latency_ms: int
    max_latency_samples: int
    priority_weight: float
    p95_penalty_per_second: float
    warm_bonus: float


@dataclass(frozen=True)
class AdmissionConfig:
    """Admission control settings — per-backend semaphores, rate limiting, downgrade."""

    max_concurrent_global: int
    max_tokens_per_request: int
    rate_limit_requests_per_window: int
    rate_limit_window_seconds: float
    queue_enabled: bool
    queue_timeout_seconds: float
    reject_retry_after_seconds: float
    downgrade_model: str
    downgrade_backend: str
    backend_concurrency: dict[str, int]


@dataclass(frozen=True)
class RoutingPolicyRule:
    """A single routing policy rule: task_type → (backend, model)."""

    task_type: str
    backend: str
    model: str


@dataclass(frozen=True)
class RoutingPolicyConfig:
    """Policy-based fallback routing — maps task types to preferred backends."""

    default_backend: str
    default_model: str
    rules: tuple[RoutingPolicyRule, ...]


@dataclass(frozen=True)
class EscalationConfig:
    """Confidence-based model escalation settings."""

    enabled: bool
    min_critic_score: float
    max_escalations: int
    chain: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityConfig:
    """Model capability detection settings."""

    enabled: bool
    probe_on_first_use: bool


@dataclass(frozen=True)
class ContainerLifecycleConfig:
    """Container lifecycle management — on-demand start/stop of service containers."""

    enabled: bool
    idle_timeout: int
    start_timeout: int
    health_poll_interval: float
    idle_check_interval: int
    docker_host: str
    compose_project: str
    compose_file: str
    compose_project_dir: str
    compose_profiles: tuple[str, ...]
    always_on: tuple[str, ...]
    pre_warm: tuple[str, ...]
    per_service_overrides: dict[str, dict]


@dataclass(frozen=True)
class PrewarmConfig:
    """Predictive prewarming — predict needed services before LLM planning."""

    enabled: bool
    max_prewarm_per_request: int
    max_gpu_prewarm_per_request: int
    high_confidence_threshold: float
    medium_confidence_threshold: float
    embedding_model: str
    classifier_model: str
    classifier_timeout_ms: int
    ttl_unused_seconds: int
    catalog_path: str
    l1_backend: str
    l1_fastembed_model: str
    level1_enabled: bool
    rule_boost: float
    recent_usage_boost: float
    startup_cost_penalty: float
    gpu_pressure_penalty: float
    already_running_bonus: float
    level2_enabled: bool
    level2_ambiguity_gap: float


@dataclass(frozen=True)
class IntelligentPipelineConfig:
    """Intelligent Execution Pipeline settings."""

    smart_retry_enabled: bool
    max_retries_per_agent: int
    retry_confidence_threshold: float
    progressive_refinement_enabled: bool
    early_termination_enabled: bool
    early_termination_confidence: float
    dead_path_elimination_enabled: bool


@dataclass(frozen=True)
class Settings:
    symbiont: SymbiontConfig
    rag: RAGConfig
    ollama: OllamaConfig
    services: ServicesConfig
    models: ModelsConfig
    context: ContextConfig
    repos: ReposConfig
    graph: GraphConfig
    security: SecurityConfig
    logging: LoggingConfig
    agentic: AgenticConfig
    agentic_runtime: AgenticRuntimeConfig
    session: SessionConfig
    classify: ClassifyConfig
    calendar: CalendarConfig
    rss: RSSConfig
    email: EmailConfig
    llm: LLMConfig
    metrics: MetricsStoreConfig
    dashboard: DashboardSettingsConfig
    hardware: HardwareConfig
    performance: PerformanceConfig
    latency_routing: LatencyRoutingConfig
    dynamic_routing: DynamicRoutingConfig
    collaboration: CollaborationConfig
    pipeline: PipelineConfig
    escalation: EscalationConfig
    capability: CapabilityConfig
    intelligent_pipeline: IntelligentPipelineConfig
    container_lifecycle: ContainerLifecycleConfig
    prewarming: PrewarmConfig
    inference_profiles: dict[str, InferenceProfileConfig]
    context_budgets: dict[str, ContextBudgetConfig]
    context_budget_scaling: ContextBudgetScalingConfig
    admission: AdmissionConfig | None
    routing_policy: RoutingPolicyConfig | None
    dispatch: DispatchConfig
    observability_raw: dict
    i18n_raw: dict
    execution: Any  # ExecutionConfig or None
    openai_compat_profiles: tuple[str, ...]


# ---------------------------------------------------------------------------
# Strict config access — fail on missing keys
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _require(section: dict, key: str, section_name: str):
    """Get a required key from a TOML section dict. Raises ValueError if missing."""
    val = section.get(key, _SENTINEL)
    if val is _SENTINEL:
        raise ValueError(
            f"Missing required config key '{key}' in [{section_name}]. "
            f"Add it to the appropriate config/orc/*.toml file."
        )
    return val


def _get(section: dict, key: str, section_name: str):
    """Alias for _require — reads a required key from TOML section."""
    return _require(section, key, section_name)


_DERIVED_DISPATCH_DEFAULTS = {
    "context_budget_tokens": 6000,
    "context_timeout_per_source": 10.0,
    "feature_budget_tokens": 2000,
    "feature_timeout_seconds": 10.0,
    "context_parallel_max_workers": 8,
    "agent_budget_tokens": 2000,
    "agent_timeout_seconds": 120.0,
}

_DERIVED_DISPATCH_STREAMING_DEFAULTS = {
    "max_context_budget_tokens": 420,
    "max_tokens": 384,
    "timeout_seconds": 300.0,
}

_DERIVED_DISPATCH_AGENT_TIMEOUTS = {
    "reasoning_and_response": 150.0,
    "audio_transcribe": 120.0,
}

_DERIVED_DYNAMIC_ROUTING_DEFAULTS = {
    "routing_timeout": 5.0,
    "max_agents_per_request": 4,
    "per_agent_timeout": 120.0,
    "total_budget_tokens": 15000,
    "decomposition_timeout": 5.0,
    "negotiation_timeout": 1.0,
    "max_subtasks": 5,
}

_DERIVED_COLLABORATION_DEFAULTS = {
    "max_rounds": 2,
    "round_timeout_seconds": 5.0,
    "max_memory_entries": 10,
    "memory_ttl_seconds": 300,
}

_DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS = {
    "command_tool_timeout_seconds": 120,
    "command_tool_max_output_bytes": 64000,
    "command_tool_session_ttl_seconds": 2160,
    "command_tool_max_commands_per_session": 40,
    "command_tool_docker_memory_limit_mb": 512,
    "command_tool_docker_pids_limit": 192,
}

_DERIVED_AGENTIC_DEFAULTS = {
    "max_iterations": 10,
    "max_tool_calls": 30,
    "timeout": 1200,
    "token_budget": 32000,
}

_DERIVED_CLASSIFY_DEFAULTS = {
    "confidence_threshold": 0.7,
    "history_aware_threshold": 0.75,
    "history_window": 4,
    "anaphora_confidence_penalty": 0.2,
    "general_followup_penalty": 0.1,
    "general_followup_max_words": 8,
}

_DERIVED_AGENTIC_RUNTIME_DEFAULTS = {
    "db_path": "/app/data/symbiont/agentic.db",
    "approval_ttl_seconds": 3600,
    "runner_poll_interval_seconds": 2.0,
    "max_concurrent_tasks": 1,
    "task_default_timeout_seconds": 600,
    "material_decision_timeout_seconds": 300,
    "task_max_retries": 1,
    "event_loop_poll_interval_seconds": 30.0,
    "event_loop_min_repeat_interval_seconds": 300,
    "actuator_poll_interval_seconds": 5.0,
    "actuator_max_auto_ttl_seconds": 900,
    "actuator_min_confidence": 0.75,
    "actuator_min_score": 3.0,
    "actuator_impact_interval_seconds": 60,
    "actuator_bypass_failure_threshold": 1,
    "actuator_renewal_window_seconds": 60,
    "actuator_max_renewals": 1,
    "actuator_attention_ttl_seconds": 300,
    "actuator_escalation_window_seconds": 900,
    "actuator_escalation_l2_threshold": 2,
    "actuator_escalation_l3_threshold": 3,
    "actuator_escalation_flag_ttl_seconds": 900,
    "actuator_escalation_route_flag_ttl_seconds": 900,
    "preapproval_window_default_ttl_seconds": 300,
    "preapproval_window_max_ttl_seconds": 900,
    "preapproval_window_max_uses": 5,
    "maintenance_report_interval_seconds": 3600,
    "improvement_review_interval_seconds": 3600,
    "agent_failure_threshold": 3,
    "agent_failure_window_seconds": 900,
    "rag_miss_threshold": 3,
    "rag_miss_window_seconds": 900,
    "runtime_flag_ttl_seconds": 300,
}

_DERIVED_ADMISSION_DEFAULTS = {
    "max_concurrent_global": 3,
    "max_tokens_per_request": 8192,
    "rate_limit_requests_per_window": 20,
    "rate_limit_window_seconds": 60.0,
    "queue_enabled": False,
    "queue_timeout_seconds": 30.0,
    "reject_retry_after_seconds": 5.0,
}

_DERIVED_ADMISSION_BACKEND_CONCURRENCY_DEFAULTS = {
    "vllm": 2,
    "llama_cpp_aux": 3,
    "llama_cpp_fast": 4,
    "ollama": 1,
}

_DERIVED_SESSION_DEFAULTS = {
    "ttl_seconds": 604800,
    "db_path": "/app/data/symbiont/sessions.db",
    "cli_session_state_file": "/app/data/symbiont/cli_default_session",
}

_DERIVED_METRICS_DEFAULTS = {
    "retention_days": 90,
    "flush_interval_seconds": 2.0,
    "db_path": "",
    "resource_interval_seconds": 60.0,
}

_DERIVED_SYMBIONT_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8585,
}

_DERIVED_SECURITY_DEFAULTS = {
    "max_command_timeout": 5,
    "rate_limiting": False,
    "opa_shadow_enabled": True,
    "opa_enforce_enabled": False,
    "injection_block_threshold": 0.8,
    "rate_limit_calls_per_minute": 30,
}

_DERIVED_CONTEXT_DEFAULTS = {
    "token_budget": 6000,
    "provider_timeout": 10,
}

_DERIVED_CONTEXT_CAG_DEFAULTS = {
    "db_path": "/app/data/symbiont/cag.db",
}

_DERIVED_CONTEXT_BUDGET_DEFAULTS = {
    "fast": {
        "max_context_tokens": 800,
        "rag_top_k": 3,
        "graph_enabled": False,
        "system_snapshot_enabled": "false",
    },
    "default": {
        "max_context_tokens": 1800,
        "rag_top_k": 5,
        "graph_enabled": True,
        "system_snapshot_enabled": "auto",
    },
    "code": {
        "max_context_tokens": 1800,
        "rag_top_k": 5,
        "graph_enabled": True,
        "system_snapshot_enabled": "false",
    },
    "deep": {
        "max_context_tokens": 3500,
        "rag_top_k": 8,
        "graph_enabled": True,
        "system_snapshot_enabled": "auto",
    },
}

_DERIVED_CONTEXT_BUDGET_SCALING_DEFAULTS = {
    "reference_context_window": 8192,
    "max_scale": 2.0,
    "graph_min_context_window": 4096,
}

_DERIVED_HARDWARE_DEFAULTS = {
    "auto_detect": True,
    "refresh_interval": 0,
    "response_cache_enabled": True,
    "response_cache_ttl": 3600,
    "adaptive_degradation": True,
}

_DERIVED_HARDWARE_ADAPTIVE_DEFAULTS = {
    "min_context_workers": 2,
    "max_context_workers": 8,
    "parallel_min_vram_mb": 16000,
    "parallel_min_physical_cores": 8,
    "ollama_num_parallel_default": 1,
    "ollama_num_parallel_gpu": 2,
    "low_disk_free_gb": 10,
    "vram_pressure_threshold": 0.90,
    "ram_pressure_threshold": 0.85,
    "cpu_pressure_threshold": 0.90,
}

_DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS = {
    "high_mb": 24000,
    "mid_mb": 12000,
    "entry_mb": 6000,
    "quantize_below_mb": 8000,
    "full_warning_free_mb": 1000,
}

_DERIVED_HARDWARE_VRAM_PROFILE_DEFAULTS = {
    "high": {
        "max_loaded_models": 3,
        "max_concurrent_llm": 2,
        "preferred_num_ctx": 8192,
        "keep_alive": "4h",
        "prefer_quantized": False,
        "gpu_offload": True,
    },
    "mid": {
        "max_loaded_models": 2,
        "max_concurrent_llm": 1,
        "preferred_num_ctx": 8192,
        "keep_alive": "2h",
        "prefer_quantized": False,
        "gpu_offload": True,
    },
    "entry": {
        "max_loaded_models": 2,
        "max_concurrent_llm": 1,
        "preferred_num_ctx": 4096,
        "keep_alive": "2h",
        "prefer_quantized": False,
        "gpu_offload": True,
    },
    "low": {
        "max_loaded_models": 1,
        "max_concurrent_llm": 1,
        "preferred_num_ctx": 2048,
        "keep_alive": "30m",
        "prefer_quantized": True,
        "gpu_offload": True,
    },
    "cpu_only": {
        "max_loaded_models": 1,
        "max_concurrent_llm": 1,
        "preferred_num_ctx": 2048,
        "keep_alive": "1h",
        "prefer_quantized": True,
        "gpu_offload": False,
    },
}

_DERIVED_HARDWARE_RAM_THRESHOLDS_DEFAULTS = {
    "high_mb": 32000,
    "standard_mb": 16000,
    "low_mb": 8000,
    "swap_warning_ratio": 0.30,
}

_DERIVED_HARDWARE_RAM_PROFILE_DEFAULTS = {
    "high": {
        "response_cache_max_size": 10000,
        "metrics_ring_buffer_size": 5000,
        "context_token_budget": 8000,
        "context_budget_multiplier": 1.3,
    },
    "standard": {
        "response_cache_max_size": 5000,
        "metrics_ring_buffer_size": 2000,
        "context_token_budget": 6000,
        "context_budget_multiplier": 1.0,
    },
    "low": {
        "response_cache_max_size": 2000,
        "metrics_ring_buffer_size": 1000,
        "context_token_budget": 4000,
        "context_budget_multiplier": 0.8,
    },
    "minimal": {
        "response_cache_max_size": 500,
        "metrics_ring_buffer_size": 500,
        "context_token_budget": 2500,
        "context_budget_multiplier": 0.5,
    },
}

_DERIVED_HARDWARE_DISK_PROFILE_DEFAULTS = {
    "nvme": {
        "metrics_flush_batch_size": 100,
        "enable_model_preloading": True,
        "log_buffer_size": 200,
    },
    "ssd": {
        "metrics_flush_batch_size": 50,
        "enable_model_preloading": True,
        "log_buffer_size": 100,
    },
    "hdd": {
        "metrics_flush_batch_size": 20,
        "enable_model_preloading": False,
        "log_buffer_size": 50,
    },
}

_DERIVED_LIFECYCLE_DEFAULTS = {
    "enabled": True,
    "idle_timeout": 600,
    "start_timeout": 30,
    "health_poll_interval": 0.5,
    "idle_check_interval": 15,
    "docker_host": "https://docker-proxy:2375",
    "compose_project": "ai-local",
    "compose_file": "/project/compose.yml",
    "compose_project_dir": "/project",
    "compose_profiles": ["core", "storage", "agents", "features", "i18n", "heavy"],
    "always_on": [
        "storage_guardian",
    ],
    "pre_warm": [],
}

_DERIVED_LIFECYCLE_OVERRIDE_DEFAULTS = {
    "audio_transcribe": {"idle_timeout": 900, "start_timeout": 45},
    "audio_streaming": {"idle_timeout": 900, "start_timeout": 45},
    "redis": {"idle_timeout": 900, "start_timeout": 30},
    "reasoning_and_response": {"idle_timeout": 900, "start_timeout": 45},
    "research": {"idle_timeout": 600},
    "local_evidence_operator": {"idle_timeout": 600},
    "execution_policy_operator": {"start_timeout": 60},
    "material_builder": {"start_timeout": 60},
    "material_execution_kernel": {"start_timeout": 60},
    "workspace_execution": {"start_timeout": 120},
    "storage_guardian": {"start_timeout": 60},
    "personal_context": {"idle_timeout": 600},
    "extrator": {"idle_timeout": 900, "start_timeout": 45},
    "translation": {"idle_timeout": 900, "start_timeout": 45},
}

_SESSION_PRESERVING_LIFECYCLE_SERVICES = frozenset(
    {
        "material_builder",
        "material_execution_kernel",
        "workspace_execution",
        "storage_guardian",
    }
)

_DERIVED_PREWARMING_DEFAULTS = {
    "enabled": True,
    "max_prewarm_per_request": 2,
    "max_gpu_prewarm_per_request": 0,
    "high_confidence_threshold": 0.85,
    "medium_confidence_threshold": 0.65,
    "embedding_model": "bge-m3",
    "classifier_model": "qwen3:0.6b",
    "classifier_timeout_ms": 500,
    "ttl_unused_seconds": 300,
    "catalog_path": "",
    "l1_backend": "fastembed",
    "l1_fastembed_model": "BAAI/bge-small-en-v1.5",
    "level1_enabled": False,
    "rule_boost": 0.15,
    "recent_usage_boost": 0.10,
    "startup_cost_penalty": 0.10,
    "gpu_pressure_penalty": 0.20,
    "already_running_bonus": 0.30,
    "level2_enabled": True,
    "level2_ambiguity_gap": 0.15,
}

_DERIVED_LLM_DEFAULTS = {
    "request_timeout_seconds": 120,
    "stream_timeout_seconds": 180,
    "routing_strategy": "capability_first",
    "fallback_enabled": True,
    "health_cache_seconds": 5,
}

_DERIVED_LLM_PERFORMANCE_DEFAULTS = {
    "warmup_enabled": False,
    "warmup_on_startup": False,
    "keep_alive": "5m",
    "max_loaded_models": 1,
    "primary_warm_models": [],
    "fallback_warm_models": [],
}

_DERIVED_LLM_BACKEND_TIMEOUT_DEFAULTS = {
    "ollama": {"request_timeout": 120, "stream_timeout": 180},
    "llama_cpp_aux": {"request_timeout": 30, "stream_timeout": 180},
    "llama_cpp_fast": {"request_timeout": 15, "stream_timeout": 180},
    "vllm": {"request_timeout": 120, "stream_timeout": 180},
}

_DERIVED_LLM_LATENCY_ROUTING_DEFAULTS = {
    "enabled": True,
    "p95_threshold_ms": 8000,
    "prefer_warm": True,
    "simple_task_max_latency_ms": 3000,
    "max_latency_samples": 50,
    "priority_weight": 1.0,
    "p95_penalty_per_second": 0.5,
    "warm_bonus": 1.0,
}

_DERIVED_LLM_PIPELINE_DEFAULTS = {
    "speculative_prefetch": True,
    "batch_inference": True,
    "async_providers": True,
    "connection_pool_size": 20,
    "keepalive_expiry": 300,
    "http2_enabled": True,
    "streaming_events": False,
}

_DERIVED_LLM_PROFILE_DEFAULTS = {
    "fast": {"num_ctx": 2048, "num_predict": 200, "temperature": 0.2, "top_p": 0.9},
    "default": {"num_ctx": 4096, "num_predict": 512, "temperature": 0.3, "top_p": 0.9},
    "code": {"num_ctx": 4096, "num_predict": 700, "temperature": 0.15, "top_p": 0.95},
    "deep": {"num_ctx": 8192, "num_predict": 1024, "temperature": 0.3, "top_p": 0.9},
}

_DERIVED_LLM_ESCALATION_DEFAULTS = {
    "enabled": False,
    "min_critic_score": 0.5,
    "max_escalations": 1,
    "chain": [],
}

_DERIVED_LLM_CAPABILITY_DEFAULTS = {
    "enabled": False,
    "probe_on_first_use": False,
}

_DERIVED_INTELLIGENT_PIPELINE_DEFAULTS = {
    "smart_retry_enabled": False,
    "max_retries_per_agent": 1,
    "retry_confidence_threshold": 0.4,
    "progressive_refinement_enabled": False,
    "early_termination_enabled": False,
    "early_termination_confidence": 0.95,
    "dead_path_elimination_enabled": False,
}

_DERIVED_OPENAI_COMPAT_DEFAULTS = {
    "expose_profiles": ["symbiont", "symbiont-code", "symbiont-deep", "symbiont-fast"],
}

_DERIVED_OLLAMA_DEFAULTS = {
    "max_concurrent_llm": 1,
}

_DERIVED_REPOS_DEFAULTS = {
    "paths": [],
}

_DERIVED_GRAPH_DEFAULTS = {
    "output_dir": "/app/data/symbiont/graphify",
    "cache_ttl": 300,
}

_DERIVED_CALENDAR_DEFAULTS = {
    "enabled": False,
    "ics_paths": [],
    "window_days": 7,
}

_DERIVED_RSS_DEFAULTS = {
    "enabled": False,
    "feeds": [],
    "cache_ttl": 1800,
    "max_entries": 20,
    "cache_dir": "/app/data/symbiont/rss",
}

_DERIVED_EMAIL_DEFAULTS = {
    "enabled": False,
    "mailbox_paths": [],
    "max_age_days": 7,
    "max_results": 15,
}


def _derived_value(section: dict, key: str, defaults: dict[str, Any], env_section: str):
    default = section.get(key, defaults[key])
    return _env(env_section, key, default)


def _derived_section_value(section: dict, key: str, defaults: dict[str, Any], env_section: str):
    return _env(env_section, key, section.get(key, defaults[key]))


def _derived_context_budget_value(profile: str, section: dict, key: str):
    env_key = f"ORC_CONTEXT_BUDGET_{profile.upper()}_{key.upper()}"
    default = section.get(key, _DERIVED_CONTEXT_BUDGET_DEFAULTS[profile][key])
    return _env_key(env_key, default)


def _derived_env_value(env_prefix: str, section: dict, defaults: dict[str, Any], key: str):
    env_key = f"ORC_{env_prefix.upper()}_{key.upper()}"
    return _env_key(env_key, section.get(key, defaults[key]))


def _material_session_idle_timeout(agentic_runtime: AgenticRuntimeConfig, lifecycle_idle_timeout: int) -> int:
    """Derive lifecycle TTL for services that preserve in-memory material sessions."""
    material_session_budget = _env_key(
        "MATERIAL_EXECUTION_KERNEL_SESSION_BUDGET_SECONDS",
        max(agentic_runtime.task_default_timeout_seconds, agentic_runtime.material_decision_timeout_seconds),
    )
    material_builder_timeout = _env_key(
        "MATERIAL_EXECUTION_KERNEL_BUILDER_TIMEOUT_SECONDS",
        material_session_budget,
    )
    material_workspace_timeout = _env_key(
        "MATERIAL_EXECUTION_KERNEL_WORKSPACE_TIMEOUT_SECONDS",
        lifecycle_idle_timeout,
    )
    no_progress_watchdog = _env_key(
        "MATERIAL_EXECUTION_KERNEL_NO_PROGRESS_WATCHDOG_SECONDS",
        max(int(lifecycle_idle_timeout / 5), 60),
    )
    base_budget = max(
        int(lifecycle_idle_timeout),
        int(agentic_runtime.task_default_timeout_seconds),
        int(agentic_runtime.material_decision_timeout_seconds),
        int(material_session_budget),
        int(material_builder_timeout),
        int(material_workspace_timeout),
    )
    operational_margin = max(int(no_progress_watchdog), int(lifecycle_idle_timeout / 2))
    return base_budget + operational_margin


def _derived_lifecycle_override(
    service_name: str,
    raw_overrides: dict,
    *,
    session_idle_timeout: int | None = None,
) -> dict[str, Any]:
    merged = {
        **_DERIVED_LIFECYCLE_OVERRIDE_DEFAULTS.get(service_name, {}),
        **raw_overrides.get(service_name, {}),
    }
    if (
        service_name in _SESSION_PRESERVING_LIFECYCLE_SERVICES
        and "idle_timeout" not in merged
        and session_idle_timeout is not None
    ):
        merged["idle_timeout"] = session_idle_timeout
    env_prefix = f"LIFECYCLE_OVERRIDES_{service_name.upper()}"
    return {
        key: _derived_env_value(env_prefix, merged, merged, key)
        for key in merged
    }


def _derived_backend_timeout(backend_name: str, backend: dict, llm_raw: dict, key: str):
    backend_defaults = _DERIVED_LLM_BACKEND_TIMEOUT_DEFAULTS.get(backend_name, {})
    default = backend.get(key, llm_raw.get(f"{key}_seconds", backend_defaults.get(key, _DERIVED_LLM_DEFAULTS[f"{key}_seconds"])))
    env_name = backend_name.upper().replace("-", "_")
    return _env_key(f"ORC_LLM_BACKEND_{env_name}_{key.upper()}", default)


def _derived_llm_profile_value(profile_name: str, profile: dict, key: str):
    return _env_key(
        f"ORC_LLM_PROFILE_{profile_name.upper()}_{key.upper()}",
        profile.get(key, _DERIVED_LLM_PROFILE_DEFAULTS[profile_name][key]),
    )


def _registry_llm_backends(
    registry_data: dict[str, Any],
    *,
    llm_raw: dict[str, Any],
    models: ModelsConfig,
    ollama: OllamaConfig,
) -> tuple[BackendConfig, ...]:
    catalog = registry_data.get("orchestration", {}).get("backends", {})
    if not isinstance(catalog, dict) or not catalog:
        auto_models = tuple(m for m in [models.default, models.fast, models.code, models.deep, models.embedding] if m)
        enabled = _llm_backend_enabled("ollama", True)
        capabilities = _llm_backend_capabilities("ollama", ("chat", "stream", "local", "private"))
        return (
            BackendConfig(
                name="ollama",
                base_url=_llm_backend_base_url("ollama", ollama.base_url, enabled=enabled),
                api_key_env="OLLAMA_API_KEY",
                priority=10,
                enabled=enabled,
                models=auto_models,
                capabilities=capabilities,
                privacy_level="local",
                request_timeout=_derived_backend_timeout("ollama", {}, llm_raw, "request_timeout"),
                stream_timeout=_derived_backend_timeout("ollama", {}, llm_raw, "stream_timeout"),
            ),
        )

    parsed: list[BackendConfig] = []
    for name, backend in catalog.items():
        if not isinstance(backend, dict):
            continue
        backend_name = str(name)
        enabled = _llm_backend_enabled(backend_name, bool(backend.get("enabled", True)))
        backend_models = tuple(str(model) for model in backend.get("models", []) if str(model))
        if backend_name == "ollama" and not backend_models:
            backend_models = tuple(m for m in [models.default, models.fast, models.code, models.deep, models.embedding] if m)
        parsed.append(
            BackendConfig(
                name=backend_name,
                base_url=_llm_backend_base_url(backend_name, str(backend.get("base_url", "")), enabled=enabled),
                api_key_env=str(backend.get("api_key_env", "OLLAMA_API_KEY" if backend_name == "ollama" else "")),
                priority=int(backend.get("priority", 10)),
                enabled=enabled,
                models=backend_models,
                capabilities=_llm_backend_capabilities(
                    backend_name,
                    tuple(str(item) for item in backend.get("capabilities", [])),
                ),
                privacy_level=str(backend.get("privacy_level", "local")),
                request_timeout=_derived_backend_timeout(backend_name, backend, llm_raw, "request_timeout"),
                stream_timeout=_derived_backend_timeout(backend_name, backend, llm_raw, "stream_timeout"),
            )
        )
    return tuple(parsed)


def _registry_model_profiles(registry_data: dict[str, Any]) -> tuple[ModelProfileConfig, ...]:
    profiles = registry_data.get("orchestration", {}).get("routing", {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return ()

    parsed: list[ModelProfileConfig] = []
    for alias, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        preferred = tuple(str(item) for item in profile.get("preferred_models", []) if str(item))
        model = str(profile.get("model", ""))
        if not preferred and model:
            preferred = (model,)
        fallback = str(profile.get("fallback_model", preferred[0] if preferred else model))
        parsed.append(
            ModelProfileConfig(
                alias=str(alias),
                preferred_models=preferred,
                fallback_model=fallback,
                required_capabilities=tuple(str(item) for item in profile.get("required_capabilities", ["chat"])),
                enabled=bool(profile.get("enabled", True)),
            )
        )
    return tuple(parsed)


def _registry_profile_model(registry_data: dict[str, Any], profile_key: str, fallback: str = "") -> str:
    if not profile_key:
        return fallback
    profiles = registry_data.get("orchestration", {}).get("routing", {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return fallback
    profile = profiles.get(profile_key, {})
    if not isinstance(profile, dict):
        return fallback
    return str(profile.get("model", fallback))


def _admission_registry_data(registry_data: dict[str, Any]) -> dict[str, Any]:
    admission = registry_data.get("orchestration", {}).get("admission", {})
    return admission if isinstance(admission, dict) else {}


def _admission_downgrade_model(registry_data: dict[str, Any], adm_raw: dict[str, Any]) -> str:
    configured = str(adm_raw.get("downgrade_model", ""))
    profile = str(adm_raw.get("downgrade_profile", ""))
    if not profile:
        profile = str(_admission_registry_data(registry_data).get("downgrade", {}).get("profile", "fast"))
    default = configured or _registry_profile_model(registry_data, profile, "")
    return _env("admission", "downgrade_model", default)


def _admission_downgrade_backend(registry_data: dict[str, Any], adm_raw: dict[str, Any]) -> str:
    default = str(
        adm_raw.get(
            "downgrade_backend",
            _admission_registry_data(registry_data).get("downgrade", {}).get("backend", "llama_cpp_fast"),
        )
    )
    return _env("admission", "downgrade_backend", default)


def _routing_policy_from_registry_or_toml(
    registry_data: dict[str, Any],
    rp_raw: dict[str, Any],
) -> RoutingPolicyConfig | None:
    registry_policy = _admission_registry_data(registry_data).get("routing_policy", {})
    if not isinstance(registry_policy, dict):
        registry_policy = {}
    source = rp_raw if rp_raw else registry_policy
    if not source:
        return None

    default_profile = str(source.get("default_profile", registry_policy.get("default_profile", "default")))
    default_model = str(source.get("default_model", "")) or _registry_profile_model(registry_data, default_profile, "")
    default_backend = str(source.get("default_backend", registry_policy.get("default_backend", "vllm")))

    rules_source = source.get("rules", registry_policy.get("rules", []))
    rp_rules: list[RoutingPolicyRule] = []
    for rule in rules_source:
        if not isinstance(rule, dict):
            continue
        task_type = str(rule.get("task_type", ""))
        backend = str(rule.get("backend", ""))
        profile = str(rule.get("profile", ""))
        model = str(rule.get("model", "")) or _registry_profile_model(registry_data, profile, "")
        if not task_type:
            raise ValueError("Each [[routing_policy.rules]] entry must have a 'task_type'")
        if not backend:
            raise ValueError("Each [[routing_policy.rules]] entry must have a 'backend'")
        if not model:
            raise ValueError("Each [[routing_policy.rules]] entry must have a 'model' or 'profile'")
        rp_rules.append(RoutingPolicyRule(task_type=task_type, backend=backend, model=model))

    return RoutingPolicyConfig(
        default_backend=default_backend,
        default_model=default_model,
        rules=tuple(rp_rules),
    )


def _parse_dispatch(raw: dict) -> DispatchConfig:
    """Parse [dispatch] from raw TOML (config/orc/agents.toml)."""
    d = raw.get("dispatch", {})
    if not d:
        raise ValueError("[dispatch] section is required in config/orc/agents.toml")

    st = d.get("streaming", {})
    if not st:
        raise ValueError("[dispatch.streaming] section is required in config/orc/agents.toml")
    sp_raw = st.get("source_priority", {})
    if not sp_raw:
        raise ValueError(
            "[dispatch.streaming.source_priority] section is required in config/orc/agents.toml"
        )
    streaming = DispatchStreamingConfig(
        max_context_budget_tokens=_derived_value(
            st,
            "max_context_budget_tokens",
            _DERIVED_DISPATCH_STREAMING_DEFAULTS,
            "dispatch_streaming",
        ),
        max_tokens=_derived_value(
            st,
            "max_tokens",
            _DERIVED_DISPATCH_STREAMING_DEFAULTS,
            "dispatch_streaming",
        ),
        temperature=_require(st, "temperature", "dispatch.streaming"),
        timeout_seconds=_derived_value(
            st,
            "timeout_seconds",
            _DERIVED_DISPATCH_STREAMING_DEFAULTS,
            "dispatch_streaming",
        ),
        unranked_source_priority=_require(st, "unranked_source_priority", "dispatch.streaming"),
        source_priority={str(k): int(v) for k, v in sp_raw.items()},
    )

    sm_raw = d.get("source_map", {})
    if not sm_raw:
        raise ValueError("[dispatch.source_map] section is required in config/orc/agents.toml")
    source_map: dict[str, FeatureEndpointMapping] = {}
    for source, entry in sm_raw.items():
        source_map[str(source)] = FeatureEndpointMapping(
            feature=_require(entry, "feature", f"dispatch.source_map.{source}"),
            method=_require(entry, "method", f"dispatch.source_map.{source}"),
            path=_require(entry, "path", f"dispatch.source_map.{source}"),
            policy_action=str(entry.get("policy_action") or ""),
        )

    fe_raw = d.get("feature_endpoints", {})
    if not fe_raw:
        raise ValueError(
            "[dispatch.feature_endpoints] section is required in config/orc/agents.toml"
        )
    feature_endpoints: dict[str, DispatchFeatureEndpoint] = {}
    for feature, entry in fe_raw.items():
        feature_endpoints[str(feature)] = DispatchFeatureEndpoint(
            method=_require(entry, "method", f"dispatch.feature_endpoints.{feature}"),
            path=_require(entry, "path", f"dispatch.feature_endpoints.{feature}"),
            auth_profile=str(entry.get("auth_profile") or "internal_api"),
            tls_alias_profile=str(entry.get("tls_alias_profile") or ""),
            policy_action=str(entry.get("policy_action") or ""),
        )

    ae_raw = d.get("agent_endpoints", {})
    if not ae_raw:
        raise ValueError("[dispatch.agent_endpoints] section is required in config/orc/agents.toml")
    agent_endpoints: dict[str, tuple[str, str]] = {}
    for agent, entry in ae_raw.items():
        agent_endpoints[str(agent)] = (
            _require(entry, "method", f"dispatch.agent_endpoints.{agent}"),
            _require(entry, "path", f"dispatch.agent_endpoints.{agent}"),
        )

    at_raw = d.get("agent_timeouts", {})
    agent_timeouts = {
        str(agent): float(timeout)
        for agent, timeout in {**_DERIVED_DISPATCH_AGENT_TIMEOUTS, **at_raw}.items()
    }
    for agent, timeout in list(agent_timeouts.items()):
        env_key = f"ORC_DISPATCH_AGENT_TIMEOUT_{agent.upper()}"
        agent_timeouts[agent] = float(_env_key(env_key, timeout))

    return DispatchConfig(
        context_budget_tokens=_derived_value(
            d, "context_budget_tokens", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        context_timeout_per_source=_derived_value(
            d, "context_timeout_per_source", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        feature_budget_tokens=_derived_value(
            d, "feature_budget_tokens", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        feature_timeout_seconds=_derived_value(
            d, "feature_timeout_seconds", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        context_parallel_max_workers=_derived_value(
            d, "context_parallel_max_workers", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        agent_budget_tokens=_derived_value(
            d, "agent_budget_tokens", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        agent_timeout_seconds=_derived_value(
            d, "agent_timeout_seconds", _DERIVED_DISPATCH_DEFAULTS, "dispatch"
        ),
        streaming=streaming,
        source_map=source_map,
        feature_endpoints=feature_endpoints,
        agent_endpoints=agent_endpoints,
        agent_timeouts=agent_timeouts,
    )


def _parse_collaboration(raw: dict) -> CollaborationConfig:
    """Parse [agents.collaboration] from raw TOML."""
    agents_raw = raw.get("agents", {})
    c = agents_raw.get("collaboration", {})
    if not c:
        raise ValueError(
            "[agents.collaboration] section is required in config/orc/agents.toml"
        )
    return CollaborationConfig(
        enabled=_env("agents_collaboration", "enabled", _require(c, "enabled", "agents.collaboration")),
        max_rounds=_derived_value(
            c,
            "max_rounds",
            _DERIVED_COLLABORATION_DEFAULTS,
            "agents_collaboration",
        ),
        round_timeout_seconds=_derived_value(
            c,
            "round_timeout_seconds",
            _DERIVED_COLLABORATION_DEFAULTS,
            "agents_collaboration",
        ),
        max_memory_entries=_derived_value(
            c,
            "max_memory_entries",
            _DERIVED_COLLABORATION_DEFAULTS,
            "agents_collaboration",
        ),
        memory_ttl_seconds=_derived_value(
            c,
            "memory_ttl_seconds",
            _DERIVED_COLLABORATION_DEFAULTS,
            "agents_collaboration",
        ),
    )


def load_settings() -> Settings:
    raw = _load_toml()
    if not raw:
        raise ValueError(
            "No configuration found. Ensure config/orc/*.toml files exist. "
            "Set AI_ORC_SETTINGS_DIR env var or place config at <workspace>/config/orc/"
        )

    o = raw.get("symbiont", {})
    api_key = _env("symbiont", "api_key", o.get("api_key", ""))
    if not api_key:
        raise ValueError(
            "API key is required. Set ORC_SYMBIONT_API_KEY in your environment "
            "or api_key in config/orc/server.toml.\n"
            "Generate one with: openssl rand -hex 32"
        )
    symbiont = SymbiontConfig(
        host=_derived_section_value(o, "host", _DERIVED_SYMBIONT_DEFAULTS, "symbiont"),
        port=_derived_section_value(o, "port", _DERIVED_SYMBIONT_DEFAULTS, "symbiont"),
        api_key=api_key,
    )

    r = raw.get("rag", {})
    rag_url = (
        _first_env("ORC_SERVICES_RAG_URL", "ORC_RAG_URL", "RAG_URL")
        or str(r.get("url", "https://rag:8484")).rstrip("/")
    )
    rag = RAGConfig(
        url=_require_https_url(rag_url, "rag.url"),
        timeout=_env("rag", "timeout", r.get("timeout", 30)),
        health_interval=_env("rag", "health_interval", r.get("health_interval", 30)),
        circuit_breaker_threshold=_env("rag", "circuit_breaker_threshold", r.get("circuit_breaker_threshold", 3)),
        circuit_breaker_reset=_env("rag", "circuit_breaker_reset", r.get("circuit_breaker_reset", 60)),
    )

    ol = raw.get("ollama", {})
    ollama = OllamaConfig(
        base_url=_ollama_base_url(str(ol.get("base_url", ""))),
        max_concurrent_llm=_derived_section_value(ol, "max_concurrent_llm", _DERIVED_OLLAMA_DEFAULTS, "ollama"),
    )

    # Services: feature/agent microservice URLs.
    # Detailed TOML entries are replaced by central generated env and internal
    # registry defaults now define the normal Docker service topology.
    sv = raw.get("services", {})
    services = ServicesConfig(
        reasoning_and_response_url=_service_url(sv, "reasoning_and_response_url"),
        research_url=_service_url(sv, "research_url"),
        personal_context_url=_service_url(sv, "personal_context_url"),
        local_evidence_operator_url=_service_url(sv, "local_evidence_operator_url"),
        execution_policy_operator_url=_service_url(sv, "execution_policy_operator_url"),
        material_builder_url=_service_url(sv, "material_builder_url"),
        material_execution_kernel_url=_service_url(sv, "material_execution_kernel_url"),
        workspace_execution_url=_service_url(sv, "workspace_execution_url"),
        extrator_url=_service_url(sv, "extrator_url"),
        audio_transcribe_url=_service_url(sv, "audio_transcribe_url"),
        audio_streaming_url=_service_url(sv, "audio_streaming_url"),
        storage_guardian_url=_service_url(sv, "storage_guardian_url"),
        translation_url=_service_url(sv, "translation_url"),
        langfuse_url=_service_url(sv, "langfuse_url"),
        clickhouse_url=_service_url(sv, "clickhouse_url"),
        otel_endpoint=_service_url(sv, "otel_endpoint"),
    )

    # Models: 100% centralized in models.json
    from orchestrator.registry import get_registry
    reg = get_registry()
    m = raw.get("models", {})
    models = ModelsConfig(
        default=_env("models", "default", m.get("default", reg.get_model_for_key("default"))),
        fast=_env("models", "fast", m.get("fast", reg.get_model_for_key("fast"))),
        code=_env("models", "code", m.get("code", reg.get_model_for_key("code"))),
        deep=_env("models", "deep", m.get("deep", reg.get_model_for_key("deep"))),
        embedding=_env("models", "embedding", m.get("embedding", reg.get_model_for_key("embedding"))),
    )

    cx = raw.get("context", {})
    cag_raw = cx.get("cag", {})
    cag = CAGConfig(
        db_path=_env("context", "cag_db_path", cag_raw.get("db_path", _DERIVED_CONTEXT_CAG_DEFAULTS["db_path"])),
    )
    context = ContextConfig(
        token_budget=_derived_section_value(cx, "token_budget", _DERIVED_CONTEXT_DEFAULTS, "context"),
        provider_timeout=_derived_section_value(cx, "provider_timeout", _DERIVED_CONTEXT_DEFAULTS, "context"),
        cag=cag,
    )

    rp = raw.get("repos", {})
    raw_paths = _derived_section_value(rp, "paths", _DERIVED_REPOS_DEFAULTS, "repos")
    if isinstance(raw_paths, str):
        raw_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
    repos = ReposConfig(
        paths=tuple(_resolve_path(p) for p in raw_paths),
    )

    g = raw.get("graph", {})
    graph = GraphConfig(
        output_dir=_resolve_path(_derived_section_value(g, "output_dir", _DERIVED_GRAPH_DEFAULTS, "graph")),
        cache_ttl=_derived_section_value(g, "cache_ttl", _DERIVED_GRAPH_DEFAULTS, "graph"),
    )

    s = raw.get("security", {})
    if not s:
        raise ValueError("[security] section missing from infra/security/orchestrator.toml")
    security = SecurityConfig(
        allowed_commands=frozenset(_require(s, "allowed_commands", "security")),
        max_command_timeout=_derived_section_value(s, "max_command_timeout", _DERIVED_SECURITY_DEFAULTS, "security"),
        injection_scanning=_env("security", "injection_scanning", _require(s, "injection_scanning", "security")),
        agent_sandboxing=_env("security", "agent_sandboxing", _require(s, "agent_sandboxing", "security")),
        secrets_scanning=_env("security", "secrets_scanning", _require(s, "secrets_scanning", "security")),
        rate_limiting=_derived_section_value(s, "rate_limiting", _DERIVED_SECURITY_DEFAULTS, "security"),
        audit_trail=_env("security", "audit_trail", _require(s, "audit_trail", "security")),
        opa_shadow_enabled=_derived_section_value(s, "opa_shadow_enabled", _DERIVED_SECURITY_DEFAULTS, "security"),
        opa_enforce_enabled=_derived_section_value(s, "opa_enforce_enabled", _DERIVED_SECURITY_DEFAULTS, "security"),
        injection_block_threshold=_derived_section_value(
            s, "injection_block_threshold", _DERIVED_SECURITY_DEFAULTS, "security"
        ),
        per_agent_token_budget=_env("security", "per_agent_token_budget", _require(s, "per_agent_token_budget", "security")),
        rate_limit_calls_per_minute=_derived_section_value(
            s, "rate_limit_calls_per_minute", _DERIVED_SECURITY_DEFAULTS, "security"
        ),
    )

    lg = raw.get("logging", {})
    if not lg:
        raise ValueError("[logging] section missing from config/orc/server.toml")
    logging_cfg = LoggingConfig(
        level=_env("logging", "level", _require(lg, "level", "logging")),
        format=_env("logging", "format", _require(lg, "format", "logging")),
    )

    se = raw.get("session", {})
    if not se:
        raise ValueError("[session] section missing from config/orc/server.toml")
    session = SessionConfig(
        enabled=_env("session", "enabled", _require(se, "enabled", "session")),
        ttl_seconds=_derived_section_value(se, "ttl_seconds", _DERIVED_SESSION_DEFAULTS, "session"),
        db_path=_derived_section_value(se, "db_path", _DERIVED_SESSION_DEFAULTS, "session"),
        max_messages=_env("session", "max_messages", _require(se, "max_messages", "session")),
        cli_default_session=_env("session", "cli_default_session", _require(se, "cli_default_session", "session")),
        cli_session_state_file=_derived_section_value(
            se, "cli_session_state_file", _DERIVED_SESSION_DEFAULTS, "session"
        ),
    )

    cl = raw.get("classify", {})
    if not cl:
        raise ValueError("[classify] section missing from config/orc/agents.toml")
    classify = ClassifyConfig(
        confidence_threshold=_derived_section_value(
            cl, "confidence_threshold", _DERIVED_CLASSIFY_DEFAULTS, "classify"
        ),
        history_aware_threshold=_derived_section_value(
            cl, "history_aware_threshold", _DERIVED_CLASSIFY_DEFAULTS, "classify"
        ),
        history_window=_derived_section_value(cl, "history_window", _DERIVED_CLASSIFY_DEFAULTS, "classify"),
        anaphora_confidence_penalty=_derived_section_value(
            cl, "anaphora_confidence_penalty", _DERIVED_CLASSIFY_DEFAULTS, "classify"
        ),
        general_followup_penalty=_derived_section_value(
            cl, "general_followup_penalty", _DERIVED_CLASSIFY_DEFAULTS, "classify"
        ),
        general_followup_max_words=_derived_section_value(
            cl, "general_followup_max_words", _DERIVED_CLASSIFY_DEFAULTS, "classify"
        ),
        anaphora_words=frozenset(str(w).lower() for w in _require(cl, "anaphora_words", "classify")),
        anaphora_patterns=tuple(str(p).lower() for p in _require(cl, "anaphora_patterns", "classify")),
    )

    # Agentic config: loaded from TOML [agentic] section (previously from models.json)
    ag = raw.get("agentic", {})
    if not ag:
        raise ValueError("[agentic] section missing from config/orc/agents.toml")
    agentic = AgenticConfig(
        enabled=_env("agentic", "enabled", _require(ag, "enabled", "agentic")),
        max_iterations=_derived_section_value(ag, "max_iterations", _DERIVED_AGENTIC_DEFAULTS, "agentic"),
        max_tool_calls=_derived_section_value(ag, "max_tool_calls", _DERIVED_AGENTIC_DEFAULTS, "agentic"),
        timeout=_derived_section_value(ag, "timeout", _DERIVED_AGENTIC_DEFAULTS, "agentic"),
        token_budget=_derived_section_value(ag, "token_budget", _DERIVED_AGENTIC_DEFAULTS, "agentic"),
    )

    ar = raw.get("agentic_runtime", {})
    if not ar:
        raise ValueError("[agentic_runtime] section missing from config/orc/agents.toml")
    agentic_runtime = AgenticRuntimeConfig(
        enabled=_env("agentic_runtime", "enabled", _require(ar, "enabled", "agentic_runtime")),
        shadow_ledger_enabled=_env(
            "agentic_runtime",
            "shadow_ledger_enabled",
            _require(ar, "shadow_ledger_enabled", "agentic_runtime"),
        ),
        default_mode=_env("agentic_runtime", "default_mode", _require(ar, "default_mode", "agentic_runtime")),
        autonomous_safe_enabled=_env(
            "agentic_runtime",
            "autonomous_safe_enabled",
            _require(ar, "autonomous_safe_enabled", "agentic_runtime"),
        ),
        policy_mode=_env("agentic_runtime", "policy_mode", _require(ar, "policy_mode", "agentic_runtime")),
        db_path=_derived_section_value(ar, "db_path", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"),
        approval_ttl_seconds=_derived_section_value(
            ar, "approval_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        recover_on_startup=_env(
            "agentic_runtime",
            "recover_on_startup",
            _require(ar, "recover_on_startup", "agentic_runtime"),
        ),
        runner_enabled=_env("agentic_runtime", "runner_enabled", _require(ar, "runner_enabled", "agentic_runtime")),
        runner_poll_interval_seconds=_derived_section_value(
            ar, "runner_poll_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        max_concurrent_tasks=_derived_section_value(
            ar, "max_concurrent_tasks", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        runner_execute_proposals=_env(
            "agentic_runtime",
            "runner_execute_proposals",
            _require(ar, "runner_execute_proposals", "agentic_runtime"),
        ),
        task_default_timeout_seconds=_derived_section_value(
            ar, "task_default_timeout_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        material_decision_timeout_seconds=_derived_section_value(
            ar, "material_decision_timeout_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        task_max_retries=_derived_section_value(
            ar, "task_max_retries", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        event_loop_enabled=_env(
            "agentic_runtime",
            "event_loop_enabled",
            _require(ar, "event_loop_enabled", "agentic_runtime"),
        ),
        event_loop_poll_interval_seconds=_derived_section_value(
            ar, "event_loop_poll_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        event_loop_min_repeat_interval_seconds=_derived_section_value(
            ar, "event_loop_min_repeat_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        event_loop_vllm_unhealthy_enabled=_env(
            "agentic_runtime",
            "event_loop_vllm_unhealthy_enabled",
            _require(ar, "event_loop_vllm_unhealthy_enabled", "agentic_runtime"),
        ),
        autonomous_maintenance_enabled=_env(
            "agentic_runtime",
            "autonomous_maintenance_enabled",
            _require(ar, "autonomous_maintenance_enabled", "agentic_runtime"),
        ),
        governed_improvement_enabled=_env(
            "agentic_runtime",
            "governed_improvement_enabled",
            _require(ar, "governed_improvement_enabled", "agentic_runtime"),
        ),
        actuator_enabled=_env(
            "agentic_runtime",
            "actuator_enabled",
            _require(ar, "actuator_enabled", "agentic_runtime"),
        ),
        actuator_poll_interval_seconds=_derived_section_value(
            ar, "actuator_poll_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_auto_apply_runtime_flags=_env(
            "agentic_runtime",
            "actuator_auto_apply_runtime_flags",
            _require(ar, "actuator_auto_apply_runtime_flags", "agentic_runtime"),
        ),
        actuator_max_auto_ttl_seconds=_derived_section_value(
            ar, "actuator_max_auto_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_min_confidence=_derived_section_value(
            ar, "actuator_min_confidence", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_min_score=_derived_section_value(
            ar, "actuator_min_score", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_impact_interval_seconds=_derived_section_value(
            ar, "actuator_impact_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_bypass_failure_threshold=_derived_section_value(
            ar, "actuator_bypass_failure_threshold", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_closed_loop_enabled=_env(
            "agentic_runtime",
            "actuator_closed_loop_enabled",
            _require(ar, "actuator_closed_loop_enabled", "agentic_runtime"),
        ),
        actuator_renew_enforced_flags=_env(
            "agentic_runtime",
            "actuator_renew_enforced_flags",
            _require(ar, "actuator_renew_enforced_flags", "agentic_runtime"),
        ),
        actuator_renewal_window_seconds=_derived_section_value(
            ar, "actuator_renewal_window_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_max_renewals=_derived_section_value(
            ar, "actuator_max_renewals", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_attention_ttl_seconds=_derived_section_value(
            ar, "actuator_attention_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_auto_rollback_missing_flags=_env(
            "agentic_runtime",
            "actuator_auto_rollback_missing_flags",
            _require(ar, "actuator_auto_rollback_missing_flags", "agentic_runtime"),
        ),
        actuator_escalation_ladder_enabled=_env(
            "agentic_runtime",
            "actuator_escalation_ladder_enabled",
            _require(ar, "actuator_escalation_ladder_enabled", "agentic_runtime"),
        ),
        actuator_escalation_window_seconds=_derived_section_value(
            ar, "actuator_escalation_window_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_escalation_l2_threshold=_derived_section_value(
            ar, "actuator_escalation_l2_threshold", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_escalation_l3_threshold=_derived_section_value(
            ar, "actuator_escalation_l3_threshold", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_escalation_flag_ttl_seconds=_derived_section_value(
            ar, "actuator_escalation_flag_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        actuator_escalation_create_proposals=_env(
            "agentic_runtime",
            "actuator_escalation_create_proposals",
            _require(ar, "actuator_escalation_create_proposals", "agentic_runtime"),
        ),
        actuator_escalation_policy_router_enabled=_env(
            "agentic_runtime",
            "actuator_escalation_policy_router_enabled",
            _require(ar, "actuator_escalation_policy_router_enabled", "agentic_runtime"),
        ),
        actuator_escalation_policy_router_create_proposals=_env(
            "agentic_runtime",
            "actuator_escalation_policy_router_create_proposals",
            _require(ar, "actuator_escalation_policy_router_create_proposals", "agentic_runtime"),
        ),
        actuator_escalation_route_flag_ttl_seconds=_derived_section_value(
            ar, "actuator_escalation_route_flag_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        preapproval_windows_enabled=_env(
            "agentic_runtime",
            "preapproval_windows_enabled",
            _require(ar, "preapproval_windows_enabled", "agentic_runtime"),
        ),
        preapproval_window_default_ttl_seconds=_derived_section_value(
            ar, "preapproval_window_default_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        preapproval_window_max_ttl_seconds=_derived_section_value(
            ar, "preapproval_window_max_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        preapproval_window_max_uses=_derived_section_value(
            ar, "preapproval_window_max_uses", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        preapproval_window_allowed_actions=_env(
            "agentic_runtime",
            "preapproval_window_allowed_actions",
            _require(ar, "preapproval_window_allowed_actions", "agentic_runtime"),
        ),
        command_tool_enabled=_env(
            "agentic_runtime",
            "command_tool_enabled",
            _require(ar, "command_tool_enabled", "agentic_runtime"),
        ),
        command_tool_backend=_env(
            "agentic_runtime",
            "command_tool_backend",
            _require(ar, "command_tool_backend", "agentic_runtime"),
        ),
        command_tool_default_context_profile=_env(
            "agentic_runtime",
            "command_tool_default_context_profile",
            _require(ar, "command_tool_default_context_profile", "agentic_runtime"),
        ),
        command_tool_sandbox_image=_env(
            "agentic_runtime",
            "command_tool_sandbox_image",
            _require(ar, "command_tool_sandbox_image", "agentic_runtime"),
        ),
        command_tool_timeout_seconds=_env(
            "agentic_runtime",
            "command_tool_timeout_seconds",
            ar.get(
                "command_tool_timeout_seconds",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_timeout_seconds"],
            ),
        ),
        command_tool_max_output_bytes=_env(
            "agentic_runtime",
            "command_tool_max_output_bytes",
            ar.get(
                "command_tool_max_output_bytes",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_max_output_bytes"],
            ),
        ),
        command_tool_session_ttl_seconds=_env(
            "agentic_runtime",
            "command_tool_session_ttl_seconds",
            ar.get(
                "command_tool_session_ttl_seconds",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_session_ttl_seconds"],
            ),
        ),
        command_tool_max_commands_per_session=_env(
            "agentic_runtime",
            "command_tool_max_commands_per_session",
            ar.get(
                "command_tool_max_commands_per_session",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_max_commands_per_session"],
            ),
        ),
        command_tool_docker_memory_limit_mb=_env(
            "agentic_runtime",
            "command_tool_docker_memory_limit_mb",
            ar.get(
                "command_tool_docker_memory_limit_mb",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_docker_memory_limit_mb"],
            ),
        ),
        command_tool_docker_pids_limit=_env(
            "agentic_runtime",
            "command_tool_docker_pids_limit",
            ar.get(
                "command_tool_docker_pids_limit",
                _DERIVED_AGENTIC_COMMAND_TOOL_DEFAULTS["command_tool_docker_pids_limit"],
            ),
        ),
        command_tool_allow_user_context_ro=_env(
            "agentic_runtime",
            "command_tool_allow_user_context_ro",
            _require(ar, "command_tool_allow_user_context_ro", "agentic_runtime"),
        ),
        command_tool_allow_host_context_ro=_env(
            "agentic_runtime",
            "command_tool_allow_host_context_ro",
            _require(ar, "command_tool_allow_host_context_ro", "agentic_runtime"),
        ),
        maintenance_report_interval_seconds=_derived_section_value(
            ar, "maintenance_report_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        improvement_review_interval_seconds=_derived_section_value(
            ar, "improvement_review_interval_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        agent_failure_threshold=_derived_section_value(
            ar, "agent_failure_threshold", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        agent_failure_window_seconds=_derived_section_value(
            ar, "agent_failure_window_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        rag_miss_threshold=_derived_section_value(
            ar, "rag_miss_threshold", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        rag_miss_window_seconds=_derived_section_value(
            ar, "rag_miss_window_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
        runtime_flag_ttl_seconds=_derived_section_value(
            ar, "runtime_flag_ttl_seconds", _DERIVED_AGENTIC_RUNTIME_DEFAULTS, "agentic_runtime"
        ),
    )

    # Calendar
    cal = raw.get("calendar", {})
    cal_paths = _derived_section_value(cal, "ics_paths", _DERIVED_CALENDAR_DEFAULTS, "calendar")
    if isinstance(cal_paths, str):
        cal_paths = [cal_paths]
    calendar = CalendarConfig(
        enabled=_derived_section_value(cal, "enabled", _DERIVED_CALENDAR_DEFAULTS, "calendar"),
        ics_paths=tuple(cal_paths),
        window_days=_derived_section_value(cal, "window_days", _DERIVED_CALENDAR_DEFAULTS, "calendar"),
    )

    # RSS
    rs = raw.get("rss", {})
    rss_feeds = _derived_section_value(rs, "feeds", _DERIVED_RSS_DEFAULTS, "rss")
    if isinstance(rss_feeds, str):
        rss_feeds = [item.strip() for item in rss_feeds.split(",") if item.strip()]
    rss = RSSConfig(
        enabled=_derived_section_value(rs, "enabled", _DERIVED_RSS_DEFAULTS, "rss"),
        feeds=tuple(rss_feeds),
        cache_ttl=_derived_section_value(rs, "cache_ttl", _DERIVED_RSS_DEFAULTS, "rss"),
        max_entries=_derived_section_value(rs, "max_entries", _DERIVED_RSS_DEFAULTS, "rss"),
        cache_dir=_derived_section_value(rs, "cache_dir", _DERIVED_RSS_DEFAULTS, "rss"),
    )

    # Email
    em = raw.get("email", {})
    em_paths = _derived_section_value(em, "mailbox_paths", _DERIVED_EMAIL_DEFAULTS, "email")
    if isinstance(em_paths, str):
        em_paths = [em_paths]
    email_cfg = EmailConfig(
        enabled=_derived_section_value(em, "enabled", _DERIVED_EMAIL_DEFAULTS, "email"),
        mailbox_paths=tuple(em_paths),
        max_age_days=_derived_section_value(em, "max_age_days", _DERIVED_EMAIL_DEFAULTS, "email"),
        max_results=_derived_section_value(em, "max_results", _DERIVED_EMAIL_DEFAULTS, "email"),
    )

    # ---------------------------------------------------------------------------
    # v0.7 — Multi-backend LLM config
    # ---------------------------------------------------------------------------
    llm_raw = raw.get("llm", {})
    backends_raw = llm_raw.get("backends", [])

    # Latency routing (parsed before LLMConfig so the router can read it)
    lr_raw = llm_raw.get("latency_routing", {})
    latency_routing = LatencyRoutingConfig(
        enabled=_derived_section_value(lr_raw, "enabled", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"),
        p95_threshold_ms=_derived_section_value(
            lr_raw, "p95_threshold_ms", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        prefer_warm=_derived_section_value(
            lr_raw, "prefer_warm", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        simple_task_max_latency_ms=_derived_section_value(
            lr_raw, "simple_task_max_latency_ms", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        max_latency_samples=_derived_section_value(
            lr_raw, "max_latency_samples", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        priority_weight=_derived_section_value(
            lr_raw, "priority_weight", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        p95_penalty_per_second=_derived_section_value(
            lr_raw, "p95_penalty_per_second", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
        warm_bonus=_derived_section_value(
            lr_raw, "warm_bonus", _DERIVED_LLM_LATENCY_ROUTING_DEFAULTS, "llm_latency_routing"
        ),
    )

    if not backends_raw:
        _auto_backends = _registry_llm_backends(reg.data, llm_raw=llm_raw, models=models, ollama=ollama)
    else:
        _parsed: list[BackendConfig] = []
        _seen: set[str] = set()
        for b in backends_raw:
            _bname = b.get("name", "")
            if not _bname:
                raise ValueError("Each [[llm.backends]] entry must have a 'name'")
            if _bname in _seen:
                raise ValueError(f"Duplicate backend name in [[llm.backends]]: {_bname!r}")
            _seen.add(_bname)
            _enabled = _llm_backend_enabled(_bname, bool(b.get("enabled", True)))
            _burl = _llm_backend_base_url(_bname, str(b.get("base_url", "")), enabled=_enabled)
            _parsed.append(BackendConfig(
                name=_bname,
                base_url=_burl,
                api_key_env=b.get("api_key_env", ""),
                priority=b.get("priority", 10),
                enabled=_enabled,
                models=tuple(b.get("models", [])),
                capabilities=tuple(b.get("capabilities", [])),
                privacy_level=b.get("privacy_level", "local"),
                request_timeout=_derived_backend_timeout(_bname, b, llm_raw, "request_timeout"),
                stream_timeout=_derived_backend_timeout(_bname, b, llm_raw, "stream_timeout"),
            ))
        _auto_backends = tuple(_parsed)

    # Security hardening: Ollama backend always requires an explicit API key.
    for backend in _auto_backends:
        if not backend.enabled:
            continue
        if backend.name != "ollama":
            continue
        key_env = backend.api_key_env or "OLLAMA_API_KEY"
        key_val = os.environ.get(key_env, "").strip()
        if not key_val:
            raise ValueError(
                "OLLAMA_API_KEY is required when Ollama backend is enabled. "
                f"Set {key_env} via environment/secrets before starting the symbiont."
            )

    _parsed_profiles: list[ModelProfileConfig] = []
    profile_overrides = llm_raw.get("model_profiles", [])
    if profile_overrides:
        for p in profile_overrides:
            _palias = p.get("alias", "")
            if not _palias:
                raise ValueError("Each [[llm.model_profiles]] entry must have an 'alias'")
            _parsed_profiles.append(ModelProfileConfig(
                alias=_palias,
                preferred_models=tuple(p.get("preferred_models", [])),
                fallback_model=p.get("fallback_model", ""),
                required_capabilities=tuple(p.get("required_capabilities", [])),
                enabled=p.get("enabled", True),
            ))
    else:
        _parsed_profiles = list(_registry_model_profiles(reg.data))

    llm_config = LLMConfig(
        default_model=_env("llm", "default_model", llm_raw.get("default_model", models.default)),
        routing_strategy=_derived_section_value(llm_raw, "routing_strategy", _DERIVED_LLM_DEFAULTS, "llm"),
        fallback_enabled=_derived_section_value(llm_raw, "fallback_enabled", _DERIVED_LLM_DEFAULTS, "llm"),
        health_cache_seconds=_derived_section_value(llm_raw, "health_cache_seconds", _DERIVED_LLM_DEFAULTS, "llm"),
        request_timeout_seconds=_derived_section_value(llm_raw, "request_timeout_seconds", _DERIVED_LLM_DEFAULTS, "llm"),
        stream_timeout_seconds=_derived_section_value(llm_raw, "stream_timeout_seconds", _DERIVED_LLM_DEFAULTS, "llm"),
        backends=_auto_backends,
        model_profiles=tuple(_parsed_profiles),
    )

    # ---------------------------------------------------------------------------
    # Performance & Inference Profiles
    # ---------------------------------------------------------------------------
    perf_raw = llm_raw.get("performance", {})
    _primary_warm = _derived_section_value(
        perf_raw, "primary_warm_models", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"
    )
    _fallback_warm = _derived_section_value(
        perf_raw, "fallback_warm_models", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"
    )
    performance = PerformanceConfig(
        warmup_enabled=_derived_section_value(
            perf_raw, "warmup_enabled", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"
        ),
        warmup_on_startup=_derived_section_value(
            perf_raw, "warmup_on_startup", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"
        ),
        keep_alive=_derived_section_value(perf_raw, "keep_alive", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"),
        max_loaded_models=_derived_section_value(
            perf_raw, "max_loaded_models", _DERIVED_LLM_PERFORMANCE_DEFAULTS, "llm_performance"
        ),
        primary_warm_models=tuple(_primary_warm),
        fallback_warm_models=tuple(_fallback_warm),
    )

    # Pipeline parallelism settings: [llm.performance.pipeline]
    pipe_raw = perf_raw.get("pipeline", {})
    pipeline = PipelineConfig(
        speculative_prefetch=_derived_section_value(
            pipe_raw, "speculative_prefetch", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"
        ),
        batch_inference=_derived_section_value(pipe_raw, "batch_inference", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"),
        async_providers=_derived_section_value(pipe_raw, "async_providers", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"),
        connection_pool_size=_derived_section_value(
            pipe_raw, "connection_pool_size", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"
        ),
        keepalive_expiry=_derived_section_value(pipe_raw, "keepalive_expiry", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"),
        http2_enabled=_derived_section_value(pipe_raw, "http2_enabled", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"),
        streaming_events=_derived_section_value(pipe_raw, "streaming_events", _DERIVED_LLM_PIPELINE_DEFAULTS, "llm_pipeline"),
    )

    # v1.6 — Intelligent pipeline: [pipeline.intelligent]
    intel_raw = raw.get("pipeline", {}).get("intelligent", {})
    intelligent_pipeline = IntelligentPipelineConfig(
        smart_retry_enabled=_derived_section_value(
            intel_raw, "smart_retry_enabled", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        max_retries_per_agent=_derived_section_value(
            intel_raw, "max_retries_per_agent", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        retry_confidence_threshold=_derived_section_value(
            intel_raw, "retry_confidence_threshold", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        progressive_refinement_enabled=_derived_section_value(
            intel_raw, "progressive_refinement_enabled", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        early_termination_enabled=_derived_section_value(
            intel_raw, "early_termination_enabled", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        early_termination_confidence=_derived_section_value(
            intel_raw, "early_termination_confidence", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
        dead_path_elimination_enabled=_derived_section_value(
            intel_raw, "dead_path_elimination_enabled", _DERIVED_INTELLIGENT_PIPELINE_DEFAULTS, "pipeline_intelligent"
        ),
    )

    # Inference profiles: [llm.profiles.fast], [llm.profiles.default], etc.
    profiles_raw = llm_raw.get("profiles", {})
    _required_profile_keys = ("fast", "default", "code", "deep")
    inference_profiles: dict[str, InferenceProfileConfig] = {}
    for pkey in _required_profile_keys:
        praw = profiles_raw.get(pkey, {})
        inference_profiles[pkey] = InferenceProfileConfig(
            num_ctx=_derived_llm_profile_value(pkey, praw, "num_ctx"),
            num_predict=_derived_llm_profile_value(pkey, praw, "num_predict"),
            temperature=_derived_llm_profile_value(pkey, praw, "temperature"),
            top_p=_derived_llm_profile_value(pkey, praw, "top_p"),
        )

    # Context budgets: [context_budget.fast], [context_budget.default], [context_budget.deep]
    cb_raw = raw.get("context_budget", {})
    _required_budget_keys = ("fast", "default", "code", "deep")
    context_budgets: dict[str, ContextBudgetConfig] = {}
    for bkey in _required_budget_keys:
        braw = cb_raw.get(bkey, {})
        context_budgets[bkey] = ContextBudgetConfig(
            max_context_tokens=_derived_context_budget_value(bkey, braw, "max_context_tokens"),
            rag_top_k=_derived_context_budget_value(bkey, braw, "rag_top_k"),
            graph_enabled=_derived_context_budget_value(bkey, braw, "graph_enabled"),
            system_snapshot_enabled=str(_derived_context_budget_value(bkey, braw, "system_snapshot_enabled")),
        )

    # Context budget scaling by model context window: [context_budget_scaling]
    cbs_raw = raw.get("context_budget_scaling", {})
    context_budget_scaling = ContextBudgetScalingConfig(
        reference_context_window=_derived_section_value(
            cbs_raw, "reference_context_window", _DERIVED_CONTEXT_BUDGET_SCALING_DEFAULTS, "context_budget_scaling"
        ),
        max_scale=_derived_section_value(
            cbs_raw, "max_scale", _DERIVED_CONTEXT_BUDGET_SCALING_DEFAULTS, "context_budget_scaling"
        ),
        graph_min_context_window=_derived_section_value(
            cbs_raw, "graph_min_context_window", _DERIVED_CONTEXT_BUDGET_SCALING_DEFAULTS, "context_budget_scaling"
        ),
    )

    # Dispatch budgets, timeouts and source routing: [dispatch]
    dispatch = _parse_dispatch(raw)

    # Latency routing
    # (parsed earlier, before LLMConfig — see `latency_routing` above)

    # v1.4 — Escalation config: [llm.escalation]
    esc_raw = llm_raw.get("escalation", {})
    esc_chain = _derived_section_value(esc_raw, "chain", _DERIVED_LLM_ESCALATION_DEFAULTS, "llm_escalation")
    if isinstance(esc_chain, str):
        esc_chain = [m.strip() for m in esc_chain.split(",") if m.strip()]
    escalation = EscalationConfig(
        enabled=_derived_section_value(esc_raw, "enabled", _DERIVED_LLM_ESCALATION_DEFAULTS, "llm_escalation"),
        min_critic_score=_derived_section_value(
            esc_raw, "min_critic_score", _DERIVED_LLM_ESCALATION_DEFAULTS, "llm_escalation"
        ),
        max_escalations=_derived_section_value(
            esc_raw, "max_escalations", _DERIVED_LLM_ESCALATION_DEFAULTS, "llm_escalation"
        ),
        chain=tuple(esc_chain),
    )

    # v1.4 — Capability config: [llm.capability]
    cap_raw = llm_raw.get("capability", {})
    capability = CapabilityConfig(
        enabled=_derived_section_value(cap_raw, "enabled", _DERIVED_LLM_CAPABILITY_DEFAULTS, "llm_capability"),
        probe_on_first_use=_derived_section_value(
            cap_raw, "probe_on_first_use", _DERIVED_LLM_CAPABILITY_DEFAULTS, "llm_capability"
        ),
    )

    # v2.1 — Execution Layer (multi-agent code execution)
    exec_raw = raw.get("execution", {})
    execution_cfg = None
    if exec_raw.get("enabled", False):
        try:
            from orchestrator.execution.config import (
                ExecutionConfig,
                ExecutionGeminiConfig,
                ExecutionResourcesConfig,
            )
            exec_res = exec_raw.get("resources", {})
            if not exec_res:
                raise ValueError("[execution.resources] section missing from config/orc/agents.toml")
            exec_gem = exec_raw.get("gemini", {})
            if not exec_gem:
                raise ValueError("[execution.gemini] section missing from config/orc/agents.toml")
            # Parse blocked_intents and allowed_sources as tuples
            _blocked = _require(exec_raw, "blocked_intents", "execution")
            if isinstance(_blocked, str):
                _blocked = [_blocked]
            _allowed_src = _require(exec_raw, "external_allowed_sources", "execution")
            if isinstance(_allowed_src, str):
                _allowed_src = [_allowed_src]
            execution_cfg = ExecutionConfig(
                enabled=True,
                redis_url=_env("execution", "redis_url", _require(exec_raw, "redis_url", "execution")),
                workspace_path=_env("execution", "workspace_path", _require(exec_raw, "workspace_path", "execution")),
                worker_image=_env("execution", "worker_image", _require(exec_raw, "worker_image", "execution")),
                max_workers_per_execution=_env("execution", "max_workers_per_execution", _require(exec_raw, "max_workers_per_execution", "execution")),
                max_concurrent_executions=_env("execution", "max_concurrent_executions", _require(exec_raw, "max_concurrent_executions", "execution")),
                worker_timeout_seconds=_env("execution", "worker_timeout_seconds", _require(exec_raw, "worker_timeout_seconds", "execution")),
                execution_timeout_seconds=_env("execution", "execution_timeout_seconds", _require(exec_raw, "execution_timeout_seconds", "execution")),
                cleanup_on_completion=_env("execution", "cleanup_on_completion", _require(exec_raw, "cleanup_on_completion", "execution")),
                mock_mode=_env("execution", "mock_mode", _require(exec_raw, "mock_mode", "execution")),
                complexity_threshold=_env("execution", "complexity_threshold", _require(exec_raw, "complexity_threshold", "execution")),
                blocked_intents=tuple(_blocked),
                external_allowed_sources=tuple(_allowed_src),
                require_sanitized_context=_env("execution", "require_sanitized_context", _require(exec_raw, "require_sanitized_context", "execution")),
                bundle_root=_env("execution", "bundle_root", _require(exec_raw, "bundle_root", "execution")),
                mount_repo_directly=_env("execution", "mount_repo_directly", _require(exec_raw, "mount_repo_directly", "execution")),
                workspace_readonly=_env("execution", "workspace_readonly", _require(exec_raw, "workspace_readonly", "execution")),
                require_execution_bundle=_env("execution", "require_execution_bundle", _require(exec_raw, "require_execution_bundle", "execution")),
                resources=ExecutionResourcesConfig(
                    mem_limit=_require(exec_res, "mem_limit", "execution.resources"),
                    cpus=_require(exec_res, "cpus", "execution.resources"),
                    pids_limit=_require(exec_res, "pids_limit", "execution.resources"),
                    network_mode=_require(exec_res, "network_mode", "execution.resources"),
                ),
                gemini=ExecutionGeminiConfig(
                    api_key_env=_require(exec_gem, "api_key_env", "execution.gemini"),
                    model=_require(exec_gem, "model", "execution.gemini"),
                    temperature=_require(exec_gem, "temperature", "execution.gemini"),
                    max_tokens=_require(exec_gem, "max_tokens", "execution.gemini"),
                    auth_mode=_require(exec_gem, "auth_mode", "execution.gemini"),
                    oauth_credentials_path=_require(exec_gem, "oauth_credentials_path", "execution.gemini"),
                ),
            )
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "Execution layer not available (removed). Ignoring [execution] config."
            )

    # ---------------------------------------------------------------------------
    # Container Lifecycle Management
    # ---------------------------------------------------------------------------
    lc_raw = raw.get("lifecycle", {})
    _lc_always_on = _derived_section_value(lc_raw, "always_on", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle")
    if isinstance(_lc_always_on, str):
        _lc_always_on = [_lc_always_on]
    _lc_pre_warm = _derived_section_value(lc_raw, "pre_warm", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle")
    if isinstance(_lc_pre_warm, str):
        _lc_pre_warm = [_lc_pre_warm]
    _lc_compose_profiles = _derived_section_value(
        lc_raw, "compose_profiles", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"
    )
    if isinstance(_lc_compose_profiles, str):
        _lc_compose_profiles = [
            part.strip()
            for part in _lc_compose_profiles.split(",")
            if part.strip()
        ]
    _lc_overrides = lc_raw.get("overrides", {})
    _lc_idle_timeout = _derived_section_value(lc_raw, "idle_timeout", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle")
    _session_idle_timeout = _material_session_idle_timeout(agentic_runtime, int(_lc_idle_timeout))
    container_lifecycle = ContainerLifecycleConfig(
        enabled=_derived_section_value(lc_raw, "enabled", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"),
        idle_timeout=_lc_idle_timeout,
        start_timeout=_derived_section_value(lc_raw, "start_timeout", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"),
        health_poll_interval=_derived_section_value(
            lc_raw, "health_poll_interval", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"
        ),
        idle_check_interval=_derived_section_value(
            lc_raw, "idle_check_interval", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"
        ),
        docker_host=_derived_section_value(lc_raw, "docker_host", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"),
        compose_project=_derived_section_value(lc_raw, "compose_project", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"),
        compose_file=_derived_section_value(lc_raw, "compose_file", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"),
        compose_project_dir=_derived_section_value(
            lc_raw, "compose_project_dir", _DERIVED_LIFECYCLE_DEFAULTS, "lifecycle"
        ),
        compose_profiles=tuple(str(profile) for profile in _lc_compose_profiles),
        always_on=tuple(_lc_always_on),
        pre_warm=tuple(_lc_pre_warm),
        per_service_overrides={
            name: _derived_lifecycle_override(
                name,
                _lc_overrides,
                session_idle_timeout=_session_idle_timeout,
            )
            for name in sorted({*list(_DERIVED_LIFECYCLE_OVERRIDE_DEFAULTS), *[str(key) for key in _lc_overrides]})
        },
    )

    # ---------------------------------------------------------------------------
    # Predictive Prewarming
    # ---------------------------------------------------------------------------
    pw_raw = raw.get("prewarming", {})
    prewarming = PrewarmConfig(
        enabled=_derived_section_value(pw_raw, "enabled", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        max_prewarm_per_request=_derived_section_value(
            pw_raw, "max_prewarm_per_request", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        max_gpu_prewarm_per_request=_derived_section_value(
            pw_raw, "max_gpu_prewarm_per_request", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        high_confidence_threshold=_derived_section_value(
            pw_raw, "high_confidence_threshold", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        medium_confidence_threshold=_derived_section_value(
            pw_raw, "medium_confidence_threshold", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        embedding_model=_derived_section_value(pw_raw, "embedding_model", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        classifier_model=_derived_section_value(
            pw_raw, "classifier_model", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        classifier_timeout_ms=_derived_section_value(
            pw_raw, "classifier_timeout_ms", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        ttl_unused_seconds=_derived_section_value(
            pw_raw, "ttl_unused_seconds", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        catalog_path=_derived_section_value(pw_raw, "catalog_path", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        l1_backend=_derived_section_value(pw_raw, "l1_backend", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        l1_fastembed_model=_derived_section_value(
            pw_raw, "l1_fastembed_model", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        level1_enabled=_derived_section_value(pw_raw, "level1_enabled", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        rule_boost=_derived_section_value(pw_raw, "rule_boost", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        recent_usage_boost=_derived_section_value(
            pw_raw, "recent_usage_boost", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        startup_cost_penalty=_derived_section_value(
            pw_raw, "startup_cost_penalty", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        gpu_pressure_penalty=_derived_section_value(
            pw_raw, "gpu_pressure_penalty", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        already_running_bonus=_derived_section_value(
            pw_raw, "already_running_bonus", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
        level2_enabled=_derived_section_value(pw_raw, "level2_enabled", _DERIVED_PREWARMING_DEFAULTS, "prewarming"),
        level2_ambiguity_gap=_derived_section_value(
            pw_raw, "level2_ambiguity_gap", _DERIVED_PREWARMING_DEFAULTS, "prewarming"
        ),
    )

    # ---------------------------------------------------------------------------
    # Admission Control (optional — system works without it)
    # ---------------------------------------------------------------------------
    adm_raw = raw.get("admission", {})
    admission_cfg: AdmissionConfig | None = None
    if adm_raw or _admission_registry_data(reg.data):
        adm_bc_raw = adm_raw.get("backend_concurrency", {})
        backend_concurrency = dict(_DERIVED_ADMISSION_BACKEND_CONCURRENCY_DEFAULTS)
        backend_concurrency.update({str(k): int(v) for k, v in adm_bc_raw.items()})
        admission_cfg = AdmissionConfig(
            max_concurrent_global=_derived_section_value(
                adm_raw, "max_concurrent_global", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            max_tokens_per_request=_derived_section_value(
                adm_raw, "max_tokens_per_request", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            rate_limit_requests_per_window=_derived_section_value(
                adm_raw, "rate_limit_requests_per_window", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            rate_limit_window_seconds=_derived_section_value(
                adm_raw, "rate_limit_window_seconds", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            queue_enabled=_derived_section_value(adm_raw, "queue_enabled", _DERIVED_ADMISSION_DEFAULTS, "admission"),
            queue_timeout_seconds=_derived_section_value(
                adm_raw, "queue_timeout_seconds", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            reject_retry_after_seconds=_derived_section_value(
                adm_raw, "reject_retry_after_seconds", _DERIVED_ADMISSION_DEFAULTS, "admission"
            ),
            downgrade_model=_admission_downgrade_model(reg.data, adm_raw),
            downgrade_backend=_admission_downgrade_backend(reg.data, adm_raw),
            backend_concurrency=backend_concurrency,
        )

    # ---------------------------------------------------------------------------
    # Routing Policy (optional — maps task types to preferred backends)
    # ---------------------------------------------------------------------------
    rp_raw = raw.get("routing_policy", {})
    routing_policy_cfg = _routing_policy_from_registry_or_toml(reg.data, rp_raw)

    # Metrics
    met_raw = raw.get("metrics", {})
    if not met_raw:
        raise ValueError("[metrics] section missing from config/orc/server.toml")

    # Dashboard
    dash_raw = raw.get("dashboard", {})
    if not dash_raw:
        raise ValueError("[dashboard] section missing from config/orc/server.toml")

    # Hardware
    hw_raw = raw.get("hardware", {})
    adaptive_raw = hw_raw.get("adaptive", {})
    vram_thresholds_raw = adaptive_raw.get("vram_thresholds", {})
    vram_profiles_raw = adaptive_raw.get("vram_profiles", {})
    ram_thresholds_raw = adaptive_raw.get("ram_thresholds", {})
    ram_profiles_raw = adaptive_raw.get("ram_profiles", {})
    disk_profiles_raw = adaptive_raw.get("disk_profiles", {})
    adaptive_policy = AdaptivePolicyConfig(
        min_context_workers=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "min_context_workers"
        ),
        max_context_workers=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "max_context_workers"
        ),
        parallel_min_vram_mb=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "parallel_min_vram_mb"
        ),
        parallel_min_physical_cores=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "parallel_min_physical_cores"
        ),
        ollama_num_parallel_default=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "ollama_num_parallel_default"
        ),
        ollama_num_parallel_gpu=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "ollama_num_parallel_gpu"
        ),
        low_disk_free_gb=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "low_disk_free_gb"
        ),
        vram_pressure_threshold=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "vram_pressure_threshold"
        ),
        ram_pressure_threshold=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "ram_pressure_threshold"
        ),
        cpu_pressure_threshold=_derived_env_value(
            "HARDWARE_ADAPTIVE", adaptive_raw, _DERIVED_HARDWARE_ADAPTIVE_DEFAULTS, "cpu_pressure_threshold"
        ),
        vram_thresholds=AdaptiveVramThresholdsConfig(
            high_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_VRAM_THRESHOLDS",
                vram_thresholds_raw,
                _DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS,
                "high_mb",
            ),
            mid_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_VRAM_THRESHOLDS",
                vram_thresholds_raw,
                _DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS,
                "mid_mb",
            ),
            entry_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_VRAM_THRESHOLDS",
                vram_thresholds_raw,
                _DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS,
                "entry_mb",
            ),
            quantize_below_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_VRAM_THRESHOLDS",
                vram_thresholds_raw,
                _DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS,
                "quantize_below_mb",
            ),
            full_warning_free_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_VRAM_THRESHOLDS",
                vram_thresholds_raw,
                _DERIVED_HARDWARE_VRAM_THRESHOLDS_DEFAULTS,
                "full_warning_free_mb",
            ),
        ),
        vram_profiles=AdaptiveVramProfilesConfig(
            high=_parse_adaptive_vram_profile(
                vram_profiles_raw.get("high", {}),
                "high",
                "hardware.adaptive.vram_profiles.high",
            ),
            mid=_parse_adaptive_vram_profile(
                vram_profiles_raw.get("mid", {}),
                "mid",
                "hardware.adaptive.vram_profiles.mid",
            ),
            entry=_parse_adaptive_vram_profile(
                vram_profiles_raw.get("entry", {}),
                "entry",
                "hardware.adaptive.vram_profiles.entry",
            ),
            low=_parse_adaptive_vram_profile(
                vram_profiles_raw.get("low", {}),
                "low",
                "hardware.adaptive.vram_profiles.low",
            ),
            cpu_only=_parse_adaptive_vram_profile(
                vram_profiles_raw.get("cpu_only", {}),
                "cpu_only",
                "hardware.adaptive.vram_profiles.cpu_only",
            ),
        ),
        ram_thresholds=AdaptiveRamThresholdsConfig(
            high_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_RAM_THRESHOLDS",
                ram_thresholds_raw,
                _DERIVED_HARDWARE_RAM_THRESHOLDS_DEFAULTS,
                "high_mb",
            ),
            standard_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_RAM_THRESHOLDS",
                ram_thresholds_raw,
                _DERIVED_HARDWARE_RAM_THRESHOLDS_DEFAULTS,
                "standard_mb",
            ),
            low_mb=_derived_env_value(
                "HARDWARE_ADAPTIVE_RAM_THRESHOLDS",
                ram_thresholds_raw,
                _DERIVED_HARDWARE_RAM_THRESHOLDS_DEFAULTS,
                "low_mb",
            ),
            swap_warning_ratio=_derived_env_value(
                "HARDWARE_ADAPTIVE_RAM_THRESHOLDS",
                ram_thresholds_raw,
                _DERIVED_HARDWARE_RAM_THRESHOLDS_DEFAULTS,
                "swap_warning_ratio",
            ),
        ),
        ram_profiles=AdaptiveRamProfilesConfig(
            high=_parse_adaptive_ram_profile(
                ram_profiles_raw.get("high", {}),
                "high",
                "hardware.adaptive.ram_profiles.high",
            ),
            standard=_parse_adaptive_ram_profile(
                ram_profiles_raw.get("standard", {}),
                "standard",
                "hardware.adaptive.ram_profiles.standard",
            ),
            low=_parse_adaptive_ram_profile(
                ram_profiles_raw.get("low", {}),
                "low",
                "hardware.adaptive.ram_profiles.low",
            ),
            minimal=_parse_adaptive_ram_profile(
                ram_profiles_raw.get("minimal", {}),
                "minimal",
                "hardware.adaptive.ram_profiles.minimal",
            ),
        ),
        disk_profiles=AdaptiveDiskProfilesConfig(
            nvme=_parse_adaptive_disk_profile(
                disk_profiles_raw.get("nvme", {}),
                "nvme",
                "hardware.adaptive.disk_profiles.nvme",
            ),
            ssd=_parse_adaptive_disk_profile(
                disk_profiles_raw.get("ssd", {}),
                "ssd",
                "hardware.adaptive.disk_profiles.ssd",
            ),
            hdd=_parse_adaptive_disk_profile(
                disk_profiles_raw.get("hdd", {}),
                "hdd",
                "hardware.adaptive.disk_profiles.hdd",
            ),
        ),
    )

    # Dynamic Routing
    dr_raw = raw.get("dynamic_routing", {})
    if not dr_raw:
        raise ValueError("[dynamic_routing] section missing from config/orc/agents.toml")

    # OpenAI compat
    oc_raw = raw.get("openai_compat", {})

    return Settings(
        symbiont=symbiont,
        rag=rag,
        ollama=ollama,
        services=services,
        models=models,
        context=context,
        repos=repos,
        graph=graph,
        security=security,
        logging=logging_cfg,
        agentic=agentic,
        agentic_runtime=agentic_runtime,
        session=session,
        classify=classify,
        calendar=calendar,
        rss=rss,
        email=email_cfg,
        llm=llm_config,
        metrics=MetricsStoreConfig(
            enabled=_env("metrics", "enabled", _require(met_raw, "enabled", "metrics")),
            retention_days=_derived_section_value(met_raw, "retention_days", _DERIVED_METRICS_DEFAULTS, "metrics"),
            flush_interval_seconds=_derived_section_value(
                met_raw, "flush_interval_seconds", _DERIVED_METRICS_DEFAULTS, "metrics"
            ),
            db_path=_derived_section_value(met_raw, "db_path", _DERIVED_METRICS_DEFAULTS, "metrics"),
            resource_monitor_enabled=_env("metrics", "resource_monitor_enabled", met_raw.get("resource_monitor_enabled", False)),
            resource_interval_seconds=_derived_section_value(
                met_raw, "resource_interval_seconds", _DERIVED_METRICS_DEFAULTS, "metrics"
            ),
            vram_warning_mb=_env("metrics", "vram_warning_mb", met_raw.get("vram_warning_mb", 7200)),
            vram_critical_mb=_env("metrics", "vram_critical_mb", met_raw.get("vram_critical_mb", 7600)),
            swap_warning_mb=_env("metrics", "swap_warning_mb", met_raw.get("swap_warning_mb", 2048)),
            swap_critical_mb=_env("metrics", "swap_critical_mb", met_raw.get("swap_critical_mb", 4096)),
        ),
        dashboard=DashboardSettingsConfig(
            enabled=_env("dashboard", "enabled", _require(dash_raw, "enabled", "dashboard")),
        ),
        hardware=HardwareConfig(
            auto_detect=_derived_section_value(hw_raw, "auto_detect", _DERIVED_HARDWARE_DEFAULTS, "hardware"),
            refresh_interval=_derived_section_value(hw_raw, "refresh_interval", _DERIVED_HARDWARE_DEFAULTS, "hardware"),
            response_cache_enabled=_derived_section_value(
                hw_raw, "response_cache_enabled", _DERIVED_HARDWARE_DEFAULTS, "hardware"
            ),
            response_cache_ttl=_derived_section_value(
                hw_raw, "response_cache_ttl", _DERIVED_HARDWARE_DEFAULTS, "hardware"
            ),
            adaptive_degradation=_derived_section_value(
                hw_raw, "adaptive_degradation", _DERIVED_HARDWARE_DEFAULTS, "hardware"
            ),
            adaptive=adaptive_policy,
        ),
        performance=performance,
        latency_routing=latency_routing,
        dynamic_routing=DynamicRoutingConfig(
            mode=_env("dynamic_routing", "mode", _require(dr_raw, "mode", "dynamic_routing")),
            routing_model=_env("dynamic_routing", "routing_model", _require(dr_raw, "routing_model", "dynamic_routing")),
            synthesis_model=_env("dynamic_routing", "synthesis_model", _require(dr_raw, "synthesis_model", "dynamic_routing")),
            routing_timeout=_derived_value(
                dr_raw,
                "routing_timeout",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            max_agents_per_request=_derived_value(
                dr_raw,
                "max_agents_per_request",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            per_agent_timeout=_derived_value(
                dr_raw,
                "per_agent_timeout",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            total_budget_tokens=_derived_value(
                dr_raw,
                "total_budget_tokens",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            fallback_on_error=_env("dynamic_routing", "fallback_on_error", _require(dr_raw, "fallback_on_error", "dynamic_routing")),
            decomposition_enabled=_env("dynamic_routing", "decomposition_enabled", _require(dr_raw, "decomposition_enabled", "dynamic_routing")),
            negotiation_enabled=_env("dynamic_routing", "negotiation_enabled", _require(dr_raw, "negotiation_enabled", "dynamic_routing")),
            peer_review_enabled=_env("dynamic_routing", "peer_review_enabled", _require(dr_raw, "peer_review_enabled", "dynamic_routing")),
            decomposition_timeout=_derived_value(
                dr_raw,
                "decomposition_timeout",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            negotiation_timeout=_derived_value(
                dr_raw,
                "negotiation_timeout",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
            max_subtasks=_derived_value(
                dr_raw,
                "max_subtasks",
                _DERIVED_DYNAMIC_ROUTING_DEFAULTS,
                "dynamic_routing",
            ),
        ),
        collaboration=_parse_collaboration(raw),
        pipeline=pipeline,
        escalation=escalation,
        capability=capability,
        intelligent_pipeline=intelligent_pipeline,
        container_lifecycle=container_lifecycle,
        prewarming=prewarming,
        inference_profiles=inference_profiles,
        context_budgets=context_budgets,
        context_budget_scaling=context_budget_scaling,
        admission=admission_cfg,
        routing_policy=routing_policy_cfg,
        dispatch=dispatch,
        observability_raw=raw.get("observability", {}),
        i18n_raw={
            key: raw.get(key, {})
            for key in (
                "i18n",
                "language_detection",
                "protected_spans",
                "spellcheck",
                "glossary",
                "translation",
                "latency",
                "cache",
                "i18n_rag",
                "final_response",
                "i18n_observability",
            )
            if raw.get(key, {}) or key == "i18n"
        },
        execution=execution_cfg,
        openai_compat_profiles=tuple(
            _derived_section_value(oc_raw, "expose_profiles", _DERIVED_OPENAI_COMPAT_DEFAULTS, "openai_compat")
        ),
    )


# Module-level singleton (lazy)
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _reset_settings() -> None:
    """Reset singleton — for testing."""
    global _settings
    _settings = None


class _JSONFormatter:
    """Simple JSON log formatter for structured logging."""

    def format(self, record: "logging.LogRecord") -> str:
        import json
        import logging as _logging  # noqa: F811
        import time as _time

        ct = _time.localtime(record.created)
        ts = f"{_time.strftime('%Y-%m-%dT%H:%M:%S', ct)}.{int(record.msecs):03d}"

        entry = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = _logging.Formatter().formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(cfg: "Settings | None" = None) -> None:
    """Configure root logger based on settings.

    When ``logging.format = "json"`` in config, emits structured JSON lines.
    Otherwise uses default text format.
    """
    import logging

    if cfg is None:
        cfg = get_settings()

    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicates on reload
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level)

    if cfg.logging.format == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        ))

    root.addHandler(handler)
