"""Observability configuration — parsed from [metrics] and [dashboard] TOML sections."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.storage_paths import symbiont_data_path


@dataclass(frozen=True)
class SQLiteConfig:
    wal_mode: bool = True
    busy_timeout_ms: int = 3000


@dataclass(frozen=True)
class MetricsConfig:
    """Configuration for the observability metrics store."""

    enabled: bool = True
    db_path: str = field(default_factory=lambda: str(symbiont_data_path("symbiont", "metrics.db")))
    retention_days: int = 90
    flush_interval_seconds: float = 2.0
    max_queue_size: int = 1000
    resource_monitor_enabled: bool = False
    resource_interval_seconds: float = 30.0
    vram_warning_mb: int = 7200
    vram_critical_mb: int = 7600
    swap_warning_mb: int = 2048
    swap_critical_mb: int = 4096
    record_prompts: bool = False
    record_responses: bool = False
    prompt_preview_chars: int = 0
    response_preview_chars: int = 0
    estimate_tokens_when_missing: bool = True
    sqlite: SQLiteConfig = field(default_factory=SQLiteConfig)

    @property
    def resolved_db_path(self) -> Path:
        """Expand ~ and return absolute Path."""
        return Path(self.db_path).expanduser()


@dataclass(frozen=True)
class DashboardConfig:
    """Configuration for the dashboard UI and API."""

    enabled: bool = True
    auth_required: bool = False
    realtime: bool = True
    sse_enabled: bool = True
    default_days: int = 7
