"""Configuration loader for code_analysis feature."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _load_toml() -> dict:
    candidates = [
        Path(os.environ.get("CODE_ANALYSIS_CONFIG", "")),
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config" / "code_analysis.toml",
    ]
    for p in candidates:
        if p.is_file():
            return tomllib.loads(p.read_text())
    return {}


_TOML = _load_toml()


class GraphSettings(BaseSettings):
    rag_url: str = "https://rag:8484"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_nodes: int = 20

    model_config = {"env_prefix": "CODE_ANALYSIS_GRAPH_"}

    @field_validator("rag_url")
    @classmethod
    def _validate_rag_url(cls, value: str) -> str:
        parts = urlsplit(value.rstrip("/"))
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("CODE_ANALYSIS_GRAPH_RAG_URL must be an absolute HTTPS URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("CODE_ANALYSIS_GRAPH_RAG_URL must not contain credentials, query, or fragment")
        return value.rstrip("/")


class RepoSettings(BaseSettings):
    scan_paths: list[str] = Field(default_factory=lambda: ["~/repos", "~/projects"])
    max_files_list: int = 50
    include_git_status: bool = True

    model_config = {"env_prefix": "CODE_ANALYSIS_REPO_"}


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    model_config = {"env_prefix": "CODE_ANALYSIS_SERVER_"}


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "CODE_ANALYSIS_SECURITY_"}


class Settings:
    def __init__(self) -> None:
        self.graph = GraphSettings(
            **{k: v for k, v in _TOML.get("graph", {}).items() if v is not None}
        )
        self.repo = RepoSettings(
            **{k: v for k, v in _TOML.get("repo", {}).items() if v is not None}
        )
        self.server = ServerSettings(
            **{k: v for k, v in _TOML.get("server", {}).items() if v is not None}
        )
        self.security = SecuritySettings(
            **{k: v for k, v in _TOML.get("security", {}).items() if v is not None}
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
