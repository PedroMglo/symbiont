"""Shared working memory — stateless helpers for LangGraph state accumulation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MemoryEntry:
    """A single entry in the shared working memory."""

    agent_name: str
    key: str
    value: str
    timestamp: float
    visibility: Literal["public", "private"]
    ttl_seconds: int = 300


@dataclass(frozen=True)
class HandoffRequest:
    """Request from one agent to hand off work to another."""

    source_agent: str
    target_agent: str
    reason: str
    context_summary: str
    trace_id: str


class SharedWorkingMemory:
    """Stateless utilities operating on the memory list in SymbiontState.

    All methods are static — actual state lives in the LangGraph TypedDict
    and accumulates via operator.add.
    """

    @staticmethod
    def publish(
        entries: list[MemoryEntry],
        entry: MemoryEntry,
        max_entries: int = 10,
    ) -> list[MemoryEntry]:
        """Add an entry and evict expired/overflow entries."""
        now = time.time()
        live = [e for e in entries if now - e.timestamp < e.ttl_seconds]
        live.append(entry)
        if len(live) > max_entries:
            live.sort(key=lambda e: e.timestamp)
            live = live[-max_entries:]
        return live

    @staticmethod
    def read_public(
        entries: list[MemoryEntry],
        reader_agent: str,
        now: float | None = None,
    ) -> list[MemoryEntry]:
        """Return public entries + reader's own private entries, excluding expired."""
        if now is None:
            now = time.time()
        return [
            e
            for e in entries
            if (now - e.timestamp < e.ttl_seconds)
            and (e.visibility == "public" or e.agent_name == reader_agent)
        ]

    @staticmethod
    def evict(
        entries: list[MemoryEntry],
        now: float | None = None,
        max_entries: int = 10,
    ) -> list[MemoryEntry]:
        """Remove expired entries and cap at max_entries (most recent kept)."""
        if now is None:
            now = time.time()
        live = [e for e in entries if now - e.timestamp < e.ttl_seconds]
        if len(live) > max_entries:
            live.sort(key=lambda e: e.timestamp)
            live = live[-max_entries:]
        return live
