"""Resolved runtime storage paths owned by the central config resolver."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from config.resolver import DEFAULT_CONFIG_PATH, ROOT, resolve_config


class StoragePathConfigError(RuntimeError):
    """Raised when resolved storage path config is missing required data."""


def ai_local_root() -> Path:
    """Return the ai-local project root."""
    return ROOT.resolve()


@lru_cache(maxsize=1)
def _resolved_storage_paths() -> dict[str, str]:
    resolved = resolve_config(DEFAULT_CONFIG_PATH)
    storage_paths = resolved.get("storage_paths")
    if not isinstance(storage_paths, dict):
        raise StoragePathConfigError("resolved config missing storage_paths")
    return {
        str(key): str(value)
        for key, value in storage_paths.items()
        if value not in (None, "")
    }


def clear_storage_path_cache() -> None:
    """Clear cached resolver output for tests and config reloads."""
    _resolved_storage_paths.cache_clear()


def _resolved_path(key: str) -> Path:
    raw = _resolved_storage_paths().get(key)
    if not raw:
        raise StoragePathConfigError(f"resolved storage path {key} is missing")
    return Path(raw).expanduser().resolve()


def local_storage_root() -> Path:
    """Return the resolved ai-local storage root."""
    return _resolved_path("AI_LOCAL_STORAGE_ROOT")


def symbiont_data_root() -> Path:
    """Return the resolved orchestrator runtime data root."""
    return _resolved_path("SYMBIONT_DATA_DIR")


def symbiont_data_path(*parts: str) -> Path:
    """Return a path under the orchestrator runtime data root."""
    return symbiont_data_root().joinpath(*parts)


def symbiont_logs_root() -> Path:
    """Return the resolved orchestrator runtime logs root."""
    return _resolved_path("SYMBIONT_LOGS_DIR")


def symbiont_logs_path(*parts: str) -> Path:
    """Return a path under the orchestrator runtime logs root."""
    return symbiont_logs_root().joinpath(*parts)
