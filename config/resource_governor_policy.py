"""Effective Resource Governor policy payload generation.

`config/` owns the generated policy payload because it is derived from central
machine/runtime configuration. Runtime Resource Governor services may validate
and serve this payload, but they should not be imported by the resolver.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml


def build_effective_policy_payload(
    config: dict[str, Any] | None = None,
    *,
    resolved_config: dict[str, Any] | None = None,
    policy_path: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the effective Resource Governor policy as plain JSON data."""

    if config is None and resolved_config is None and policy_path is None:
        snapshot = _load_policy_snapshot(snapshot_path or _default_snapshot_path())
        if snapshot:
            return snapshot

    policy_root = _policy_root(config=config, policy_path=policy_path)
    runtime = _mapping((resolved_config or {}).get("runtime"))
    storage_paths = _mapping((resolved_config or {}).get("storage_paths"))
    decisions = list((resolved_config or {}).get("decisions") or [])
    workers = int(_decision_value(decisions, "runtime.workers.final", 1))
    background_workers = int(_decision_value(decisions, "runtime.workers.background_cpu_io", workers))
    batch = int(_decision_value(decisions, "runtime.batch_size", 1))
    limits = dict(_mapping(policy_root.get("limits")))
    limits.setdefault("max_workers", workers)
    limits.setdefault("background_workers", max(1, background_workers))
    limits.setdefault("storage_workers", 1)
    limits.setdefault("embedding_batch", min(8, max(1, batch)))
    limits.setdefault("heavy_gpu_concurrency", 1 if runtime.get("gpu_available") else 0)

    return {
        "contract_version": str(policy_root.get("contract_version") or "resource-governor.v1"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "smart_resolver" if resolved_config else "config.resource_governor",
        "mode": str(policy_root.get("mode") or "observe_only"),
        "machine_profile": str(policy_root.get("machine_profile_value") or _machine_profile(runtime)),
        "thin_but_capable": False,
        "foreground_first": bool(_mapping(policy_root.get("experience_policy")).get("foreground_first", True)),
        "preserve_quality": bool(_mapping(policy_root.get("experience_policy")).get("preserve_quality", True)),
        "allow_deferred_quality": bool(_mapping(policy_root.get("experience_policy")).get("allow_deferred_quality", True)),
        "allow_silent_quality_loss": bool(_mapping(policy_root.get("experience_policy")).get("allow_silent_quality_loss", False)),
        "limits": limits,
        "lanes": _lanes(limits),
        "thresholds": dict(_mapping(policy_root.get("thresholds"))),
        "gpu_conflict_matrix": dict(_mapping(policy_root.get("gpu_conflict_matrix"))),
        "storage_policy": _storage_policy(storage_paths),
        "runtime_layers": _runtime_layers(runtime=runtime, limits=limits),
        "experience_slo": dict(_mapping(policy_root.get("experience_slo"))),
        "fallback_policy": dict(_mapping(policy_root.get("fallback_policy"))),
        "service_lease_requirements": dict(_mapping(policy_root.get("service_lease_requirements"))),
        "operational_authority": dict(_mapping(policy_root.get("operational_authority")) or {"foreground_first": True}),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _policy_root(*, config: dict[str, Any] | None, policy_path: str | Path | None) -> dict[str, Any]:
    direct = dict(config or {})
    if direct.get("resource_governor"):
        direct = _mapping(direct.get("resource_governor"))
    loaded = _load_policy_path(policy_path) if policy_path else {}
    if not loaded and not direct:
        loaded = _load_policy_path(_default_policy_path())
    merged = dict(loaded)
    merged.update(direct)
    return merged


def _load_policy_path(policy_path: str | Path | None) -> dict[str, Any]:
    if not policy_path:
        return {}
    path = Path(policy_path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    root = data.get("resource_governor") if isinstance(data, dict) else {}
    return dict(root) if isinstance(root, dict) else {}


def _load_policy_snapshot(snapshot_path: str | Path | None) -> dict[str, Any]:
    if not snapshot_path:
        return {}
    path = Path(snapshot_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _default_snapshot_path() -> Path | None:
    candidates = [
        os.environ.get("AI_RESOURCE_GOVERNOR_POLICY_SNAPSHOT_PATH"),
        str(Path(os.environ.get("AI_LOCAL_PROJECT_ROOT", "")) / ".local" / "generated" / "resource_governor_policy.json")
        if os.environ.get("AI_LOCAL_PROJECT_ROOT")
        else None,
        "/app/config/generated/resource_governor_policy.json",
        "/project/.local/generated/resource_governor_policy.json",
        "/workspace/ai-local/.local/generated/resource_governor_policy.json",
        str(Path.cwd() / ".local" / "generated" / "resource_governor_policy.json"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".local" / "generated" / "resource_governor_policy.json"
        if candidate.exists():
            return candidate
    return None


def _default_policy_path() -> Path | None:
    candidates = [
        os.environ.get("AI_RESOURCE_GOVERNOR_CONFIG_PATH"),
        str(Path(os.environ.get("AI_LOCAL_PROJECT_ROOT", "")) / "config" / "resource_governor.yaml")
        if os.environ.get("AI_LOCAL_PROJECT_ROOT")
        else None,
        "/project/config/resource_governor.yaml",
        "/workspace/ai-local/config/resource_governor.yaml",
        "/app/config/resource_governor.yaml",
        str(Path.cwd() / "config" / "resource_governor.yaml"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "resource_governor.yaml"
        if candidate.exists():
            return candidate
    return None


def _decision_value(decisions: list[Any], field: str, default: Any) -> Any:
    for item in decisions:
        if isinstance(item, dict) and item.get("field") == field:
            return item.get("value", default)
        if getattr(item, "field", None) == field:
            return getattr(item, "value", default)
    return default


def _machine_profile(runtime: dict[str, Any]) -> str:
    if not runtime.get("gpu_available"):
        ram_gb = float(runtime.get("ram_total_gb") or 0)
        return "low_ram_cpu" if ram_gb and ram_gb <= 16 else "balanced_desktop"
    vram_gb = float(runtime.get("vram_total_gb") or 0)
    if vram_gb <= 4:
        return "gpu_4gb"
    if vram_gb <= 6:
        return "gpu_6gb"
    if vram_gb <= 8:
        return "gpu_8gb"
    return "workstation" if vram_gb >= 16 else "balanced_desktop"


def _lanes(limits: dict[str, Any]) -> dict[str, Any]:
    return {
        "interactive": {"workers": 1, "protected": True},
        "interactive_enrichment": {"workers": 1, "preemptible": True},
        "background": {"workers": int(limits.get("background_workers") or 1), "preemptible": True},
        "storage": {"workers": int(limits.get("storage_workers") or 1), "preemptible": True},
        "heavy_gpu": {"concurrency": int(limits.get("heavy_gpu_concurrency") or 0), "preemptible": True},
    }


def _storage_policy(storage_paths: dict[str, Any]) -> dict[str, Any]:
    storage_mode = str(storage_paths.get("AI_LOCAL_STORAGE_MODE") or "unknown")
    external_root = str(storage_paths.get("AI_STORAGE_EXTERNAL_ROOT") or "")
    return {
        "effective_mode": storage_mode,
        "external_root": external_root,
        "external_configured": bool(external_root),
        "external_available": storage_mode == "external",
        "require_external": storage_paths.get("AI_STORAGE_REQUIRE_EXTERNAL") == "true",
        "local_fallback_enabled": storage_paths.get("AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK") == "true",
        "fallback_is_operational": storage_mode == "local_fallback",
        "missing_external_is_blocker": storage_mode == "external_missing",
    }


def _runtime_layers(*, runtime: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": {
            "cpu_threads": runtime.get("cpu_threads"),
            "ram_total_gb": runtime.get("ram_total_gb"),
            "ram_available_gb": runtime.get("ram_available_gb"),
            "gpu_available": runtime.get("gpu_available"),
            "gpu_name": runtime.get("gpu_name"),
            "vram_total_gb": runtime.get("vram_total_gb"),
        },
        "docker": {
            "available": runtime.get("docker_available"),
            "context": runtime.get("docker_context"),
            "gpu_available": runtime.get("gpu_available"),
        },
        "effective": {
            "cpu_workers": limits.get("max_workers"),
            "background_workers": limits.get("background_workers"),
            "storage_workers": limits.get("storage_workers"),
            "heavy_gpu_concurrency": limits.get("heavy_gpu_concurrency"),
            "embedding_batch": limits.get("embedding_batch"),
        },
    }
