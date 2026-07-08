"""Effective config generation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.lifecycle_math import archive_chunk_target, max_parallel_jobs
from storage_guardian.resource_guard import ResourceSnapshot, current_resource_snapshot
from storage_guardian.storage_schema import storage_schema_payload


def build_effective_config(config: StorageGuardianConfig, resources: ResourceSnapshot | None = None) -> dict[str, Any]:
    resources = resources or current_resource_snapshot(config.data_root)
    root = config.root
    resource_cfg = root.get("resources", {})
    cpu_budget = float(resource_cfg.get("cpu_budget_fraction_of_idle", 0.35))

    effective = {
        "enabled": config.enabled,
        "identity": root.get("identity", {}),
        "mode": root.get("mode", {}),
        "lifecycle": {
            **root.get("lifecycle", {}),
            "warm_after_days": config.hot_until_days,
            "warm_span_days": config.cold_after_days - config.hot_until_days,
        },
        "placement": root.get("placement", {}),
        "storage_schema": storage_schema_payload(root),
        "scheduler": root.get("scheduler", {}),
        "resources": {
            **resource_cfg,
            "cpu_cores": resources.cpu_cores,
            "available_memory_bytes": resources.available_memory_bytes,
            "disk_free_bytes": resources.disk_free_bytes,
            "disk_free_ratio": resources.disk_free_ratio,
            "max_parallel_jobs": max_parallel_jobs(resources.cpu_cores, cpu_budget),
            "archive_chunk_target_bytes": archive_chunk_target(resources.available_memory_bytes),
        },
        "stores": [store.__dict__ | {"path": str(store.path)} for store in config.stores],
        "policies": {name: policy.values for name, policy in config.policies.items()},
        "safety": root.get("safety", {}),
        "index": root.get("index", {}),
        "manifests": root.get("manifests", {}),
        "api": root.get("api", {}),
        "observability": root.get("observability", {}),
    }
    effective["effective_config_hash"] = effective_config_hash(effective)
    return effective


def effective_config_hash(effective: dict[str, Any]) -> str:
    payload = dict(effective)
    payload.pop("effective_config_hash", None)
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
