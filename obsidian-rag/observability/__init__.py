"""Observability public API.

Usage:
    from observability import emit, start, stop, is_enabled

    emit(RAGEvent(event=EventName.REQUEST_COMPLETED, latency_ms=42.0))
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .events import EventName, RAGEvent, query_hash

if TYPE_CHECKING:
    pass

__all__ = [
    "EventName",
    "RAGEvent",
    "emit",
    "is_enabled",
    "query_hash",
    "start",
    "stop",
]

log = logging.getLogger(__name__)

_dispatcher = None
_enabled = False


def is_enabled() -> bool:
    return _enabled


def start(
    *,
    clickhouse_url: str = "https://localhost:8123",
    database: str = "obsidian_rag",
    username: str = "default",
    password: str = "",
    batch_size: int = 500,
    flush_interval: float = 2.0,
    queue_max_size: int = 10_000,
    resource_sampling: bool = True,
    resource_sample_interval: float = 5.0,
) -> None:
    global _dispatcher, _enabled

    from ._dispatcher import Dispatcher

    _dispatcher = Dispatcher(
        clickhouse_url=clickhouse_url,
        database=database,
        username=username,
        password=password,
        batch_size=batch_size,
        flush_interval=flush_interval,
        queue_max_size=queue_max_size,
    )
    _dispatcher.start()
    _enabled = True

    if resource_sampling:
        from ._resource_sampler import start_sampler

        start_sampler(interval=resource_sample_interval)

    log.info("Observability enabled → %s/%s", clickhouse_url, database)


def stop() -> None:
    global _dispatcher, _enabled

    from ._resource_sampler import stop_sampler

    stop_sampler()

    if _dispatcher:
        _dispatcher.stop()
        _dispatcher = None

    _enabled = False


def emit(event: RAGEvent) -> None:
    if _dispatcher:
        _dispatcher.emit(event)
