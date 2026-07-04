"""Session store — SQLite-backed conversation history with TTL."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from config.storage_paths import symbiont_data_path

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_DEFAULT_DB_DIR = symbiont_data_path("symbiont")
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "sessions.db"

_SCHEMA = _sql("schema.sql")


class SessionStore:
    """Lightweight SQLite session store for conversation history.

    Each session is a sequence of ``{role, content}`` messages keyed by
    ``session_id``.  Messages older than the configured TTL are pruned
    on explicit ``cleanup()`` calls.
    """

    def __init__(self, *, db_path: str | None = None, max_messages: int = 20) -> None:
        path = Path(os.path.expanduser(db_path)) if db_path else _DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(_sql("execute_40.sql"))
        self._db.executescript(_SCHEMA)
        self._max_messages = max_messages
        log.debug("SessionStore opened at %s", path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> list[dict]:
        """Return the last ``max_messages`` messages for a session."""
        rows = self._db.execute(
            _sql("execute_52.sql"),
            (session_id,),
        ).fetchall()
        messages = [{"role": r, "content": c} for r, c in rows]
        return messages[-self._max_messages:]

    def append(self, session_id: str, role: str, content: str) -> None:
        """Append a message to a session."""
        self._db.execute(
            _sql("execute_61.sql"),
            (session_id, role, content, time.time()),
        )
        self._db.commit()

    def cleanup(self, max_age_seconds: int) -> int:
        """Delete messages older than *max_age_seconds*. Returns rows deleted."""
        cutoff = time.time() - max_age_seconds
        cur = self._db.execute(_sql("execute_69.sql"), (cutoff,))
        self._db.commit()
        deleted = cur.rowcount
        if deleted:
            log.info("SessionStore: cleaned up %d expired messages", deleted)
        return deleted

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
