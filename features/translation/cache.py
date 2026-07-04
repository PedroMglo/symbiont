"""SQLite cache for translation results."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheKeyParts:
    normalized_text: str
    source_lang: str
    target_lang: str
    glossary_version: str
    model_version: str
    spans_hash: str = ""


def build_cache_key(parts: CacheKeyParts) -> str:
    payload = "\x1f".join(
        [
            parts.normalized_text.strip().lower(),
            parts.source_lang,
            parts.target_lang,
            parts.glossary_version,
            parts.model_version,
            parts.spans_hash,
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class SQLiteCache:
    def __init__(self, path: str | Path, *, ttl_seconds: int = 604800, enabled: bool = True):
        self.path = Path(path).expanduser()
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._init()
            except (OSError, sqlite3.Error) as exc:
                log.warning("SQLite translation cache disabled at %s: %s", self.path, exc)
                self.enabled = False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                _sql("execute_60.sql")
            )

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(_sql("execute_74.sql"), (key,)).fetchone()
        except sqlite3.Error as exc:
            log.warning("SQLite translation cache read failed: %s", exc)
            self.enabled = False
            return None
        if row is None:
            return None
        created_at, payload = row
        if self.ttl_seconds > 0 and time.time() - float(created_at) > self.ttl_seconds:
            self.delete(key)
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            self.delete(key)
            return None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    _sql("execute_97.sql"),
                    (key, time.time(), json.dumps(payload, ensure_ascii=False)),
                )
        except sqlite3.Error as exc:
            log.warning("SQLite translation cache write failed: %s", exc)
            self.enabled = False

    def delete(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            with self._connect() as conn:
                conn.execute(_sql("execute_109.sql"), (key,))
        except sqlite3.Error as exc:
            log.warning("SQLite translation cache delete failed: %s", exc)
            self.enabled = False
