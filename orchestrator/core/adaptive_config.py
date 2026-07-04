"""Adaptive configuration — derives optimal runtime parameters from hardware.

Takes a HardwareProfile and the base Settings, producing an AdaptiveOverrides
dataclass that the engine, API, and subsystems use to auto-tune at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from orchestrator.core.hardware_profile import DiskType, HardwareProfile

if TYPE_CHECKING:
    from orchestrator.config import (
        AdaptiveDiskProfileConfig,
        AdaptivePolicyConfig,
        AdaptiveRamProfileConfig,
        AdaptiveVramProfileConfig,
    )

log = logging.getLogger(__name__)


class DegradationMode(str, Enum):
    """Runtime degradation level based on resource pressure."""

    NORMAL = "normal"
    CONSTRAINED = "constrained"  # reduce budgets, disable graph
    MINIMAL = "minimal"  # disable RAG, smallest model, cache-only


@dataclass
class AdaptiveOverrides:
    """Computed runtime parameters derived from hardware detection."""

    # GPU / Model loading
    max_loaded_models: int
    max_concurrent_llm: int
    keep_alive: str
    preferred_num_ctx: int
    gpu_offload: bool
    prefer_quantized: bool

    # CPU / Workers
    context_worker_threads: int
    suggested_ollama_num_parallel: int

    # RAM / Cache
    response_cache_max_size: int
    metrics_ring_buffer_size: int
    context_token_budget: int
    context_budget_multiplier: float

    # Disk / I/O
    metrics_flush_batch_size: int
    enable_model_preloading: bool
    log_buffer_size: int

    # Degradation
    degradation_mode: DegradationMode = DegradationMode.NORMAL
    vram_pressure_threshold: float = 0.0
    ram_pressure_threshold: float = 0.0
    cpu_pressure_threshold: float = 0.0

    # Derived recommendations
    recommendations: list[str] = field(default_factory=list)


def _resolve_policy(policy: "AdaptivePolicyConfig | None") -> "AdaptivePolicyConfig":
    if policy is not None:
        return policy
    from orchestrator.config import get_settings

    return get_settings().hardware.adaptive


def _apply_vram_profile(overrides: AdaptiveOverrides, profile: "AdaptiveVramProfileConfig") -> None:
    overrides.max_loaded_models = profile.max_loaded_models
    overrides.max_concurrent_llm = profile.max_concurrent_llm
    overrides.preferred_num_ctx = profile.preferred_num_ctx
    overrides.keep_alive = profile.keep_alive
    overrides.prefer_quantized = profile.prefer_quantized
    overrides.gpu_offload = profile.gpu_offload


def _apply_ram_profile(overrides: AdaptiveOverrides, profile: "AdaptiveRamProfileConfig") -> None:
    overrides.response_cache_max_size = profile.response_cache_max_size
    overrides.metrics_ring_buffer_size = profile.metrics_ring_buffer_size
    overrides.context_token_budget = profile.context_token_budget
    overrides.context_budget_multiplier = profile.context_budget_multiplier


def _apply_disk_profile(overrides: AdaptiveOverrides, profile: "AdaptiveDiskProfileConfig") -> None:
    overrides.metrics_flush_batch_size = profile.metrics_flush_batch_size
    overrides.enable_model_preloading = profile.enable_model_preloading
    overrides.log_buffer_size = profile.log_buffer_size


def _base_overrides(policy: "AdaptivePolicyConfig") -> AdaptiveOverrides:
    vram_profile = policy.vram_profiles.cpu_only
    ram_profile = policy.ram_profiles.standard
    disk_profile = policy.disk_profiles.ssd
    return AdaptiveOverrides(
        max_loaded_models=vram_profile.max_loaded_models,
        max_concurrent_llm=vram_profile.max_concurrent_llm,
        keep_alive=vram_profile.keep_alive,
        preferred_num_ctx=vram_profile.preferred_num_ctx,
        gpu_offload=vram_profile.gpu_offload,
        prefer_quantized=vram_profile.prefer_quantized,
        context_worker_threads=policy.min_context_workers,
        suggested_ollama_num_parallel=policy.ollama_num_parallel_default,
        response_cache_max_size=ram_profile.response_cache_max_size,
        metrics_ring_buffer_size=ram_profile.metrics_ring_buffer_size,
        context_token_budget=ram_profile.context_token_budget,
        context_budget_multiplier=ram_profile.context_budget_multiplier,
        metrics_flush_batch_size=disk_profile.metrics_flush_batch_size,
        enable_model_preloading=disk_profile.enable_model_preloading,
        log_buffer_size=disk_profile.log_buffer_size,
        vram_pressure_threshold=policy.vram_pressure_threshold,
        ram_pressure_threshold=policy.ram_pressure_threshold,
        cpu_pressure_threshold=policy.cpu_pressure_threshold,
    )


def _has_accelerated_backend(profile: HardwareProfile) -> bool:
    return profile.gpu.available or profile.ollama.accelerated


def _effective_vram_total_mb(
    profile: HardwareProfile,
    policy: "AdaptivePolicyConfig",
) -> int:
    if profile.has_gpu:
        return profile.gpu.vram_total_mb
    if profile.gpu.available:
        # Some containers can see the NVIDIA device through procfs but not
        # nvidia-smi. Treat this as a conservative entry-level GPU signal so
        # model policy does not incorrectly downgrade to CPU-only.
        return policy.vram_thresholds.entry_mb
    if profile.ollama.accelerated:
        # Remote Ollama only reports loaded/offloaded memory, not device total.
        # Use the entry threshold as the conservative floor for policy choice.
        return max(profile.ollama.loaded_vram_mb, policy.vram_thresholds.entry_mb)
    return 0


def compute_overrides(
    profile: HardwareProfile,
    policy: "AdaptivePolicyConfig | None" = None,
) -> AdaptiveOverrides:
    """Compute optimal runtime parameters from detected hardware.

    This is the core intelligence — translates raw hardware into tuned config.
    """
    policy = _resolve_policy(policy)
    overrides = _base_overrides(policy)
    recommendations: list[str] = []

    # -------------------------------------------------------------------------
    # GPU / VRAM optimization
    # -------------------------------------------------------------------------
    if _has_accelerated_backend(profile):
        vram = _effective_vram_total_mb(profile, policy)
        vram_free = profile.gpu.vram_free_mb if profile.has_gpu else 0
        thresholds = policy.vram_thresholds

        if vram >= thresholds.high_mb:
            # High-end GPU (24GB+): max parallelism
            _apply_vram_profile(overrides, policy.vram_profiles.high)
        elif vram >= thresholds.mid_mb:
            # Mid-range (12-24GB): 2 models, standard context
            _apply_vram_profile(overrides, policy.vram_profiles.mid)
        elif vram >= thresholds.entry_mb:
            # Entry-level (6-12GB): 1-2 small models
            _apply_vram_profile(overrides, policy.vram_profiles.entry)
            if vram < thresholds.quantize_below_mb:
                overrides.prefer_quantized = True
                recommendations.append(
                    f"VRAM={vram}MB: consider q4_0/q8_0 quantized models for better fit"
                )
        else:
            # Very low VRAM (<6GB): single model, minimal context
            _apply_vram_profile(overrides, policy.vram_profiles.low)
            recommendations.append(
                f"VRAM={vram}MB: limited — using single model with reduced context"
            )

        # Account for already-loaded models
        if profile.has_gpu and profile.ollama.loaded_vram_mb > 0:
            effective_free = vram_free
            if effective_free < thresholds.full_warning_free_mb:
                recommendations.append(
                    f"VRAM nearly full ({vram_free}MB free): consider unloading unused models"
                )

        if profile.gpu.available and not profile.has_gpu:
            recommendations.append(
                "GPU device is visible but VRAM metrics are unavailable; "
                "using conservative entry-level GPU policy"
            )

        if profile.ollama.accelerated and not profile.has_gpu:
            recommendations.append(
                "Ollama backend reports accelerated model execution; "
                "using remote backend GPU signal for adaptive policy"
            )
    else:
        # No GPU: CPU-only mode
        _apply_vram_profile(overrides, policy.vram_profiles.cpu_only)
        recommendations.append(
            "No GPU detected: running in CPU-only mode with smaller models"
        )

    # -------------------------------------------------------------------------
    # CPU optimization
    # -------------------------------------------------------------------------
    physical = profile.cpu.physical_cores

    # Context provider workers: bounded by cores and typical provider count
    overrides.context_worker_threads = min(
        max(physical, policy.min_context_workers),
        policy.max_context_workers,
    )

    # Ollama parallel: only suggest >1 if we have spare cores AND VRAM
    if (
        _has_accelerated_backend(profile)
        and _effective_vram_total_mb(profile, policy) >= policy.parallel_min_vram_mb
        and physical >= policy.parallel_min_physical_cores
    ):
        overrides.suggested_ollama_num_parallel = policy.ollama_num_parallel_gpu
    else:
        overrides.suggested_ollama_num_parallel = policy.ollama_num_parallel_default

    if physical <= policy.min_context_workers:
        recommendations.append(
            f"CPU has only {physical} physical cores: reduced worker pool"
        )

    # -------------------------------------------------------------------------
    # RAM optimization
    # -------------------------------------------------------------------------
    ram_total = profile.ram.total_mb
    ram_thresholds = policy.ram_thresholds

    if ram_total >= ram_thresholds.high_mb:
        # High RAM: large caches
        _apply_ram_profile(overrides, policy.ram_profiles.high)
    elif ram_total >= ram_thresholds.standard_mb:
        # Standard: moderate caches
        _apply_ram_profile(overrides, policy.ram_profiles.standard)
    elif ram_total >= ram_thresholds.low_mb:
        # Low-ish: smaller caches
        _apply_ram_profile(overrides, policy.ram_profiles.low)
        recommendations.append(
            f"RAM={ram_total}MB: reduced cache sizes and context budgets"
        )
    else:
        # Very low RAM (<8GB): minimal
        _apply_ram_profile(overrides, policy.ram_profiles.minimal)
        recommendations.append(
            f"RAM={ram_total}MB: aggressive resource reduction — ensure swap is available"
        )

    # Swap pressure warning
    if profile.ram.swap_used_mb > 0 and profile.ram.swap_total_mb > 0:
        swap_pct = profile.ram.swap_used_mb / profile.ram.swap_total_mb
        if swap_pct > ram_thresholds.swap_warning_ratio:
            recommendations.append(
                f"High swap usage ({profile.ram.swap_used_mb}MB/{profile.ram.swap_total_mb}MB): "
                "performance will be degraded"
            )

    # -------------------------------------------------------------------------
    # Disk optimization
    # -------------------------------------------------------------------------
    if profile.disk.disk_type == DiskType.NVME:
        _apply_disk_profile(overrides, policy.disk_profiles.nvme)
    elif profile.disk.disk_type == DiskType.SSD:
        _apply_disk_profile(overrides, policy.disk_profiles.ssd)
    elif profile.disk.disk_type == DiskType.HDD:
        _apply_disk_profile(overrides, policy.disk_profiles.hdd)
        recommendations.append(
            "HDD detected: model loading will be slower, disabled preloading"
        )

    if profile.disk.free_gb < policy.low_disk_free_gb:
        recommendations.append(
            f"Low disk space ({profile.disk.free_gb:.1f}GB free): "
            "monitor for model/log storage"
        )

    overrides.recommendations = recommendations
    return overrides


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_overrides: AdaptiveOverrides | None = None


def get_adaptive_overrides(
    profile: HardwareProfile | None = None,
    policy: "AdaptivePolicyConfig | None" = None,
) -> AdaptiveOverrides:
    """Get or compute the singleton AdaptiveOverrides."""
    global _overrides
    if _overrides is None:
        if profile is None:
            from orchestrator.core.hardware_profile import get_hardware_profile
            profile = get_hardware_profile()
        _overrides = compute_overrides(profile, policy)
        log.info(
            "Adaptive config: max_models=%d, max_concurrent=%d, num_ctx=%d, "
            "cache_size=%d, workers=%d, budget=%d, mode=%s",
            _overrides.max_loaded_models,
            _overrides.max_concurrent_llm,
            _overrides.preferred_num_ctx,
            _overrides.response_cache_max_size,
            _overrides.context_worker_threads,
            _overrides.context_token_budget,
            _overrides.degradation_mode.value,
        )
        if _overrides.recommendations:
            for rec in _overrides.recommendations:
                log.info("  → %s", rec)
    return _overrides


def update_degradation_mode(mode: DegradationMode) -> None:
    """Update the current degradation mode (called by resource monitor)."""
    global _overrides
    if _overrides is None:
        return
    if _overrides.degradation_mode != mode:
        log.warning("Degradation mode changed: %s → %s", _overrides.degradation_mode.value, mode.value)
        # AdaptiveOverrides is a regular dataclass (mutable)
        _overrides.degradation_mode = mode


def _reset_overrides() -> None:
    """Reset singleton — for testing."""
    global _overrides
    _overrides = None
