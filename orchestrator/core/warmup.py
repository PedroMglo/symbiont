"""Model warmup manager — preloads models into VRAM and tracks warm/cold status."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from orchestrator.config import get_settings

log = logging.getLogger(__name__)


def _log_value(value: object, limit: int = 160) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]


@dataclass
class ModelWarmStatus:
    """Status of a model in Ollama."""

    name: str
    warm: bool = False
    size_bytes: int = 0
    vram_bytes: int = 0
    expires_at: str | None = None
    last_checked: float = 0.0


class WarmupManager:
    """Manages model preloading and warm/cold status tracking.

    Uses Ollama native API:
    - POST /api/generate with empty prompt + keep_alive to warm
    - GET /api/ps to check loaded models
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._base_url = cfg.ollama.base_url  # https://localhost:11434
        self._keep_alive = cfg.performance.keep_alive
        self._primary_models = list(cfg.performance.primary_warm_models)
        self._fallback_models = list(cfg.performance.fallback_warm_models)
        self._max_loaded = cfg.performance.max_loaded_models
        self._warm_cache: dict[str, ModelWarmStatus] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0  # seconds
        # VRAM budget thresholds (MB)
        self._vram_warn_threshold_mb: int = getattr(
            cfg.performance, "vram_warn_threshold_mb", 6500
        )
        self._vram_hard_threshold_mb: int = getattr(
            cfg.performance, "vram_hard_threshold_mb", 7200
        )

    def _get_vram_used_mb(self) -> int:
        """Get current total VRAM usage from loaded models via /api/ps.

        Returns VRAM in MB, or 0 if unavailable.
        """
        status = self.get_warm_status(force_refresh=True)
        total_bytes = sum(s.vram_bytes for s in status.values())
        return int(total_bytes / (1024 * 1024))

    def _ensure_vram_budget(self, protect_model: str | None = None) -> bool:
        """Ensure VRAM usage is within safe limits before loading a new model.

        If VRAM exceeds hard threshold, evicts models until below threshold.
        If model is already loaded, always returns True (no eviction needed).

        Returns True if safe to proceed, False if cannot free enough VRAM.
        """
        # If model already loaded, no budget concern
        if protect_model and self.is_model_warm(protect_model):
            return True

        # Check current count vs max
        status = self.get_warm_status(force_refresh=True)
        if len(status) >= self._max_loaded:
            protect = [protect_model] if protect_model else []
            evicted = self.evict_least_used(protect_models=protect)
            if evicted:
                log.info(
                    "WarmupManager: evicted %s to make room (was at %d/%d models)",
                    evicted, len(status), self._max_loaded,
                )
            else:
                log.warning(
                    "WarmupManager: at capacity (%d/%d models) and cannot evict",
                    len(status), self._max_loaded,
                )
                return False

        # Check VRAM usage
        vram_used = self._get_vram_used_mb()
        if vram_used > self._vram_hard_threshold_mb:
            log.warning(
                "WarmupManager: VRAM usage %dMB exceeds hard threshold %dMB, evicting...",
                vram_used, self._vram_hard_threshold_mb,
            )
            protect = [protect_model] if protect_model else []
            evicted = self.evict_least_used(protect_models=protect)
            if evicted:
                log.info("WarmupManager: evicted %s (VRAM was %dMB)", evicted, vram_used)
            else:
                return False
        elif vram_used > self._vram_warn_threshold_mb:
            log.warning(
                "WarmupManager: VRAM usage %dMB above warning threshold %dMB",
                vram_used, self._vram_warn_threshold_mb,
            )

        return True

    def warm_model(self, model: str, *, keep_alive: str | None = None) -> bool:
        """Warm a model by sending a minimal generate request with keep_alive.

        Checks VRAM budget before loading. If VRAM usage exceeds threshold,
        evicts least-used model first to prevent OOM.

        Returns True if successful.
        """
        # VRAM budget check — evict if near capacity
        if not self._ensure_vram_budget(protect_model=model):
            log.warning(
                "WarmupManager: VRAM budget exceeded, cannot safely load %s", _log_value(model)
            )
            return False

        ka = keep_alive or self._keep_alive
        try:
            resp = httpx.post(
                f"{self._base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": ka,
                },
                timeout=60.0,
            )
            if resp.status_code == 200:
                log.info("WarmupManager: model %s warmed (keep_alive=%s)", _log_value(model), _log_value(ka))
                # Invalidate cache
                self._cache_ts = 0.0
                return True
            log.warning("WarmupManager: warm %s returned HTTP %d", _log_value(model), resp.status_code)
            return False
        except httpx.RequestError as exc:
            log.warning("WarmupManager: warm %s failed: %s", _log_value(model), _log_value(exc))
            return False

    def warm_all(self) -> dict[str, bool]:
        """Warm all configured primary and fallback models.

        Returns dict of model → success.
        """
        results: dict[str, bool] = {}
        all_models = self._primary_models + self._fallback_models
        # Limit to max_loaded_models
        to_warm = all_models[:self._max_loaded]
        for model in to_warm:
            results[model] = self.warm_model(model)
        return results

    def get_warm_status(self, *, force_refresh: bool = False) -> dict[str, ModelWarmStatus]:
        """Query Ollama /api/ps to get currently loaded models.

        Results are cached for _cache_ttl seconds.
        """
        now = time.monotonic()
        if not force_refresh and (now - self._cache_ts) < self._cache_ttl:
            return self._warm_cache

        try:
            resp = httpx.get(f"{self._base_url}/api/ps", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            log.debug("WarmupManager: /api/ps failed: %s", exc)
            return self._warm_cache

        models_data = data.get("models", [])
        new_cache: dict[str, ModelWarmStatus] = {}
        for m in models_data:
            name = m.get("name", "")
            if not name:
                continue
            new_cache[name] = ModelWarmStatus(
                name=name,
                warm=True,
                size_bytes=m.get("size", 0),
                vram_bytes=m.get("size_vram", 0),
                expires_at=m.get("expires_at"),
                last_checked=now,
            )

        self._warm_cache = new_cache
        self._cache_ts = now
        return self._warm_cache

    def is_model_warm(self, model: str) -> bool:
        """Check if a specific model is currently loaded in VRAM."""
        status = self.get_warm_status()
        # Check exact match and also partial (tag matching)
        if model in status:
            return True
        # Handle cases like "qwen3:8b" matching "qwen3:8b" in ps output
        for loaded_name in status:
            if model in loaded_name or loaded_name in model:
                return True
        return False

    def get_loaded_models(self) -> list[str]:
        """Return list of currently loaded model names."""
        status = self.get_warm_status()
        return list(status.keys())

    def evict_model(self, model: str) -> bool:
        """Force-unload a model from VRAM by setting keep_alive=0.

        Returns True if the unload request succeeded.
        """
        try:
            resp = httpx.post(
                f"{self._base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": 0,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                log.info("WarmupManager: evicted model %s from VRAM", model)
                self._cache_ts = 0.0  # Invalidate cache
                return True
            log.warning("WarmupManager: evict %s returned HTTP %d", model, resp.status_code)
            return False
        except httpx.RequestError as exc:
            log.warning("WarmupManager: evict %s failed: %s", model, exc)
            return False

    def evict_least_used(self, *, protect_models: list[str] | None = None) -> str | None:
        """Evict the model with lowest VRAM usage that is NOT in the protect list.

        Uses model size_vram as a proxy: evicts the largest model to free the most VRAM.
        If protect_models is given, those models will be kept loaded.

        Returns the name of the evicted model, or None if nothing could be evicted.
        """
        status = self.get_warm_status(force_refresh=True)
        if not status:
            return None

        protect = set(protect_models or [])
        # Build candidates sorted by VRAM usage (evict largest first to free most space)
        candidates = [
            (name, s) for name, s in status.items()
            if name not in protect and not any(p in name or name in p for p in protect)
        ]
        if not candidates:
            return None

        # Sort by vram_bytes descending — evict the one using most VRAM
        candidates.sort(key=lambda x: x[1].vram_bytes, reverse=True)
        target = candidates[0][0]

        if self.evict_model(target):
            return target
        return None

    def reduce_keep_alive(self, duration: str = "5m") -> int:
        """Reduce keep_alive for all loaded models to speed up natural eviction.

        Returns number of models updated.
        """
        status = self.get_warm_status(force_refresh=True)
        updated = 0
        for model_name in status:
            try:
                resp = httpx.post(
                    f"{self._base_url}/api/generate",
                    json={
                        "model": model_name,
                        "prompt": "",
                        "keep_alive": duration,
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    updated += 1
            except httpx.RequestError:
                pass
        if updated:
            log.info("WarmupManager: reduced keep_alive to %s for %d models", duration, updated)
            self._cache_ts = 0.0
        return updated


# Module-level singleton
_warmup_manager: WarmupManager | None = None


def get_warmup_manager() -> WarmupManager:
    """Get or create the singleton WarmupManager."""
    global _warmup_manager
    if _warmup_manager is None:
        _warmup_manager = WarmupManager()
    return _warmup_manager


def _reset_warmup_manager() -> None:
    """Reset singleton — for testing."""
    global _warmup_manager
    _warmup_manager = None
