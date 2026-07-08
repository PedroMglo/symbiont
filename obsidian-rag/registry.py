"""Thin registry client for the RAG project.

Reads models.json from the symbiont project directory.
Falls back gracefully if the file is not available.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REGISTRY_DEFAULT = Path(__file__).resolve().parents[1] / "config" / "models" / "rag.config.json"

_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0


def _registry_path() -> Path:
    env = os.environ.get("AI_MODELS_REGISTRY")
    if env:
        return Path(env).expanduser()
    return _REGISTRY_DEFAULT


def _load() -> dict[str, Any]:
    global _cache, _cache_mtime
    path = _registry_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Model registry not found at {path}. "
            f"All model configuration is centralized in models.json. "
            "Set AI_MODELS_REGISTRY or place the registry under config/models/rag.config.json."
        )
    mtime = path.stat().st_mtime
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    with open(path) as f:
        _cache = json.load(f)
    _cache_mtime = mtime
    log.info("RAG: loaded model registry from %s", path)
    return _cache


def get_rag_model(role: str) -> str | None:
    """Get a RAG role model name (e.g., 'embedding', 'router', 'reranker')."""
    data = _load()
    return data.get("rag", {}).get("roles", {}).get(role, {}).get("model")


def get_rag_prompt(key: str) -> str:
    """Get a RAG prompt template by key."""
    data = _load()
    return data.get("rag", {}).get("prompts", {}).get(key, "")


def get_rag_system_prompt(role: str) -> str:
    """Get the system prompt for a RAG role."""
    data = _load()
    return data.get("rag", {}).get("roles", {}).get(role, {}).get("system_prompt", "")


def is_rag_capable(model: str) -> bool:
    """Check if a model has RAG capability enabled in the registry.

    Looks up the model name (or alias) across orchestration roles and checks
    the ``rag_capable`` field. Unknown models default to True.
    """
    data = _load()
    orch = data.get("orchestration", {}).get("roles", {})
    for _role_name, role_cfg in orch.items():
        if role_cfg.get("model") == model:
            return role_cfg.get("rag_capable", True)
        if model in role_cfg.get("aliases", []):
            return role_cfg.get("rag_capable", True)
    return True


def get_default_chat_model() -> str:
    """Get the default chat model name from orchestration.default_chat_role."""
    data = _load()
    orch = data.get("orchestration", {})
    role_name = orch.get("default_chat_role", "default-conversation")
    role = orch.get("roles", {}).get(role_name)
    if role:
        return role["model"]
    return ""
