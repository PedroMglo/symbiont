"""In-memory background queue metadata for scheduler diagnostics."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedDecision:
    owner: str
    lane: str
    reason: str
    queued_at: float
    retry_after_s: int | None


class SchedulerQueue:
    def __init__(self, *, maxlen: int = 200) -> None:
        self._items: deque[QueuedDecision] = deque(maxlen=maxlen)

    def add(self, *, owner: str, lane: str, reason: str, retry_after_s: int | None = None) -> None:
        self._items.append(
            QueuedDecision(
                owner=owner,
                lane=lane,
                reason=reason,
                queued_at=time.time(),
                retry_after_s=retry_after_s,
            )
        )

    def recent(self, *, limit: int = 50) -> list[dict[str, object]]:
        return [item.__dict__ for item in list(self._items)[-max(1, min(limit, 200)):]]
