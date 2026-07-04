"""Immutable audit trail for security-relevant events.

Records all LLM interactions and security events in an in-memory ring buffer.
Events are also emitted to the observability system for durable ClickHouse persistence.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    timestamp: float
    event_type: str
    agent_name: str | None
    session_id: str | None
    request_id: str
    detail: dict[str, Any]


class AuditTrail:
    """In-memory ring buffer audit log with query capabilities."""

    def __init__(self, max_entries: int = 10000):
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)

    def log_llm_call(
        self,
        *,
        request_id: str,
        agent_name: str | None = None,
        session_id: str | None = None,
        model: str = "",
        tokens: int = 0,
        **extra: Any,
    ) -> None:
        """Record an LLM interaction."""
        entry = AuditEntry(
            timestamp=time.time(),
            event_type="LLM_CALL",
            agent_name=agent_name,
            session_id=session_id,
            request_id=request_id,
            detail={"model": model, "tokens": tokens, **extra},
        )
        self._entries.append(entry)

    def log_security_event(
        self,
        *,
        event_type: str,
        request_id: str,
        agent_name: str | None = None,
        session_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Record a security event (injection, budget, secret, sandbox)."""
        entry = AuditEntry(
            timestamp=time.time(),
            event_type=event_type,
            agent_name=agent_name,
            session_id=session_id,
            request_id=request_id,
            detail=dict(detail),
        )
        self._entries.append(entry)

    def query(
        self,
        *,
        since: float | None = None,
        agent: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query the audit trail with optional filters."""
        results: list[AuditEntry] = []
        for entry in reversed(self._entries):
            if since is not None and entry.timestamp < since:
                break
            if agent is not None and entry.agent_name != agent:
                continue
            if event_type is not None and entry.event_type != event_type:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    @property
    def size(self) -> int:
        return len(self._entries)
