"""Predictive prewarming module — predicts needed services before LLM planning."""

from __future__ import annotations

from orchestrator.prewarming.engine import PrewarmEngine

__all__ = ["PrewarmEngine", "get_prewarm_engine"]

_engine: PrewarmEngine | None = None


def get_prewarm_engine() -> PrewarmEngine | None:
    """Return the global PrewarmEngine singleton (None if not initialized)."""
    return _engine


def set_prewarm_engine(engine: PrewarmEngine) -> None:
    """Set the global PrewarmEngine singleton (called by factory at startup)."""
    global _engine
    _engine = engine
