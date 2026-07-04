"""Latency budget helpers for progressive responses."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SLOBudgetManager:
    budget_ms: int
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000

    @property
    def remaining_ms(self) -> float:
        return max(0.0, self.budget_ms - self.elapsed_ms)

    def can_spend(self, estimated_ms: float) -> bool:
        return self.remaining_ms >= estimated_ms
