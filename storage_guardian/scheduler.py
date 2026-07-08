"""Small autonomous scheduler."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from storage_guardian.resource_guard import ResourceGuard, current_resource_snapshot
from storage_guardian.service import StorageGuardianService


class Scheduler:
    def __init__(self, service: StorageGuardianService) -> None:
        self.service = service
        self.last_cycle_started_at = 0.0
        self.last_sync_checked_at = 0.0

    def run_forever(self) -> None:
        cfg = self.service.config.root.get("scheduler", {})
        if cfg.get("run_on_startup", False):
            self.service.run_cycle()
            self.last_cycle_started_at = time.time()
        while True:
            self.tick()
            time.sleep(60)

    def tick(self) -> None:
        self._maybe_sync_pending()
        if self._should_run_now():
            self.service.run_cycle()
            self.last_cycle_started_at = time.time()

    def _maybe_sync_pending(self) -> None:
        cfg = self.service.config.root.get("local_fallback", {})
        if not cfg.get("sync_when_external_available", True):
            return
        interval = float(cfg.get("sync_check_interval_seconds", 60))
        now = time.time()
        if now - self.last_sync_checked_at < interval:
            return
        try:
            if self._should_pause_sync():
                return
            max_items = cfg.get("sync_max_items_per_check", 10)
            max_bytes = cfg.get("sync_max_bytes_per_check", 64 * 1024 * 1024)
            self.service.sync_pending(max_items=int(max_items), max_bytes=int(max_bytes))
        finally:
            self.last_sync_checked_at = now

    def _should_pause_sync(self) -> bool:
        resource_cfg = self.service.config.root.get("resources", {})
        data_root = Path(getattr(self.service.config, "data_root", "."))
        snapshot = current_resource_snapshot(data_root)
        pause, _reason = ResourceGuard(float(resource_cfg.get("disk_free_safety_ratio", 0.12))).should_pause(snapshot)
        return pause

    def _should_run_now(self) -> bool:
        cfg = self.service.config.root.get("scheduler", {})
        if not cfg.get("enabled", True):
            return False
        minimum = float(cfg.get("minimum_hours_between_cycles", 20)) * 3600
        if time.time() - self.last_cycle_started_at < minimum:
            return False
        active = cfg.get("active_window", {})
        start = str(active.get("start", "03:00"))
        end = str(active.get("end", "06:00"))
        now = datetime.now().strftime("%H:%M")
        return start <= now <= end
