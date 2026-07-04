"""Configuration loader for the extrator feature."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(RuntimeError):
    """Raised when extrator configuration is incomplete or invalid."""


_SCHEMA: dict[str, dict[str, type]] = {
    "server": {"host": str, "port": int, "workers": int},
    "paths": {
        "data_dir": str,
        "uploads_dir": str,
        "bronze_dir": str,
        "silver_dir": str,
        "gold_dir": str,
        "conversions_dir": str,
        "cache_dir": str,
        "logs_dir": str,
        "tmp_dir": str,
    },
    "manifest": {"db_path": str},
    "hashing": {"block_size_bytes": int},
    "jobs": {"max_concurrent_jobs": int, "max_queued_jobs": int, "job_ttl_hours": int},
    "security": {
        "api_key_env": str,
        "allowed_roots": list,
        "allowed_extensions": list,
        "denied_extensions": list,
        "skip_patterns": list,
        "max_upload_size_bytes": int,
        "max_file_size_bytes": int,
        "allow_remote_links": bool,
        "preserve_originals": bool,
    },
    "formats": {
        "extract_input_extensions": list,
        "conversion_pairs": list,
        "output_formats": list,
    },
    "parsers": {
        "parser_priorities": list,
        "ocr_enabled": bool,
        "min_ocr_confidence": float,
    },
    "chunking": {
        "target_tokens": int,
        "max_tokens": int,
        "overlap_tokens": int,
        "min_chars": int,
    },
    "policies": {
        "embedding_policy_by_source_type": list,
    },
    "conversion": {
        "timeout_seconds": int,
        "pandoc_binary": str,
        "libreoffice_binary": str,
        "tesseract_binary": str,
        "overwrite_allowed": bool,
    },
    "output": {
        "write_jsonl": bool,
        "write_parquet": bool,
        "graph_candidates_enabled": bool,
        "rag_bundle_enabled": bool,
    },
    "observability": {"log_level": str, "record_events": bool},
}

_DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {
        "data_dir": "/temp/extrator",
        "uploads_dir": "/temp/extrator/uploads",
        "bronze_dir": "/temp/extrator/bronze",
        "silver_dir": "/temp/extrator/silver",
        "gold_dir": "/temp/extrator/gold",
        "conversions_dir": "/temp/extrator/conversions",
        "cache_dir": "/temp/extrator/cache",
        "logs_dir": "/temp/extrator/logs",
        "tmp_dir": "/temp/extrator/tmp",
    },
    "manifest": {"db_path": "/temp/extrator/manifest/extrator.duckdb"},
    "jobs": {
        "max_concurrent_jobs": 1,
        "job_ttl_hours": 168,
    },
    "conversion": {
        "timeout_seconds": 120,
    },
}


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    workers: int


@dataclass(frozen=True)
class PathsConfig:
    data_dir: str
    uploads_dir: str
    bronze_dir: str
    silver_dir: str
    gold_dir: str
    conversions_dir: str
    cache_dir: str
    logs_dir: str
    tmp_dir: str


@dataclass(frozen=True)
class ManifestConfig:
    db_path: str


@dataclass(frozen=True)
class HashingConfig:
    block_size_bytes: int


@dataclass(frozen=True)
class JobsConfig:
    max_concurrent_jobs: int
    max_queued_jobs: int
    job_ttl_hours: int


@dataclass(frozen=True)
class SecurityConfig:
    api_key_env: str
    allowed_roots: list[str]
    allowed_extensions: list[str]
    denied_extensions: list[str]
    skip_patterns: list[str]
    max_upload_size_bytes: int
    max_file_size_bytes: int
    allow_remote_links: bool
    preserve_originals: bool


@dataclass(frozen=True)
class FormatsConfig:
    extract_input_extensions: list[str]
    conversion_pairs: list[str]
    output_formats: list[str]


@dataclass(frozen=True)
class ParsersConfig:
    parser_priorities: list[str]
    ocr_enabled: bool
    min_ocr_confidence: float


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int
    max_tokens: int
    overlap_tokens: int
    min_chars: int


@dataclass(frozen=True)
class PoliciesConfig:
    embedding_policy_by_source_type: list[str]


@dataclass(frozen=True)
class ConversionConfig:
    timeout_seconds: int
    pandoc_binary: str
    libreoffice_binary: str
    tesseract_binary: str
    overwrite_allowed: bool


@dataclass(frozen=True)
class OutputConfig:
    write_jsonl: bool
    write_parquet: bool
    graph_candidates_enabled: bool
    rag_bundle_enabled: bool


@dataclass(frozen=True)
class ObservabilityConfig:
    log_level: str
    record_events: bool


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    paths: PathsConfig
    manifest: ManifestConfig
    hashing: HashingConfig
    jobs: JobsConfig
    security: SecurityConfig
    formats: FormatsConfig
    parsers: ParsersConfig
    chunking: ChunkingConfig
    policies: PoliciesConfig
    conversion: ConversionConfig
    output: OutputConfig
    observability: ObservabilityConfig
    raw: dict[str, Any]
    config_hash: str


def _config_path() -> Path:
    explicit = os.environ.get("EXTRATOR_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(__file__).resolve().parent.parent / "config.toml"


def _parse_env_value(value: str, expected: type) -> Any:
    if expected is bool:
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ConfigError(f"Invalid boolean value: {value!r}")
    if expected is int:
        return int(value)
    if expected is float:
        return float(value)
    if expected is list:
        stripped = value.strip()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ConfigError(f"Expected list JSON value, got: {value!r}")
            return parsed
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Extrator config file not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)

    for section, keys in _SCHEMA.items():
        section_data = dict(_DEFAULTS.get(section, {}))
        section_data.update(raw.get(section, {}))
        for key, expected_type in keys.items():
            env_key = f"EXTRATOR_{section}_{key}".upper()
            if env_key in os.environ:
                section_data[key] = _parse_env_value(os.environ[env_key], expected_type)
        raw[section] = section_data
    return raw


def _require_section(raw: dict[str, Any], section: str) -> dict[str, Any]:
    value = raw.get(section)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing required [{section}] section")
    return value


def _require_value(raw: dict[str, Any], section: str, key: str, expected: type) -> Any:
    section_data = _require_section(raw, section)
    if key not in section_data or section_data[key] is None:
        raise ConfigError(f"Missing required config value: [{section}] {key}")
    value = section_data[key]
    if expected is list:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ConfigError(f"[{section}] {key} must be a list of strings")
        return [str(item) for item in value]
    if expected is int and not isinstance(value, int):
        raise ConfigError(f"[{section}] {key} must be an integer")
    if expected is float and not isinstance(value, (float, int)):
        raise ConfigError(f"[{section}] {key} must be a number")
    if expected is bool and not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} must be a boolean")
    if expected is str and not isinstance(value, str):
        raise ConfigError(f"[{section}] {key} must be a string")
    return value


def _section_values(raw: dict[str, Any], section: str) -> dict[str, Any]:
    return {
        key: _require_value(raw, section, key, expected)
        for key, expected in _SCHEMA[section].items()
    }


def _validate_cross_fields(cfg: AppConfig) -> None:
    if cfg.chunking.overlap_tokens >= cfg.chunking.max_tokens:
        raise ConfigError("[chunking] overlap_tokens must be lower than max_tokens")
    if cfg.chunking.target_tokens > cfg.chunking.max_tokens:
        raise ConfigError("[chunking] target_tokens must be lower than or equal to max_tokens")
    if cfg.jobs.max_concurrent_jobs > cfg.jobs.max_queued_jobs:
        raise ConfigError("[jobs] max_concurrent_jobs cannot exceed max_queued_jobs")
    if cfg.hashing.block_size_bytes <= 0:
        raise ConfigError("[hashing] block_size_bytes must be positive")
    if cfg.chunking.overlap_tokens >= cfg.chunking.target_tokens:
        raise ConfigError("[chunking] overlap_tokens must be lower than target_tokens")
    if not cfg.security.allowed_roots:
        raise ConfigError("[security] allowed_roots cannot be empty")
    if not cfg.security.allowed_extensions:
        raise ConfigError("[security] allowed_extensions cannot be empty")


def load_config(path: str | Path | None = None) -> AppConfig:
    config_file = Path(path).expanduser() if path else _config_path()
    raw = _load_raw(config_file)
    config_hash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()

    cfg = AppConfig(
        server=ServerConfig(**_section_values(raw, "server")),
        paths=PathsConfig(**_section_values(raw, "paths")),
        manifest=ManifestConfig(**_section_values(raw, "manifest")),
        hashing=HashingConfig(**_section_values(raw, "hashing")),
        jobs=JobsConfig(**_section_values(raw, "jobs")),
        security=SecurityConfig(**_section_values(raw, "security")),
        formats=FormatsConfig(**_section_values(raw, "formats")),
        parsers=ParsersConfig(**_section_values(raw, "parsers")),
        chunking=ChunkingConfig(**_section_values(raw, "chunking")),
        policies=PoliciesConfig(**_section_values(raw, "policies")),
        conversion=ConversionConfig(**_section_values(raw, "conversion")),
        output=OutputConfig(**_section_values(raw, "output")),
        observability=ObservabilityConfig(**_section_values(raw, "observability")),
        raw=raw,
        config_hash=config_hash,
    )
    _validate_cross_fields(cfg)
    return cfg


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    global _config
    _config = None
