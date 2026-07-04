"""Configuration loader for the security_analysis feature."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _load_toml() -> dict:
    candidates = [
        Path(os.environ.get("SECURITY_ANALYSIS_CONFIG", "")),
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config" / "security_analysis.toml",
    ]
    for path in candidates:
        if path.is_file():
            return tomllib.loads(path.read_text(encoding="utf-8"))
    return {}


_TOML = _load_toml()


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    model_config = {"env_prefix": "SECURITY_ANALYSIS_SERVER_"}


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "SECURITY_ANALYSIS_SECURITY_"}


class WorkspaceSettings(BaseSettings):
    scan_paths: list[str] = Field(default_factory=lambda: ["/projects", "/host_home"])
    allow_host_home_mapping: bool = True

    model_config = {"env_prefix": "SECURITY_ANALYSIS_WORKSPACE_"}


class Settings:
    def __init__(self) -> None:
        self.server = ServerSettings(
            **{key: value for key, value in _TOML.get("server", {}).items() if value is not None}
        )
        self.security = SecuritySettings(
            **{key: value for key, value in _TOML.get("security", {}).items() if value is not None}
        )
        self.workspace = WorkspaceSettings(
            **{key: value for key, value in _TOML.get("workspace", {}).items() if value is not None}
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
