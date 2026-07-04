"""Non-blocking ClickHouse event dispatcher.

Architecture:
    emit(event)  →  Queue(maxsize)  →  daemon thread  →  ClickHouse HTTP batch INSERT

The daemon drains the queue every flush_interval OR when buffer reaches batch_size.
Events are grouped by target table and inserted as JSONEachRow.
On ClickHouse failure: one warning logged, exponential backoff reconnection.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .events import EVENT_TABLE_MAP, RAGEvent

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)
_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE: dict[str, str] = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text

_BACKOFF_INITIAL = 30.0
_BACKOFF_MAX = 300.0
_BACKOFF_FACTOR = 2.0


class Dispatcher:
    """Queue-based non-blocking ClickHouse writer."""

    def __init__(
        self,
        *,
        clickhouse_url: str = "https://localhost:8123",
        database: str = "obsidian_rag",
        username: str = "default",
        password: str = "",
        batch_size: int = 500,
        flush_interval: float = 2.0,
        queue_max_size: int = 10_000,
    ) -> None:
        self._url = clickhouse_url
        self._database = database
        self._username = username
        self._password = password
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._queue: queue.Queue[RAGEvent] = queue.Queue(maxsize=queue_max_size)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._available = False
        self._schema_applied = False
        self._backoff = _BACKOFF_INITIAL
        self._last_attempt: float = 0.0
        self._warned = False

        self._dropped = 0
        self._flushed = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def flushed_count(self) -> int:
        return self._flushed

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="obs-dispatcher", daemon=True
        )
        self._thread.start()
        log.info("Observability dispatcher started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._flush_all()
        log.info(
            "Observability dispatcher stopped (flushed=%d, dropped=%d)",
            self._flushed,
            self._dropped,
        )

    def emit(self, event: RAGEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        buffer: list[RAGEvent] = []
        last_flush = time.monotonic()

        while not self._stop_event.is_set():
            try:
                evt = self._queue.get(timeout=0.1)
                buffer.append(evt)
            except queue.Empty:
                pass

            now = time.monotonic()
            should_flush = (
                len(buffer) >= self._batch_size
                or (buffer and now - last_flush >= self._flush_interval)
            )

            if should_flush:
                self._flush(buffer)
                buffer = []
                last_flush = now

        if buffer:
            self._flush(buffer)

    def _flush_all(self) -> None:
        buffer: list[RAGEvent] = []
        while True:
            try:
                buffer.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if buffer:
            self._flush(buffer)

    def _flush(self, events: list[RAGEvent]) -> None:
        if not events:
            return

        from .dashboard import push_event

        if not self._available:
            if not self._try_connect():
                return

        grouped: dict[str, list[dict]] = defaultdict(list)
        for evt in events:
            table = EVENT_TABLE_MAP.get(evt.event)
            if table:
                row = evt.to_row()
                grouped[table].append(row)
                push_event(row)

        for table, rows in grouped.items():
            if not self._insert(table, rows):
                return

        self._flushed += len(events)

    def _try_connect(self) -> bool:
        now = time.monotonic()
        if now - self._last_attempt < self._backoff:
            return False

        self._last_attempt = now
        try:
            resp = httpx.get(
                f"{self._url}/ping",
                timeout=5.0,
            )
            if resp.status_code == 200:
                self._available = True
                self._backoff = _BACKOFF_INITIAL
                self._warned = False
                log.info("ClickHouse connection established (%s)", self._url)
                if not self._schema_applied:
                    self._apply_schema()
                return True
        except (httpx.HTTPError, OSError):
            pass

        if not self._warned:
            log.warning(
                "ClickHouse unavailable at %s — events buffered, retrying with backoff",
                self._url,
            )
            self._warned = True

        self._backoff = min(self._backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
        return False

    def _apply_schema(self) -> None:
        import importlib.resources as pkg_resources

        try:
            schema_text = (
                pkg_resources.files("obsidian_rag.observability")
                .joinpath("schema.sql")
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError):
            from pathlib import Path

            schema_path = Path(__file__).parent / "schema.sql"
            if not schema_path.exists():
                log.warning("schema.sql not found — skipping DDL")
                return
            schema_text = schema_path.read_text(encoding="utf-8")

        statements = [
            s.strip() for s in schema_text.split(";") if s.strip()
        ]
        for stmt in statements:
            try:
                resp = httpx.post(
                    self._url,
                    content=stmt,
                    params={"database": self._database},
                    headers=self._auth_headers(),
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    log.warning("Schema statement failed: %s", resp.text[:200])
            except (httpx.HTTPError, OSError) as exc:
                log.warning("Schema apply error: %s", exc)
                return

        self._schema_applied = True
        log.info("ClickHouse schema applied to database '%s'", self._database)

    def _insert(self, table: str, rows: list[dict]) -> bool:
        import json

        body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        query = _sql("insert_json_each_row_table.sql").format(self._database, table)

        try:
            resp = httpx.post(
                self._url,
                content=body,
                params={"query": query},
                headers=self._auth_headers(),
                timeout=10.0,
            )
            if resp.status_code != 200:
                log.debug("INSERT failed (%s): %s", table, resp.text[:200])
                self._mark_unavailable()
                return False
            return True
        except (httpx.HTTPError, OSError) as exc:
            log.debug("INSERT error (%s): %s", table, exc)
            self._mark_unavailable()
            return False

    def _mark_unavailable(self) -> None:
        self._available = False
        self._last_attempt = time.monotonic()
        if not self._warned:
            log.warning("ClickHouse connection lost — entering backoff")
            self._warned = True

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._username:
            headers["X-ClickHouse-User"] = self._username
        if self._password:
            headers["X-ClickHouse-Key"] = self._password
        return headers
