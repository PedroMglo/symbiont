"""Routing Decision Log — persists dynamic routing decisions for learning.

Stores every routing decision made by the MetaSymbiont, along with
execution outcomes and optional user feedback. This data feeds the
PatternStore for few-shot prompt injection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

_SCHEMA = _sql("schema.sql")


@dataclass
class RoutingRecord:
    """A single routing decision record for persistence."""

    request_id: str
    query: str
    action: str
    agents_planned: list[str] = field(default_factory=list)
    agents_executed: list[str] = field(default_factory=list)
    reasoning: str = ""
    intent: str = ""
    complexity: str = ""
    session_id: str = ""
    routing_model: str = ""
    routing_latency_ms: float = 0.0
    execution_latency_ms: float = 0.0
    synthesis_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    success: bool = True
    fallback_used: bool = False
    critic_score: float | None = None
    critic_acceptable: bool | None = None
    response_preview: str = ""


class RoutingDecisionLog:
    """SQLite-backed log of all dynamic routing decisions.

    Thread-safe for single-writer. Provides queries for pattern extraction,
    success rates, and feedback correlation.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path else symbiont_data_path("symbiont", "routing_log.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=3.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, rec: RoutingRecord) -> None:
        """Persist a routing decision."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                _sql("execute_116.sql"),
                (
                    rec.request_id,
                    rec.session_id,
                    time.time(),
                    rec.query[:500],
                    rec.intent,
                    rec.complexity,
                    rec.action,
                    rec.reasoning,
                    json.dumps(rec.agents_planned),
                    json.dumps(rec.agents_executed),
                    rec.routing_model,
                    rec.routing_latency_ms,
                    rec.execution_latency_ms,
                    rec.synthesis_latency_ms,
                    rec.total_latency_ms,
                    rec.total_tokens,
                    rec.success,
                    rec.fallback_used,
                    rec.critic_score,
                    rec.critic_acceptable,
                    rec.response_preview[:200] if rec.response_preview else "",
                    time.time(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("RoutingDecisionLog: write failed: %s", exc)

    def record_feedback(
        self, request_id: str, rating: int, feedback: str = ""
    ) -> bool:
        """Record user feedback for a routing decision.

        Args:
            request_id: The request_id of the original routing decision.
            rating: 1-5 star rating.
            feedback: Optional text feedback.

        Returns:
            True if the record was updated.
        """
        if self._conn is None:
            return False
        rating = max(1, min(5, rating))
        try:
            cursor = self._conn.execute(
                _sql("execute_171.sql"),
                (rating, feedback[:500] if feedback else "", request_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            log.warning("RoutingDecisionLog: feedback write failed: %s", exc)
            return False

    def successful_patterns(
        self,
        *,
        min_rating: int = 4,
        limit: int = 20,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Extract successful routing patterns for few-shot injection.

        Returns patterns where:
        - User rated >= min_rating, OR
        - success=True AND critic_acceptable=True AND no user_rating (implicit positive)
        """
        if self._conn is None:
            return []
        cutoff = time.time() - (days * 86400)
        try:
            rows = self._conn.execute(
                _sql("execute_198.sql"),
                (cutoff, min_rating, limit),
            ).fetchall()
            return [
                {
                    "query": row["query"],
                    "intent": row["intent"],
                    "complexity": row["complexity"],
                    "action": row["action"],
                    "agents": json.loads(row["agents_executed"]) if row["agents_executed"] else [],
                    "reasoning": row["reasoning"],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            log.warning("RoutingDecisionLog: pattern query failed: %s", exc)
            return []

    def stats(self, days: int = 7) -> dict[str, Any]:
        """Aggregate routing statistics for dashboard."""
        if self._conn is None:
            return {}
        cutoff = time.time() - (days * 86400)
        try:
            row = self._conn.execute(
                _sql("execute_234.sql"),
                (cutoff,),
            ).fetchone()
            return dict(row) if row else {}
        except sqlite3.Error as exc:
            log.warning("RoutingDecisionLog: stats query failed: %s", exc)
            return {}

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent routing decisions for live dashboard."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                _sql("execute_255.sql"),
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("RoutingDecisionLog: recent query failed: %s", exc)
            return []

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
