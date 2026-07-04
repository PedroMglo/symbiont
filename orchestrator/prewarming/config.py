"""Prewarming configuration helpers."""

from __future__ import annotations

from pathlib import Path

from orchestrator.config import PrewarmConfig, _find_project_root


def resolve_catalog_path(cfg: PrewarmConfig) -> Path:
    """Resolve the catalog TOML path from config or use the bundled default."""
    if cfg.catalog_path:
        p = Path(cfg.catalog_path).expanduser()
        if not p.is_absolute():
            p = _find_project_root() / p
        return p
    # Default: bundled catalog.toml next to this file
    return Path(__file__).parent / "catalog.toml"
