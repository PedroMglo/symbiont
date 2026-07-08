"""Non-destructive safety gates."""

from __future__ import annotations

from pathlib import Path

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.types import FileRecord, StoreConfig


class SafetyGate:
    def __init__(self, config: StorageGuardianConfig) -> None:
        self.config = config

    def evaluate_file(self, record: FileRecord) -> tuple[bool, str]:
        if not self._inside_registered_store(record.absolute_path, record.store):
            return False, "blocked_unregistered_path"
        if record.store.mode == "catalog_only":
            return False, "catalog_only"
        if record.store.mode == "snapshot_only" and record.input_kind != "snapshot":
            return False, "blocked_live_storage"
        if record.input_kind == "live_database":
            return False, "blocked_live_storage"
        if record.input_kind == "model" and record.store.mode != "managed":
            return False, "catalog_only"
        if not self.config.root.get("safety", {}).get("allow_lossy_transforms", False) and record.metadata.get("requires_lossy"):
            return False, "blocked_safety_policy"
        return True, "allowed"

    def validate_restore_target(self, target: Path) -> None:
        restore_root = self.config.restore_root.resolve()
        resolved = target.resolve()
        if not resolved.is_relative_to(restore_root):
            raise ValueError(f"restore target must stay under {restore_root}")
        if self.config.root.get("safety", {}).get("never_restore_over_existing_path", True) and resolved.exists():
            raise FileExistsError(f"restore target already exists: {resolved}")

    @staticmethod
    def _inside_registered_store(path: Path, store: StoreConfig) -> bool:
        try:
            path.resolve().relative_to(store.path.resolve())
            return True
        except ValueError:
            return False
