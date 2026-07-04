"""Observability configuration — loaded from [observability] section of TOML."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.storage_paths import symbiont_logs_path


@dataclass
class ClickHouseConfig:
    enabled: bool = False
    url: str = "https://clickhouse:8123"
    database: str = "ai_symbiont"
    username: str = "default"
    password_env: str = "CLICKHOUSE_PASSWORD"
    batch_size: int = 1000
    flush_interval_seconds: float = 2.0
    retention_days: int = 180
    fail_silent: bool = True


@dataclass
class OTelConfig:
    enabled: bool = False
    endpoint: str = "https://otel-collector:4318"
    protocol: str = "http/protobuf"
    timeout_seconds: float = 2.0
    fail_silent: bool = True


@dataclass
class LocalLogsConfig:
    enabled: bool = True
    path: str = field(default_factory=lambda: str(symbiont_logs_path()))
    max_file_mb: int = 100
    backup_count: int = 10


@dataclass
class ResourceMonitorConfig:
    enabled: bool = True
    collect_cpu: bool = True
    collect_ram: bool = True
    collect_gpu: bool = True
    gpu_backend: str = "nvidia-smi"
    sample_interval_seconds: float = 10.0


@dataclass
class PrivacyConfig:
    record_prompts: bool = False
    record_responses: bool = False
    record_context: bool = False
    record_prompt_preview: bool = False
    record_response_preview: bool = False
    prompt_preview_chars: int = 0
    response_preview_chars: int = 0
    hash_queries: bool = True
    redact_secrets: bool = True
    redact_paths: bool = True


@dataclass
class GemilyniConfig:
    """Configuration for the Gemilyni observability layer (Gemini execution)."""

    enabled: bool = True
    dashboard_enabled: bool = True
    log_level: str = "INFO"
    capture_container_stats: bool = True
    capture_bundle_metadata: bool = True
    capture_context_metadata: bool = True
    capture_file_metadata: bool = True
    capture_policy_events: bool = True
    capture_worker_outputs_metadata: bool = True
    capture_token_estimates: bool = True
    capture_raw_context: bool = False
    capture_raw_files: bool = False
    capture_prompt_preview: bool = False
    redact_sensitive_values: bool = True
    max_preview_chars: int = 500
    container_stats_interval_seconds: int = 10


@dataclass
class ObservabilityConfig:
    enabled: bool = True
    service_name: str = "ai-symbiont"
    environment: str = "local"
    backend: str = "sqlite"  # "sqlite" | "clickhouse" | "both"
    instrumentation: str = "opentelemetry"

    clickhouse: ClickHouseConfig | None = None
    otel: OTelConfig | None = None
    local_logs: LocalLogsConfig | None = None
    resources: ResourceMonitorConfig | None = None
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    gemilyni: GemilyniConfig = field(default_factory=GemilyniConfig)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObservabilityConfig":
        """Build config from a TOML [observability] dict."""
        return cls(
            enabled=raw.get("enabled", True),
            service_name=raw.get("service_name", "ai-symbiont"),
            environment=raw.get("environment", "local"),
            backend=raw.get("backend", "sqlite"),
            instrumentation=raw.get("instrumentation", "opentelemetry"),
            clickhouse=_build(ClickHouseConfig, raw.get("clickhouse", {})),
            otel=_build(OTelConfig, raw.get("otel", {})),
            local_logs=_build(LocalLogsConfig, raw.get("local_logs", {})),
            resources=_build(ResourceMonitorConfig, raw.get("resources", {})),
            privacy=_build(PrivacyConfig, raw.get("privacy", {})),
            gemilyni=_build(GemilyniConfig, raw.get("gemilyni", {})),
        )

    @classmethod
    def disabled(cls) -> "ObservabilityConfig":
        """Return a config with everything disabled (for tests/fallback)."""
        return cls(enabled=False)


def _build(klass, raw: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    import dataclasses

    valid_fields = {f.name for f in dataclasses.fields(klass)}
    filtered = {k: v for k, v in raw.items() if k in valid_fields}
    return klass(**filtered)
