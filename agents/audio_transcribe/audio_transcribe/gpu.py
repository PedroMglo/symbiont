"""GPU detection, device selection, and CUDA utilities."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from audio_transcribe.errors import DeviceUnavailableError, InvalidTranscriptionConfigError

logger = logging.getLogger(__name__)
VALID_COMPUTE_TYPES = ("float16", "int8_float16", "int8", "float32")


@dataclass
class GPUInfo:
    available: bool = False
    device_name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    vram_used_mb: int = 0
    cuda_version: str = ""
    driver_version: str = ""


def detect_gpu() -> GPUInfo:
    """Detect NVIDIA GPU via the installed CUDA provider probes."""
    info = GPUInfo()

    # Try ctranslate2 first (lighter dependency)
    try:
        import ctranslate2

        if ctranslate2.get_supported_compute_types("cuda"):
            info.available = True
    except Exception:
        pass

    # Try nvidia-ml-py for detailed info
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info.available = True
        device_name = pynvml.nvmlDeviceGetName(handle)
        info.device_name = device_name if isinstance(device_name, str) else device_name.decode()
        mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        info.vram_total_mb = int(mem_info.total // (1024 * 1024))
        info.vram_free_mb = int(mem_info.free // (1024 * 1024))
        info.vram_used_mb = int(mem_info.used // (1024 * 1024))
        driver = pynvml.nvmlSystemGetDriverVersion()
        info.driver_version = driver if isinstance(driver, str) else driver.decode()
        pynvml.nvmlShutdown()
    except Exception:
        pass

    # Use torch as the final CUDA availability probe.
    if not info.available:
        try:
            import torch

            if torch.cuda.is_available():
                info.available = True
                info.device_name = torch.cuda.get_device_name(0)
                info.vram_total_mb = torch.cuda.get_device_properties(0).total_mem // (1024 * 1024)
                reserved = torch.cuda.memory_reserved(0) // (1024 * 1024)
                allocated = torch.cuda.memory_allocated(0) // (1024 * 1024)
                info.vram_used_mb = int(max(reserved, allocated))
                if info.vram_total_mb:
                    info.vram_free_mb = int(max(info.vram_total_mb - info.vram_used_mb, 0))
        except Exception:
            pass

    if info.available:
        logger.info(
            "GPU detected: %s (%sMB total, %sMB free)",
            info.device_name or "NVIDIA",
            info.vram_total_mb,
            info.vram_free_mb,
        )
    else:
        logger.info("No GPU detected, will use CPU")

    return info


def select_device(config_device: str) -> str:
    """Select device based on configuration and availability.

    Returns 'cuda' or 'cpu'.
    """
    if config_device == "cpu":
        return "cpu"
    if config_device == "cuda":
        gpu = detect_gpu()
        if not gpu.available:
            raise DeviceUnavailableError("CUDA requested but not available; no CPU degradation for explicit CUDA")
        return "cuda"
    # auto
    gpu = detect_gpu()
    return "cuda" if gpu.available else "cpu"


def select_compute_type(requested: str, device: str) -> str:
    """Select safe compute type based on device.

    For CPU, float16 is not well-supported, so use an explicit CPU-safe type.
    """
    if requested not in VALID_COMPUTE_TYPES:
        raise InvalidTranscriptionConfigError(
            message=f"Unsupported compute_type '{requested}'",
            detail=f"Allowed values: {', '.join(VALID_COMPUTE_TYPES)}",
        )
    if device == "cpu":
        # CPU doesn't support float16 well with CTranslate2
        if requested in ("float16", "int8_float16"):
            logger.info(f"Compute type {requested} is not supported for CPU; using int8")
            return "int8"
        return requested

    return requested


# Cached GPU info singleton
_gpu_info: Optional[GPUInfo] = None


def get_gpu_info(*, refresh: bool = False) -> GPUInfo:
    """Get cached GPU info, optionally refreshing dynamic VRAM numbers."""
    global _gpu_info
    if _gpu_info is None or refresh:
        _gpu_info = detect_gpu()
    return _gpu_info


def reset_gpu_info() -> None:
    """Reset cached GPU info (for testing)."""
    global _gpu_info
    _gpu_info = None


def wait_for_gpu_memory(
    min_free_mb: int,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> GPUInfo:
    """Wait briefly for enough free VRAM and return the freshest GPU info."""
    deadline = time.time() + max(timeout_seconds, 0.0)
    info = get_gpu_info(refresh=True)
    while info.available and info.vram_free_mb and info.vram_free_mb < min_free_mb and time.time() < deadline:
        time.sleep(max(poll_seconds, 0.2))
        info = get_gpu_info(refresh=True)
    return info


def clear_gpu_cache() -> None:
    """Attempt to release cached CUDA memory after failed loads."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
