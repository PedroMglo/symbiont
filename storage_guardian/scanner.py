"""Registered store scanner."""

from __future__ import annotations

import time
from pathlib import Path

from storage_guardian.classifier import classify_path
from storage_guardian.config import StorageGuardianConfig
from storage_guardian.hashing import hash_file
from storage_guardian.lifecycle_math import lifecycle_state_for_age
from storage_guardian.types import FileRecord, StoreConfig


class StoreScanner:
    def __init__(self, config: StorageGuardianConfig) -> None:
        self.config = config

    def scan(self) -> tuple[FileRecord, ...]:
        records: list[FileRecord] = []
        for store in self.config.stores:
            if not store.enabled:
                continue
            records.extend(self.scan_store(store))
        return tuple(records)

    def scan_store(self, store: StoreConfig) -> tuple[FileRecord, ...]:
        if not store.path.exists():
            return ()
        policy = self.config.policy_for(store)
        records: list[FileRecord] = []
        for path in store.path.rglob("*"):
            if not path.is_file():
                continue
            if _is_internal_skip(path):
                continue
            stat = path.stat()
            age_days = _effective_age_days(stat.st_atime, stat.st_mtime)
            detected_type, input_kind = classify_path(path, policy)
            relative_path = path.relative_to(store.path).as_posix()
            content_hash = hash_file(path) if self.config.root.get("manifests", {}).get("include_hashes", True) else None
            file_id = _file_id(store.name, relative_path, content_hash, stat.st_size, stat.st_mtime)
            records.append(
                FileRecord(
                    file_id=file_id,
                    store=store,
                    absolute_path=path,
                    relative_path=relative_path,
                    extension=path.suffix.lower(),
                    size_bytes=stat.st_size,
                    modified_at=stat.st_mtime,
                    accessed_at=stat.st_atime,
                    created_at=stat.st_ctime,
                    effective_age_days=age_days,
                    detected_type=detected_type,
                    input_kind=input_kind,
                    lifecycle_state=lifecycle_state_for_age(age_days, self.config.hot_until_days, self.config.cold_after_days),
                    content_hash=content_hash,
                )
            )
        return tuple(records)


def _effective_age_days(accessed_at: float, modified_at: float) -> float:
    reference = max(accessed_at, modified_at)
    return max(0.0, (time.time() - reference) / 86400)


def _file_id(store_name: str, relative_path: str, content_hash: str | None, size: int, mtime: float) -> str:
    import hashlib

    payload = f"{store_name}\0{relative_path}\0{content_hash or ''}\0{size}\0{mtime}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_internal_skip(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" in parts or ".git" in parts or ".pytest_cache" in parts
