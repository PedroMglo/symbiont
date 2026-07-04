"""ClickHouse exporter — async batch writer for structured events.

Sends events to ClickHouse via HTTP JSON interface.
Fail-silent by default; events are not lost (JSONL catches them).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

from orchestrator.observability.config import ClickHouseConfig

log = logging.getLogger(__name__)
_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE: dict[str, str] = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text

# Reconnection settings
_RECONNECT_INTERVAL_SECONDS = 30.0
_RECONNECT_MAX_INTERVAL_SECONDS = 300.0


class ClickHouseSink:
    """Async batch exporter to ClickHouse via HTTP INSERT."""

    def __init__(self, config: ClickHouseConfig):
        self._cfg = config
        self._buffer: deque[dict[str, Any]] = deque(maxlen=config.batch_size * 5)
        self._lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._available = False
        self._schema_applied = False
        self._last_error: str | None = None
        self._reconnect_interval = _RECONNECT_INTERVAL_SECONDS

        if config.enabled:
            self._check_availability()
            if self._available:
                self._ensure_schema()

    @property
    def name(self) -> str:
        return "clickhouse"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _get_password(self) -> str:
        """Get password from env var."""
        return os.environ.get(self._cfg.password_env, "")

    def _check_availability(self) -> bool:
        """Ping ClickHouse to verify connectivity. Returns True if available."""
        try:
            import httpx

            resp = httpx.get(
                f"{self._cfg.url}/ping",
                timeout=3.0,
            )
            if resp.status_code == 200 and resp.text.strip() == "Ok.":
                if not self._available:
                    log.info("ClickHouse sink: connected to %s", self._cfg.url)
                self._available = True
                self._reconnect_interval = _RECONNECT_INTERVAL_SECONDS
                return True
            else:
                self._available = False
                self._last_error = f"Ping returned {resp.status_code}: {resp.text[:50]}"
                log.warning("ClickHouse sink: ping failed — %s", self._last_error)
                return False
        except Exception as exc:
            self._available = False
            self._last_error = str(exc)
            if not self._cfg.fail_silent:
                raise
            log.warning("ClickHouse sink: unavailable — %s", exc)
            return False

    def _ensure_schema(self) -> None:
        """Apply schema.sql to ensure all tables exist (idempotent CREATE IF NOT EXISTS)."""
        if self._schema_applied:
            return
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            log.warning("ClickHouse sink: schema.sql not found at %s", schema_path)
            return

        try:
            import httpx

            sql = schema_path.read_text()
            statements = [
                s.strip() for s in sql.split(";")
                if s.strip() and not s.strip().startswith("--")
            ]

            params: dict[str, str] = {}
            if self._cfg.username:
                params["user"] = self._cfg.username
            password = self._get_password()
            if password:
                params["password"] = password

            errors = 0
            for stmt in statements:
                try:
                    resp = httpx.post(
                        self._cfg.url,
                        content=stmt.encode("utf-8"),
                        params=params,
                        timeout=10.0,
                    )
                    if resp.status_code != 200:
                        log.warning("ClickHouse schema statement failed: %s", resp.text[:100])
                        errors += 1
                except Exception as exc:
                    log.warning("ClickHouse schema error: %s", exc)
                    errors += 1

            if errors == 0:
                self._schema_applied = True
                log.info("ClickHouse sink: schema applied (%d statements)", len(statements))
            else:
                log.warning("ClickHouse sink: schema partially applied (%d errors)", errors)
        except Exception as exc:
            log.warning("ClickHouse sink: failed to apply schema — %s", exc)

    def _reconnect_loop(self) -> None:
        """Background loop: periodically retry connection if unavailable."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._reconnect_interval)
            if self._stop_event.is_set():
                break
            if self._available:
                continue
            log.debug("ClickHouse sink: attempting reconnection...")
            if self._check_availability():
                self._ensure_schema()
                log.info("ClickHouse sink: reconnected successfully")
            else:
                # Exponential backoff capped at max interval
                self._reconnect_interval = min(
                    self._reconnect_interval * 2,
                    _RECONNECT_MAX_INTERVAL_SECONDS,
                )

    def start(self) -> None:
        """Start the background flush thread and reconnection thread."""
        if not self._cfg.enabled:
            return
        self._stop_event.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="clickhouse-flush"
        )
        self._flush_thread.start()
        # Reconnect thread: retries connection if ClickHouse was unavailable
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="clickhouse-reconnect"
        )
        self._reconnect_thread.start()

    def stop(self) -> None:
        """Stop flush thread, reconnect thread, and flush remaining events."""
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=5.0)
        # Final flush
        self._flush_batch()

    def write(self, event_dict: dict[str, Any]) -> None:
        """Add event to buffer (non-blocking)."""
        with self._lock:
            self._buffer.append(event_dict)

    def write_batch(self, events: list[dict[str, Any]]) -> None:
        """Add multiple events to buffer."""
        with self._lock:
            self._buffer.extend(events)

    def write_to_table(self, table: str, row: dict[str, Any]) -> None:
        """Direct INSERT of a single row to a specific table (bypasses buffer)."""
        if not self._available:
            return
        try:
            import httpx

            body = json.dumps(row, default=str)
            params: dict[str, str] = {
                "database": self._cfg.database,
                "query": _sql("insert_json_each_row_table.sql").format(table),
                "input_format_skip_unknown_fields": "1",
                "date_time_input_format": "best_effort",
            }
            if self._cfg.username:
                params["user"] = self._cfg.username
            password = self._get_password()
            if password:
                params["password"] = password

            resp = httpx.post(
                self._cfg.url,
                params=params,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=3.0,
            )
            if resp.status_code != 200:
                self._last_error = f"write_to_table({table}) failed ({resp.status_code}): {resp.text[:100]}"
                log.warning("ClickHouse sink: %s", self._last_error)
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("ClickHouse write_to_table(%s) error: %s", table, exc)

    def _flush_loop(self) -> None:
        """Background loop: flush buffer every N seconds."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._cfg.flush_interval_seconds)
            self._flush_batch()

    def _flush_batch(self) -> None:
        """Send buffered events to ClickHouse."""
        if not self._available or not self._buffer:
            return

        # Drain buffer
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()

        if not batch:
            return

        # Split into chunks of batch_size
        for i in range(0, len(batch), self._cfg.batch_size):
            chunk = batch[i : i + self._cfg.batch_size]
            self._insert_chunk(chunk)

    def _insert_chunk(self, chunk: list[dict[str, Any]]) -> None:
        """Insert a chunk of events into ClickHouse."""
        try:
            import httpx

            # Convert to JSONEachRow format
            body = "\n".join(json.dumps(row, default=str) for row in chunk)

            params: dict[str, str] = {
                "database": self._cfg.database,
                "query": _sql("insert_llm_events.sql"),
                "input_format_skip_unknown_fields": "1",
                "date_time_input_format": "best_effort",
            }

            # Auth via URL params (ClickHouse HTTP interface)
            if self._cfg.username:
                params["user"] = self._cfg.username
            password = self._get_password()
            if password:
                params["password"] = password

            resp = httpx.post(
                self._cfg.url,
                params=params,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=5.0,
            )

            if resp.status_code != 200:
                self._last_error = f"INSERT failed ({resp.status_code}): {resp.text[:200]}"
                log.warning("ClickHouse sink: %s", self._last_error)
                # Put back in buffer for retry
                with self._lock:
                    for item in reversed(chunk):
                        self._buffer.appendleft(item)
            else:
                log.debug("ClickHouse sink: inserted %d events", len(chunk))

        except Exception as exc:
            self._last_error = str(exc)
            if not self._cfg.fail_silent:
                raise
            log.warning("ClickHouse sink: insert error (fail_silent): %s", exc)
            # Retry: put back in buffer (capped by maxlen)
            with self._lock:
                for item in reversed(chunk[:50]):  # Don't overwhelm
                    self._buffer.appendleft(item)

    def health(self) -> dict[str, Any]:
        """Health check info."""
        return {
            "enabled": self._cfg.enabled,
            "available": self._available,
            "schema_applied": self._schema_applied,
            "url": self._cfg.url,
            "database": self._cfg.database,
            "buffer_size": len(self._buffer),
            "last_error": self._last_error,
        }
