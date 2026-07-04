"""Safe archive restore."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from storage_guardian.compressors import get_compressor
from storage_guardian.config import StorageGuardianConfig
from storage_guardian.hashing import hash_file
from storage_guardian.index import StorageIndex
from storage_guardian.path_safety import safe_child_path, safe_existing_file_under_roots, safe_path_name, sanitized_path_name
from storage_guardian.safety import SafetyGate


class RestoreManager:
    def __init__(self, config: StorageGuardianConfig, index: StorageIndex) -> None:
        self.config = config
        self.index = index
        self.safety = SafetyGate(config)

    def restore(self, manifest_path: str | Path, restore_name: str | None = None) -> dict[str, Any]:
        manifest_file = safe_existing_file_under_roots(
            manifest_path,
            (self.config.data_root / "manifests",),
            field_name="manifest_path",
        )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        archive_id = manifest["archive_id"]
        archive_path = Path(manifest["archive_path"])
        backend = manifest["compression_backend"]
        target_name = (
            safe_path_name(restore_name, field_name="restore_name")
            if restore_name
            else sanitized_path_name(f"{archive_id}_{time.strftime('%Y%m%d_%H%M%S')}", fallback="restore")
        )
        target_dir = safe_child_path(self.config.restore_root, target_name, field_name="restore_name")
        self.safety.validate_restore_target(target_dir)
        target_dir.mkdir(parents=True, exist_ok=False)

        compressor = get_compressor(backend)
        extracted = compressor.extract(archive_path, target_dir)
        verification = self._verify_restored_files(manifest, target_dir)
        restore_id = f"restore_{archive_id}_{time.time_ns()}"
        self.index.insert_restore_event(restore_id, archive_id, target_dir, verification["verified"])
        self.index.commit()
        return {
            "restore_id": restore_id,
            "archive_id": archive_id,
            "restore_root": str(target_dir),
            "files_count": len(extracted),
            **verification,
        }

    @staticmethod
    def _verify_restored_files(manifest: dict[str, Any], target_dir: Path) -> dict[str, Any]:
        mismatches: list[str] = []
        checked = 0
        for item in manifest.get("files", []):
            expected_hash = item.get("content_hash")
            if not expected_hash:
                continue
            checked += 1
            restored = safe_child_path(target_dir, str(item["relative_path"]), field_name="relative_path")
            if not restored.exists() or hash_file(restored) != expected_hash:
                mismatches.append(item["relative_path"])
        return {"verified": not mismatches, "checked_hashes": checked, "hash_mismatches": mismatches}
