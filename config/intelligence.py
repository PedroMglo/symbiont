"""Explainable inference rules for ai-local configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .runtime import RuntimeInfo
from .schema import AppConfig


@dataclass(frozen=True)
class Decision:
    field: str
    value: object
    origin: str
    reason: str
    formula: str = ""
    override: str = ""
    warning: str = ""


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _vram_pressure_warning(
    *,
    current_usable_gb: float,
    current_free_gb: float | None,
    estimated_task_gb: float,
) -> str:
    if current_usable_gb >= estimated_task_gb:
        return ""
    if current_free_gb is not None and current_free_gb >= estimated_task_gb:
        return (
            "GPU task fits in currently free VRAM, but reserved headroom is tight "
            "because VRAM is already in use; keep GPU loads serialized."
        )
    if current_free_gb is not None and current_free_gb > 0:
        return (
            "Free VRAM is below the generic new-task estimate, but a GPU path remains "
            "available; prefer reusing loaded GPU models and keep GPU work serialized."
        )
    return "No free VRAM remains for a new GPU task; queue GPU work or use a CPU-capable route."


def infer_vram(config: AppConfig, runtime: RuntimeInfo) -> list[Decision]:
    if not runtime.gpu_available or runtime.vram_total_gb is None:
        return [
            Decision(
                field="hardware.gpu.available",
                value=False,
                origin="runtime",
                reason="No NVIDIA GPU/VRAM was detected; CPU-capable backends remain valid.",
                override="AI_RUNTIME_FORCE_GPU",
                warning="GPU services should stay disabled unless explicitly forced.",
            )
        ]

    reserved = max(
        config.inference.reserved_vram_min_gb,
        runtime.vram_total_gb * config.inference.reserved_vram_fraction,
    )
    used = runtime.vram_used_gb or 0.0
    startup_usable = max(0.0, runtime.vram_total_gb - reserved)
    current_usable = max(0.0, runtime.vram_total_gb - reserved - used)
    util = min(
        config.inference.vllm_gpu_memory_utilization_cap,
        max(0.10, startup_usable / runtime.vram_total_gb),
    )
    warning = _vram_pressure_warning(
        current_usable_gb=current_usable,
        current_free_gb=runtime.vram_free_gb,
        estimated_task_gb=config.inference.estimated_vram_per_gpu_task_gb,
    )
    return [
        Decision(
            field="llm.vllm.reserved_vram_gb",
            value=round(reserved, 2),
            origin="inferred",
            reason="Reserve desktop/CUDA/KV-cache headroom before assigning VRAM to vLLM.",
            formula="max(reserved_vram_min_gb, total_vram_gb * reserved_vram_fraction)",
            override="AI_INFERENCE_RESERVED_VRAM_GB",
        ),
        Decision(
            field="llm.vllm.usable_vram_gb",
            value=round(current_usable, 2),
            origin="inferred",
            reason="Additional GPU-task headroom excludes reserved margin and currently used VRAM.",
            formula="max(0, total_vram_gb - reserved_vram_gb - currently_used_vram_gb)",
        ),
        Decision(
            field="llm.vllm.startup_usable_vram_gb",
            value=round(startup_usable, 2),
            origin="inferred",
            reason="Startup VRAM budget excludes only the reserved system margin, not VRAM currently used by running services.",
            formula="max(0, total_vram_gb - reserved_vram_gb)",
        ),
        Decision(
            field="llm.vllm.gpu_memory_utilization",
            value=round(util, 2),
            origin="inferred",
            reason="Cap vLLM startup allocation by total VRAM minus reserved system margin.",
            formula="min(vllm_gpu_memory_utilization_cap, startup_usable_vram_gb / total_vram_gb)",
            override="AI_LLM_VLLM_GPU_MEMORY_UTILIZATION",
            warning=warning,
        ),
    ]


def infer_workers(config: AppConfig, runtime: RuntimeInfo) -> Decision:
    cpu_workers = max(1, math.floor(runtime.cpu_threads * config.limits.cpu_budget_fraction))
    available_ram = runtime.ram_available_gb or 1.0
    ram_workers = max(1, math.floor(available_ram / config.inference.estimated_ram_per_worker_gb))
    if runtime.gpu_available and runtime.vram_free_gb is not None:
        reserved = max(
            config.inference.reserved_vram_min_gb,
            (runtime.vram_total_gb or runtime.vram_free_gb) * config.inference.reserved_vram_fraction,
        )
        gpu_workers = max(
            0,
            math.floor(
                max(0.0, runtime.vram_free_gb - reserved)
                / config.inference.estimated_vram_per_gpu_task_gb
            ),
        )
    else:
        gpu_workers = None
    user_max = config.limits.max_workers if isinstance(config.limits.max_workers, int) else 999
    candidates = [cpu_workers, ram_workers, user_max]
    if gpu_workers is not None:
        candidates.append(max(1, gpu_workers))
    final = max(1, min(candidates))
    return Decision(
        field="runtime.workers.final",
        value=final,
        origin="inferred",
        reason="Limit concurrency by CPU budget, available RAM, optional GPU capacity, and user max.",
        formula=(
            "min(cpu_workers=floor(cpu_threads*cpu_budget_fraction), "
            "ram_workers=floor(available_ram_gb/estimated_ram_per_worker_gb), "
            "gpu_workers_or_unlimited, user_max_workers)"
        ),
        override="AI_LIMITS_MAX_WORKERS",
    )


def infer_background_workers(config: AppConfig, runtime: RuntimeInfo) -> Decision:
    """Infer CPU/RAM-bound background concurrency separately from GPU capacity.

    `runtime.workers.final` is intentionally conservative for mixed interactive
    work because it accounts for available GPU capacity. Read-only extraction,
    local inspection, compression checks and similar background CPU/IO work
    should scale with CPU/RAM budgets without being serialized by low free VRAM.
    """

    cpu_workers = max(1, math.floor(runtime.cpu_threads * config.limits.cpu_budget_fraction))
    available_ram = runtime.ram_available_gb or 1.0
    ram_budget = max(0.1, available_ram * config.limits.memory_budget_fraction)
    ram_workers = max(1, math.floor(ram_budget / config.inference.estimated_ram_per_worker_gb))
    user_max = config.limits.max_workers if isinstance(config.limits.max_workers, int) else 999
    final = max(1, min(cpu_workers, ram_workers, user_max))
    return Decision(
        field="runtime.workers.background_cpu_io",
        value=final,
        origin="inferred",
        reason="Scale preemptible CPU/IO background work by CPU and RAM budgets without using GPU capacity as a limiter.",
        formula=(
            "min(cpu_workers=floor(cpu_threads*cpu_budget_fraction), "
            "ram_workers=floor(available_ram_gb*memory_budget_fraction/estimated_ram_per_worker_gb), "
            "user_max_workers)"
        ),
        override="AI_LIMITS_MAX_WORKERS",
    )


def infer_batch_size(config: AppConfig, runtime: RuntimeInfo) -> Decision:
    available = runtime.ram_available_gb or 1.0
    raw = math.floor(available / config.inference.estimated_memory_per_batch_item_gb)
    batch = clamp(raw, config.inference.min_batch_size, config.inference.max_batch_size)
    return Decision(
        field="runtime.batch_size",
        value=batch,
        origin="inferred",
        reason="Batch size is bounded by available RAM and profile limits.",
        formula="clamp(floor(available_memory_gb / estimated_memory_per_item_gb), min_batch_size, max_batch_size)",
        override="AI_RUNTIME_BATCH_SIZE",
    )


def infer_backend(config: AppConfig, runtime: RuntimeInfo) -> Decision:
    if config.llm.preferred_backend != "auto":
        value = config.llm.preferred_backend
        return Decision(
            field="llm.backend.effective",
            value=value,
            origin="manual",
            reason="User selected an explicit LLM backend preference.",
            override="AI_LLM_PREFERRED_BACKEND",
        )
    if runtime.gpu_available:
        value = "vllm"
        reason = "GPU was detected and backend preference is auto."
    else:
        value = "llama_cpp"
        reason = "No GPU was detected; choose CPU-capable llama.cpp before the Ollama route."
    return Decision(
        field="llm.backend.effective",
        value=value,
        origin="inferred",
        reason=reason,
        formula="vllm if gpu_available else llama_cpp",
        override="AI_LLM_PREFERRED_BACKEND",
    )


def infer_storage(config: AppConfig, runtime: RuntimeInfo) -> list[Decision]:
    root = config.storage.external_root
    if root is None:
        if config.storage.require_external and not config.storage.allow_local_heavy_fallback:
            mode = "external_missing"
            warning = "no external storage root configured or discovered"
        elif config.storage.require_external:
            mode = "local_fallback"
            warning = "no external storage root configured or discovered; using local fallback"
        else:
            mode = "local"
            warning = ""
        return [
            Decision(
                field="storage.mode",
                value=mode,
                origin="inferred",
                reason="No external storage root was configured or auto-discovered.",
                formula="local_fallback when external is required and fallback is allowed; local otherwise",
                override="storage.external_root, AI_STORAGE_EXTERNAL_ROOT, AI_STORAGE_AUTO_CANDIDATES",
                warning=warning,
            )
        ]
    warnings: list[str] = []
    if not runtime.storage_exists:
        warnings.append("external storage root does not exist")
    if runtime.storage_exists and not runtime.storage_mounted:
        warnings.append("external storage root is not a mount point")
    expected_filesystem = str(config.storage.expected_filesystem or "").lower()
    if (
        expected_filesystem
        and expected_filesystem not in {"auto", "any"}
        and runtime.storage_filesystem
        and runtime.storage_filesystem != config.storage.expected_filesystem
    ):
        warnings.append(
            f"filesystem is {runtime.storage_filesystem}, expected {config.storage.expected_filesystem}"
        )
    if runtime.storage_exists and not runtime.storage_writable:
        warnings.append("external storage root is not writable")
    docker_context = (runtime.docker_context or "").lower()
    if (
        runtime.docker_available
        and "desktop" in docker_context
        and root is not None
        and Path(root).as_posix().startswith("/mnt/")
    ):
        warnings.append("external storage root is under /mnt, which Docker Desktop does not share by default")
    valid = not warnings
    if valid:
        mode = "external"
    elif config.storage.allow_local_heavy_fallback:
        mode = "local_fallback"
    else:
        mode = "external_missing"
    return [
        Decision(
            field="storage.mode",
            value=mode,
            origin="inferred",
            reason="Storage mode is based on external root existence, mount, filesystem and writability.",
            formula=(
                "external if valid; local_fallback only when "
                "allow_local_heavy_fallback=true; otherwise external_missing"
            ),
            override="AI_STORAGE_EXTERNAL_ROOT, AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK",
            warning="; ".join(warnings),
        )
    ]


def infer_timeouts(config: AppConfig, runtime: RuntimeInfo) -> list[Decision]:
    backend = infer_backend(config, runtime).value
    cold_start = 120 if backend == "vllm" else 45
    if config.llm.quality_latency == "fast":
        normal = 90
    elif config.llm.quality_latency == "quality":
        normal = 300
    else:
        normal = 180
    return [
        Decision(
            field="timeouts.llm_request_seconds",
            value=normal,
            origin="inferred",
            reason="Request timeout follows quality/latency preference and local model-serving expectations.",
            formula="90 for fast, 180 for balanced, 300 for quality",
            override="AI_TIMEOUTS_LLM_REQUEST_SECONDS",
        ),
        Decision(
            field="timeouts.cold_start_seconds",
            value=cold_start,
            origin="inferred",
            reason="GPU model servers need longer cold-start budget than CPU backends.",
            formula="120 if effective backend is vllm else 45",
            override="AI_TIMEOUTS_COLD_START_SECONDS",
        ),
    ]


def infer_all(config: AppConfig, runtime: RuntimeInfo) -> list[Decision]:
    decisions: list[Decision] = []
    decisions.extend(infer_storage(config, runtime))
    decisions.append(infer_backend(config, runtime))
    decisions.extend(infer_vram(config, runtime))
    decisions.append(infer_workers(config, runtime))
    decisions.append(infer_background_workers(config, runtime))
    decisions.append(infer_batch_size(config, runtime))
    decisions.extend(infer_timeouts(config, runtime))
    return decisions
