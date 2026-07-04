"""Shared async HTTP client pool with connection reuse and HTTP/2 support.

Provides a singleton httpx.AsyncClient configured from PipelineConfig.
Used by async LLM methods and async context providers for efficient
connection pooling across the symbiont.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from orchestrator.config import PipelineConfig

log = logging.getLogger(__name__)

_pool: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


async def get_async_client(cfg: PipelineConfig | None = None) -> httpx.AsyncClient:
    """Return the singleton async client with connection pooling.

    Thread-safe lazy initialization. Uses PipelineConfig for pool sizing
    and HTTP/2 negotiation. Subsequent calls ignore cfg and return the
    existing client.
    """
    global _pool
    if _pool is not None:
        return _pool

    async with _lock:
        if _pool is not None:
            return _pool

        if cfg is None:
            from orchestrator.config import PipelineConfig
            cfg = PipelineConfig()

        limits = httpx.Limits(
            max_connections=cfg.connection_pool_size,
            max_keepalive_connections=cfg.connection_pool_size,
            keepalive_expiry=cfg.keepalive_expiry,
        )

        use_http2 = cfg.http2_enabled
        if use_http2:
            try:
                import h2  # noqa: F401
            except ImportError:
                use_http2 = False
                log.warning("h2 package not installed — falling back to HTTP/1.1")

        _pool = httpx.AsyncClient(
            http2=use_http2,
            limits=limits,
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
        )
        log.info(
            "HTTP pool initialized: pool_size=%d, keepalive=%ds, http2=%s",
            cfg.connection_pool_size,
            cfg.keepalive_expiry,
            cfg.http2_enabled,
        )
        return _pool


async def close_pool() -> None:
    """Shutdown the pool gracefully. Called from FastAPI lifespan shutdown."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
        log.info("HTTP pool closed")


def reset_pool() -> None:
    """Reset pool state (for testing). Does NOT close the client."""
    global _pool
    _pool = None
