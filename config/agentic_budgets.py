"""Derived budgets for long-running local agentic work."""

from __future__ import annotations


def planning_timeout_seconds(llm_timeout: int) -> int:
    """Budget for non-trivial planning agents such as decomposition."""

    return max(20, min(90, round(llm_timeout / 4)))


def routing_timeout_seconds(llm_timeout: int) -> int:
    """Budget for small routing decisions that still call a local model."""

    return max(5, min(12, round(llm_timeout / 18)))


def context_timeout_seconds(llm_timeout: int) -> int:
    """Budget for each context/feature source probe."""

    return max(8, min(20, round(llm_timeout / 10)))


def synthesis_timeout_seconds(llm_timeout: int) -> int:
    """Budget for response synthesis and similar consolidation calls."""

    return max(30, min(120, round(llm_timeout / 4)))


def material_decision_timeout_seconds(llm_timeout: int) -> int:
    """Watchdog for agentic material output generation."""

    return max(600, min(1800, llm_timeout * 5))


def task_default_timeout_seconds(llm_timeout: int) -> int:
    """Outer watchdog for a supervised agentic graph run."""

    return max(1200, min(3600, llm_timeout * 8))


def material_generation_budget_seconds(llm_timeout: int) -> int:
    """Budget for material builder/kernel proposal and repair progress."""

    return max(540, min(1740, material_decision_timeout_seconds(llm_timeout) - 30))


def material_generation_max_files(quality_latency: str) -> int:
    """Maximum planned files to request before recovery/focused repair."""

    return 24 if quality_latency == "quality" else 18


def material_generation_file_target_seconds(quality_latency: str) -> int:
    """Expected per-file budget used to select an initial plan slice."""

    return 18 if quality_latency == "quality" else 14
