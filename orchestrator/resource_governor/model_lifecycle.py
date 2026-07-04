"""Model lifecycle hooks controlled by Resource Governor policy."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ModelLifecycleManager:
    """Best-effort bridge to existing warmup/Ollama lifecycle code."""

    def cleanup_idle_models(self) -> None:
        try:
            from orchestrator.core.warmup import get_warmup_manager

            manager = get_warmup_manager()
            cleanup = getattr(manager, "cleanup_idle", None)
            if callable(cleanup):
                cleanup()
        except Exception as exc:
            log.debug("Model idle cleanup skipped: %s", exc)
