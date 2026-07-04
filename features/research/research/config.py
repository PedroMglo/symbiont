"""Configuration loader for the research feature."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _load_toml() -> dict:
    candidates = [
        Path(os.environ.get("RESEARCH_CONFIG", "")),
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
    ]
    for p in candidates:
        if p.is_file():
            return tomllib.loads(p.read_text())
    return {}


_TOML = _load_toml()


class RAGSettings(BaseSettings):
    url: str = "https://rag:8484"
    timeout_seconds: float = 8.0
    api_key: str = ""
    circuit_breaker_threshold: int = 3
    circuit_breaker_reset_seconds: int = 60

    model_config = {"env_prefix": "RESEARCH_RAG_"}

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value.rstrip("/"))
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("RESEARCH_RAG_URL must be an absolute HTTPS URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("RESEARCH_RAG_URL must not contain credentials, query, or fragment")
        return value.rstrip("/")


class CAGSettings(BaseSettings):
    db_path: str = ""
    default_intent: str = "general"

    model_config = {"env_prefix": "RESEARCH_CAG_"}


class SearchSettings(BaseSettings):
    default_top_k: int = 5
    max_top_k: int = 15
    default_budget_tokens: int = 2000

    model_config = {"env_prefix": "RESEARCH_SEARCH_"}


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    model_config = {"env_prefix": "RESEARCH_SERVER_"}


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "RESEARCH_SECURITY_"}


class Settings:
    def __init__(self) -> None:
        # pydantic-settings: env vars override TOML values.
        # Pass TOML as init kwargs but let BaseSettings resolve env overrides.
        self.rag = RAGSettings(**_toml_kwargs("rag", RAGSettings))
        self.cag = CAGSettings(**_toml_kwargs("cag", CAGSettings))
        self.search = SearchSettings(**_toml_kwargs("search", SearchSettings))
        self.server = ServerSettings(**_toml_kwargs("server", ServerSettings))
        self.security = SecuritySettings(**_toml_kwargs("security", SecuritySettings))


def _toml_kwargs(section: str, model_cls: type) -> dict:
    """Extract TOML values, excluding fields that have env var overrides set."""
    toml_section = _TOML.get(section, {})
    if not toml_section:
        return {}
    prefix = model_cls.model_config.get("env_prefix", "")
    result = {}
    for k, v in toml_section.items():
        if v is None:
            continue
        # If env var is set for this field, skip TOML value (env takes precedence)
        env_name = f"{prefix}{k}".upper()
        if os.environ.get(env_name) is not None:
            continue
        result[k] = v
    return result


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
