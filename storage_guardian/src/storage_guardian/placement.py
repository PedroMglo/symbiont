"""Storage target selection."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.types import StorageTarget, StoreConfig


class PlacementSelector:
    def __init__(self, config: StorageGuardianConfig) -> None:
        self.config = config

    def select(self, store: StoreConfig, planned_archive_size: int) -> StorageTarget:
        placement = store.placement
        if placement == "inherit":
            placement = str(self.config.root.get("mode", {}).get("placement_profile", "external_ssd_preferred"))

        if placement == "local_only":
            return self._local(False, "store_local_only")
        if placement == "external_ssd_only":
            external = self._external(planned_archive_size)
            if external:
                return external
            raise RuntimeError("external_ssd_required_but_unavailable")
        if placement == "local_preferred":
            return self._local(False, "local_preferred")

        external = self._external(planned_archive_size)
        if external:
            return external
        selection = self.config.root.get("placement", {}).get("selection", {})
        if selection.get("allow_local_when_external_missing", True):
            return self._local(True, "external_ssd_unavailable_local_allowed")
        raise RuntimeError("external_ssd_unavailable_local_fallback_disabled")

    def _local(self, fallback_used: bool, reason: str) -> StorageTarget:
        local = self.config.root["placement"]["local"]
        return StorageTarget(
            kind="local",
            archive_root=Path(local["archive_root"]),
            data_root=Path(local["data_root"]),
            fallback_used=fallback_used,
            selection_reason=reason,
        )

    def _external(self, planned_archive_size: int) -> StorageTarget | None:
        external = self.config.root.get("placement", {}).get("external_ssd", {})
        if not external.get("enabled", False):
            return None
        mount_path = Path(external["mount_path"])
        archive_root = Path(external["archive_root"])
        if external.get("require_mount", False) and not mount_path.is_mount():
            return None
        if not mount_path.exists():
            return None
        if external.get("require_writable", True) and not os.access(mount_path, os.W_OK):
            return None
        usage_path = archive_root if archive_root.exists() else mount_path
        usage = shutil.disk_usage(usage_path)
        ratio = float(external.get("require_min_free_ratio_against_planned_archive", 1.35))
        if planned_archive_size > 0 and usage.free < planned_archive_size * ratio:
            return None
        return StorageTarget(
            kind="external_ssd",
            archive_root=archive_root,
            data_root=Path(external.get("data_mirror_root", archive_root.parent / "data")),
            fallback_used=False,
            selection_reason="external_ssd_available_and_has_required_free_space",
        )

