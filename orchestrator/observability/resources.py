"""System resource monitoring — GPU, RAM, CPU, Ollama model status.

Collects hardware metrics via psutil + nvidia-smi subprocess + Ollama /api/ps.
Snapshots are stored in metrics.db for dashboard visualization and history.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)


def _run_cmd(cmd: list[str], timeout: float = 3.0) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


class ResourceCollector:
    """Collects system resource snapshots (GPU, RAM, CPU, Ollama models)."""

    def __init__(self, ollama_base_url: str = "https://localhost:11434") -> None:
        self._ollama_url = ollama_base_url

    def snapshot(self) -> dict:
        """Collect a full system resource snapshot."""
        data: dict = {}
        data.update(self._collect_ram())
        data.update(self._collect_cpu())
        data.update(self._collect_gpu())
        data.update(self._collect_ollama())
        return data

    def _collect_ram(self) -> dict:
        """Collect RAM metrics via psutil."""
        try:
            import psutil
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "ram_total_mb": round(vm.total / (1024 * 1024)),
                "ram_used_mb": round(vm.used / (1024 * 1024)),
                "ram_available_mb": round(vm.available / (1024 * 1024)),
                "ram_percent": round(vm.percent, 1),
                "swap_total_mb": round(swap.total / (1024 * 1024)),
                "swap_used_mb": round(swap.used / (1024 * 1024)),
            }
        except ImportError:
            log.debug("psutil not available for RAM metrics")
            return {}

    def _collect_cpu(self) -> dict:
        """Collect CPU metrics via psutil."""
        try:
            import psutil
            return {
                "cpu_count": psutil.cpu_count(logical=True),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
            }
        except ImportError:
            return {}

    def _collect_gpu(self) -> dict:
        """Collect GPU metrics via nvidia-smi, then lightweight procfs fallbacks."""
        query = [
            "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ]
        for command in (
            "nvidia-smi",
            "/usr/lib/wsl/lib/nvidia-smi",
            "nvidia-smi.exe",
            "/mnt/c/Windows/System32/nvidia-smi.exe",
        ):
            out = _run_cmd([command, *query])
            if out:
                parsed = self._parse_nvidia_smi_gpu(out, source=os.path.basename(command))
                if parsed:
                    return parsed
        return self._collect_gpu_from_procfs()

    def _parse_nvidia_smi_gpu(self, out: str, *, source: str) -> dict:
        try:
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 6:
                return {
                    "gpu_name": parts[0],
                    "gpu_vram_total_mb": int(float(parts[1])),
                    "gpu_vram_used_mb": int(float(parts[2])),
                    "gpu_vram_free_mb": int(float(parts[3])),
                    "gpu_utilization_pct": float(parts[4]),
                    "gpu_temperature_c": float(parts[5]),
                    "gpu_power_w": float(parts[6]) if len(parts) > 6 else None,
                    "gpu_detected_by": source,
                }
        except (ValueError, IndexError) as exc:
            log.debug("nvidia-smi parse error: %s", exc)
        return {}

    def _collect_gpu_from_procfs(self) -> dict:
        """Detect an NVIDIA GPU when nvidia-smi is missing or unhealthy."""
        for info_path in glob.glob("/proc/driver/nvidia/gpus/*/information"):
            try:
                fields: dict[str, str] = {}
                with open(info_path, encoding="utf-8") as fh:
                    for line in fh:
                        if ":" not in line:
                            continue
                        key, value = line.split(":", 1)
                        fields[key.strip().lower()] = value.strip()
                model = fields.get("model")
                if model:
                    data = {
                        "gpu_name": model,
                        "gpu_detected_by": "procfs",
                    }
                    if fields.get("bus location"):
                        data["gpu_bus_id"] = fields["bus location"]
                    return data
            except OSError as exc:
                log.debug("NVIDIA procfs GPU probe failed for %s: %s", info_path, exc)
        for dev_path in ("/dev/nvidia0", "/dev/dxg"):
            if os.path.exists(dev_path):
                return {
                    "gpu_name": "NVIDIA GPU" if "nvidia" in dev_path else "GPU device",
                    "gpu_detected_by": dev_path,
                }
        return {}

    def _collect_ollama(self) -> dict:
        """Collect Ollama loaded models via /api/ps."""
        try:
            import httpx
            resp = httpx.get(f"{self._ollama_url}/api/ps", timeout=5.0)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            models = data.get("models", [])

            models_info = []
            total_vram = 0
            for m in models:
                size_vram = m.get("size_vram", 0)
                total_vram += size_vram
                models_info.append({
                    "model": m.get("name", ""),
                    "size_vram_mb": round(size_vram / (1024 * 1024)),
                    "size_mb": round(m.get("size", 0) / (1024 * 1024)),
                    "expires_at": m.get("expires_at"),
                })

            return {
                "ollama_models_loaded": len(models),
                "ollama_vram_used_mb": round(total_vram / (1024 * 1024)),
                "models_loaded_json": json.dumps(models_info) if models_info else None,
            }
        except Exception as exc:
            log.debug("Ollama /api/ps failed: %s", exc)
            return {}

    def record_snapshot(self) -> dict:
        """Collect a snapshot and store it in metrics.db."""
        from orchestrator.observability.store import get_store

        snapshot = self.snapshot()
        store = get_store()
        if store:
            store.insert_resource_snapshot(snapshot)
        return snapshot


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_collector: ResourceCollector | None = None


def get_resource_collector() -> ResourceCollector:
    """Get or create the ResourceCollector singleton."""
    global _collector
    if _collector is None:
        _collector = ResourceCollector()
    return _collector


def _reset_resource_collector() -> None:
    """Reset singleton — for testing."""
    global _collector
    _collector = None
