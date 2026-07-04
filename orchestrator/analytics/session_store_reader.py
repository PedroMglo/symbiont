"""Read-only access to sessions.db for analytics.

Opens sessions.db in read-only mode (or normal mode with no writes).
Never modifies the database. Handles missing/empty DB gracefully.
"""

from __future__ import annotations

import logging
import sqlite3
import time
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


class SessionStoreReader:
    """Read-only reader for the symbiont sessions database.

    sessions.db schema:
        sessions(session_id TEXT, role TEXT, content TEXT, created_at REAL)
        PK: (session_id, created_at)
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()
        self._conn: sqlite3.Connection | None = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        if not self._db_path.exists():
            log.info("SessionStoreReader: %s not found", self._db_path)
            return
        try:
            # Try read-only URI mode first
            uri = f"file:{self._db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # Verify schema
            tables = [r[0] for r in self._conn.execute(
                _sql("execute_43.sql")
            ).fetchall()]
            if "sessions" not in tables:
                log.warning("SessionStoreReader: 'sessions' table not found")
                self._conn.close()
                self._conn = None
                return
            self._available = True
            log.debug("SessionStoreReader: opened %s (read-only)", self._db_path)
        except sqlite3.Error as exc:
            log.warning("SessionStoreReader: failed to open: %s", exc)
            # Fallback: regular connection (still won't write)
            try:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._available = True
            except sqlite3.Error:
                self._conn = None

    @property
    def available(self) -> bool:
        return self._available and self._conn is not None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _cutoff(self, days: int) -> float:
        return time.time() - (days * 86400)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def count_sessions(self, days: int | None = None) -> int:
        if not self.available:
            return 0
        if days:
            return self._conn.execute(
                _sql("execute_83.sql"),
                (self._cutoff(days),),
            ).fetchone()[0]
        return self._conn.execute(
            _sql("execute_87.sql")
        ).fetchone()[0]

    def count_messages(self, days: int | None = None) -> int:
        if not self.available:
            return 0
        if days:
            return self._conn.execute(
                _sql("execute_95.sql"),
                (self._cutoff(days),),
            ).fetchone()[0]
        return self._conn.execute(_sql("execute_98.sql")).fetchone()[0]

    def sessions_list(
        self, days: int = 7, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List sessions with aggregated metadata."""
        if not self.available:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_107.sql"), (cutoff, limit, offset)).fetchall()

        return [
            {
                "session_id": r["session_id"],
                "message_count": r["message_count"],
                "user_messages": r["user_messages"],
                "assistant_messages": r["assistant_messages"],
                "started_at": r["started_at"],
                "last_activity_at": r["last_activity_at"],
                "total_content_length": r["total_content_length"],
                "source": "sessions_db",
            }
            for r in rows
        ]

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        """Get detailed info for a single session (no content exposed)."""
        if not self.available:
            return None
        rows = self._conn.execute(_sql("execute_141.sql"), (session_id,)).fetchall()

        if not rows:
            return None

        messages = [
            {
                "role": r["role"],
                "content_length": r["content_length"],
                "timestamp": r["created_at"],
            }
            for r in rows
        ]

        return {
            "session_id": session_id,
            "message_count": len(messages),
            "started_at": rows[0]["created_at"],
            "last_activity_at": rows[-1]["created_at"],
            "messages": messages,
            "source": "sessions_db",
        }

    def sessions_per_day(self, days: int = 30) -> list[dict[str, Any]]:
        """Sessions and messages per day for timeline."""
        if not self.available:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_174.sql"), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def sessions_per_hour(self, days: int = 7) -> list[dict[str, Any]]:
        """Sessions per hour for fine-grained timeline."""
        if not self.available:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_192.sql"), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def active_sessions(self, since_seconds: int = 3600) -> int:
        """Count sessions active in the last N seconds."""
        if not self.available:
            return 0
        cutoff = time.time() - since_seconds
        return self._conn.execute(
            _sql("execute_211.sql"),
            (cutoff,),
        ).fetchone()[0]

    def detect_schema(self) -> dict[str, Any]:
        """Return schema info for diagnostics."""
        if not self.available:
            return {"available": False, "path": str(self._db_path)}
        cols = self._conn.execute(_sql("execute_219.sql")).fetchall()
        return {
            "available": True,
            "path": str(self._db_path),
            "columns": [{"name": c["name"], "type": c["type"]} for c in cols],
            "total_rows": self.count_messages(),
            "total_sessions": self.count_sessions(),
        }
