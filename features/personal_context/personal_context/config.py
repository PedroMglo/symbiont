"""Configuration loader for personal_context feature."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - local minimal installs
    yaml = None  # type: ignore[assignment]


def _repo_root() -> Path:
    configured = os.environ.get("PERSONAL_CONTEXT_REPO_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "config").is_dir() or (parent / ".git").exists():
            return parent
    return Path.cwd().resolve()


def _load_toml() -> dict[str, Any]:
    candidates = [
        Path(os.environ.get("PERSONAL_CONTEXT_CONFIG", "")),
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
    ]
    for p in candidates:
        if p.is_file():
            return tomllib.loads(p.read_text())
    return {}


def _load_private_yaml() -> dict[str, Any]:
    if yaml is None:
        return {}
    candidates = [
        Path(os.environ.get("PERSONAL_CONTEXT_PRIVATE_CONFIG", "")),
        Path.cwd() / "config" / "private.local.yaml",
        _repo_root() / "config" / "private.local.yaml",
    ]
    for p in candidates:
        if p.is_file():
            with p.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                return {}
            private = loaded.get("personal_context", loaded)
            return private if isinstance(private, dict) else {}
    return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


_CONFIG = _deep_merge(_load_toml(), _load_private_yaml())


class CalendarSettings(BaseSettings):
    enabled: bool = True
    ics_paths: list[str] = Field(default_factory=lambda: ["~/.local/share/gnome-calendar/local.calendar"])
    window_days: int = 7

    model_config = {"env_prefix": "PERSONAL_CONTEXT_CALENDAR_"}


class EmailAccountSettings(BaseSettings):
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    password: str = ""
    imap_ssl: bool = True
    max_emails: int = 10
    folders: list[str] = Field(default_factory=lambda: ["INBOX"])
    label: str = ""  # display name

    model_config = {"env_prefix": "PERSONAL_CONTEXT_EMAIL_"}


class EmailSettings(BaseSettings):
    enabled: bool = False
    accounts: list[dict] = Field(default_factory=list)

    model_config = {"env_prefix": "PERSONAL_CONTEXT_EMAIL_"}


class RSSSettings(BaseSettings):
    enabled: bool = False
    feeds: list[str] = Field(default_factory=list)
    max_items_per_feed: int = 5
    timeout_seconds: int = 8

    model_config = {"env_prefix": "PERSONAL_CONTEXT_RSS_"}


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8093
    workers: int = 1

    model_config = {"env_prefix": "PERSONAL_CONTEXT_SERVER_"}


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "PERSONAL_CONTEXT_SECURITY_"}


class Settings:
    def __init__(self) -> None:
        self.calendar = CalendarSettings(**_config_kwargs("calendar", CalendarSettings))
        self.email = EmailSettings(**_config_kwargs("email", EmailSettings))
        self.rss = RSSSettings(**_config_kwargs("rss", RSSSettings))
        self.server = ServerSettings(**_config_kwargs("server", ServerSettings))
        self.security = SecuritySettings(**_config_kwargs("security", SecuritySettings))


def _config_kwargs(section: str, model_cls: type) -> dict[str, Any]:
    values = _CONFIG.get(section, {})
    if not isinstance(values, dict):
        return {}
    prefix = model_cls.model_config.get("env_prefix", "")
    allowed_fields = set(getattr(model_cls, "model_fields", {}))
    result: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if allowed_fields and key not in allowed_fields:
            continue
        env_name = f"{prefix}{key}".upper()
        if os.environ.get(env_name) is not None:
            continue
        result[key] = value
    return result


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
