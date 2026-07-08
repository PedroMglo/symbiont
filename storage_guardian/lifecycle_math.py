"""Derived lifecycle and compression math."""

from __future__ import annotations

import math


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def age_ratio(file_age_days: float, hot_until_days: int, cold_after_days: int) -> float:
    warm_span = max(1, cold_after_days - hot_until_days)
    return clamp((file_age_days - hot_until_days) / warm_span, 0.0, 1.0)


def compression_aggression(file_age_days: float, hot_until_days: int, cold_after_days: int) -> float:
    return age_ratio(file_age_days, hot_until_days, cold_after_days)


def zstd_level(aggression: float) -> int:
    return int(round(3 + clamp(aggression, 0.0, 1.0) * 9))


def sevenzip_level(aggression: float) -> int:
    return int(math.ceil(5 + clamp(aggression, 0.0, 1.0) * 4))


def max_parallel_jobs(cpu_cores: int, cpu_budget_fraction_of_idle: float) -> int:
    return max(1, math.floor(cpu_cores * cpu_budget_fraction_of_idle / 2))


def archive_chunk_target(available_memory_bytes: int) -> int:
    min_target = 512 * 1024 * 1024
    max_target = 8 * 1024 * 1024 * 1024
    return int(min(max(available_memory_bytes * 0.05, min_target), max_target))


def min_gain_required(file_size_mb: float) -> float:
    return max(0.03, min(0.12, 1 / math.sqrt(file_size_mb + 1)))


def restore_space_required(original_size_bytes: int) -> int:
    return int(original_size_bytes * 1.10)


def lifecycle_state_for_age(file_age_days: float, hot_until_days: int, cold_after_days: int) -> str:
    if file_age_days < hot_until_days:
        return "hot"
    if file_age_days < cold_after_days:
        return "warm_candidate"
    return "cold_candidate"
