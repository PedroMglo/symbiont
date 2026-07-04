"""JSONL local structured logger — file-based fallback sink.

Writes events as one-per-line JSON to a rotating log file.
Always available even when ClickHouse/OTel are offline.
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from orchestrator.observability.config import LocalLogsConfig

log = logging.getLogger(__name__)


class JSONLSink:
    """Writes structured events as JSONL to a rotating file."""

    def __init__(self, config: LocalLogsConfig):
        self._cfg = config
        self._handler: RotatingFileHandler | None = None
        self._logger: logging.Logger | None = None
        self._enabled = config.enabled

        if self._enabled:
            self._setup()

    @property
    def name(self) -> str:
        return "jsonl"

    @property
    def available(self) -> bool:
        return self._enabled and self._logger is not None

    def _setup(self) -> None:
        """Create the log directory and configure rotating handler."""
        log_dir = Path(os.path.expanduser(self._cfg.path))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("JSONL sink: cannot create directory %s: %s", log_dir, exc)
            self._enabled = False
            return

        log_file = log_dir / "events.jsonl"
        max_bytes = self._cfg.max_file_mb * 1024 * 1024

        self._handler = RotatingFileHandler(
            str(log_file),
            maxBytes=max_bytes,
            backupCount=self._cfg.backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

        self._logger = logging.getLogger("orc.observability.jsonl")
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        self._logger.propagate = False

        log.debug("JSONL sink: writing to %s (max %dMB, %d backups)",
                  log_file, self._cfg.max_file_mb, self._cfg.backup_count)

    def write(self, event_dict: dict[str, Any]) -> None:
        """Write a single event dict as a JSON line."""
        if not self._logger:
            return
        try:
            line = json.dumps(event_dict, default=str, ensure_ascii=False)
            self._logger.info(line)
        except (TypeError, ValueError) as exc:
            log.debug("JSONL sink: serialisation error: %s", exc)

    def write_batch(self, events: list[dict[str, Any]]) -> None:
        """Write multiple events."""
        for event_dict in events:
            self.write(event_dict)

    def flush(self) -> None:
        """Flush the underlying handler."""
        if self._handler:
            self._handler.flush()

    def close(self) -> None:
        """Close and cleanup."""
        if self._handler:
            self._handler.close()
            self._enabled = False
