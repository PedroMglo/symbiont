"""Compatibility readers for generated env files consumed by Docker Compose."""

from __future__ import annotations

from pathlib import Path

SECRET_MARKERS = ("secret", "token", "password", "passwd", "api_key", "apikey", "salt", "credential", "auth")


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def sanitized_env(values: dict[str, str]) -> dict[str, str]:
    return {key: "<secret>" if is_secret_key(key) else value for key, value in values.items()}
