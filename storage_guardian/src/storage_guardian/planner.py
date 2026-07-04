"""Cycle planner."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from typing import Any

from storage_guardian.classifier import backend_for
from storage_guardian.config import StorageGuardianConfig
from storage_guardian.lifecycle_math import compression_aggression
from storage_guardian.placement import PlacementSelector
from storage_guardian.safety import SafetyGate
from storage_guardian.types import ArchivePlan, CyclePlan, FileRecord, SkipDecision


class LifecyclePlanner:
    def __init__(self, config: StorageGuardianConfig) -> None:
        self.config = config
        self.safety = SafetyGate(config)
        self.placement = PlacementSelector(config)

    def plan(self, files: tuple[FileRecord, ...]) -> CyclePlan:
        cycle_id = f"cycle_{_timestamp_slug(compact=True)}"
        grouped: dict[tuple[str, str, str], list[FileRecord]] = defaultdict(list)
        skipped: list[SkipDecision] = []
        events: list[dict[str, Any]] = []

        seen_hashes: set[str] = set()
        for record in files:
            allowed, reason = self.safety.evaluate_file(record)
            if record.content_hash and record.content_hash in seen_hashes:
                skipped.append(SkipDecision(record, "duplicate_content_already_seen", "duplicate_logical_reference"))
                continue
            if record.content_hash:
                seen_hashes.add(record.content_hash)
            if not allowed:
                skipped.append(SkipDecision(record, reason, _state_for_reason(reason)))
                continue
            if record.lifecycle_state == "hot":
                skipped.append(SkipDecision(record, "hot_file", "hot"))
                continue
            tier = "cold" if record.lifecycle_state == "cold_candidate" else "warm"
            policy = self.config.policy_for(record.store)
            backend = backend_for(policy, tier, record.detected_type, record.extension)
            grouped[(record.store.name, tier, backend)].append(record)

        archive_plans: list[ArchivePlan] = []
        for (_store_name, tier, backend), group in grouped.items():
            store = group[0].store
            original_size = sum(item.size_bytes for item in group)
            estimated_size = _estimate_archive_size(original_size, tier, backend)
            target = self.placement.select(store, estimated_size)
            archive_id = _archive_id(store.name, tier, backend, group)
            policy = self.config.policy_for(store)
            policy_snapshot = dict(policy.values)
            policy_snapshot["compression_aggression"] = compression_aggression(
                max(item.effective_age_days for item in group),
                self.config.hot_until_days,
                self.config.cold_after_days,
            )
            archive_plans.append(
                ArchivePlan(
                    archive_id=archive_id,
                    store=store,
                    tier=tier,  # type: ignore[arg-type]
                    backend=backend,
                    target=target,
                    files=tuple(group),
                    original_size_bytes=original_size,
                    estimated_archive_size_bytes=estimated_size,
                    policy_snapshot=policy_snapshot,
                )
            )
            events.append(
                {
                    "event_type": "archive_plan_created",
                    "store": store.name,
                    "tier": tier,
                    "backend": backend,
                    "files_count": len(group),
                    "storage_target": target.kind,
                }
            )

        return CyclePlan(cycle_id=cycle_id, files=files, archive_plans=tuple(archive_plans), skipped=tuple(skipped), events=tuple(events))


def _archive_id(store_name: str, tier: str, backend: str, files: list[FileRecord]) -> str:
    payload = "\n".join(f"{item.relative_path}:{item.content_hash}:{item.size_bytes}" for item in files).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    stamp = _timestamp_slug()
    return f"{_safe(store_name)}_{tier}_{_safe(backend)}_{stamp}_{digest}"


def _timestamp_slug(*, compact: bool = False) -> str:
    fmt = "%Y%m%d_%H%M%S" if compact else "%Y_%m_%d_%H%M%S"
    return f"{time.strftime(fmt)}_{time.time_ns()}"


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)


def _estimate_archive_size(original_size: int, tier: str, backend: str) -> int:
    if backend == "passthrough":
        return original_size
    factor = 0.65 if tier == "warm" else 0.45
    if backend == "sevenzip":
        factor -= 0.08
    return max(1, int(original_size * factor))


def _state_for_reason(reason: str) -> str:
    if reason == "catalog_only":
        return "catalog_only"
    if reason == "blocked_live_storage":
        return "blocked_live_storage"
    if reason == "blocked_unregistered_path":
        return "blocked_unregistered_path"
    return "blocked_safety_policy"
