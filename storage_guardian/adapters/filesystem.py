"""Generic filesystem adapter."""

from __future__ import annotations

from pathlib import Path

from storage_guardian.adapters.base import SnapshotRequest, StoreAdapter


class FilesystemAdapter(StoreAdapter):
    name = "filesystem"

    def prepare_snapshot(self, request: SnapshotRequest) -> Path:
        request.destination_path.parent.mkdir(parents=True, exist_ok=True)
        return request.source_path
