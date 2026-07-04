"""Webhook notification — fires after sync completes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


def notify_sync_complete(stats: dict[str, Any]) -> None:
    """Fire sync_complete webhook to all configured URLs (non-blocking).

    Runs in a background daemon thread so it never delays the main process.
    """
    from obsidian_rag.config import settings

    urls = settings.webhook.urls
    if not urls:
        return

    payload = {
        "event": "sync_complete",
        "timestamp": time.time(),
        "stats": stats,
    }

    thread = threading.Thread(
        target=_fire_webhooks,
        args=(urls, payload, settings.webhook.timeout),
        daemon=True,
        name="webhook-notify",
    )
    thread.start()


def _fire_webhooks(urls: tuple[str, ...], payload: dict, timeout: int) -> None:
    """Send POST to each webhook URL. Best-effort, logs failures."""
    import httpx

    data = payload

    for url in urls:
        try:
            httpx.post(url, json=data, timeout=timeout)
            log.info("Webhook OK: %s", url)
        except httpx.HTTPError as exc:
            log.debug("Webhook failed for %s: %s", url, exc)
        except Exception as exc:
            log.debug("Webhook unexpected error for %s: %s", url, exc)
