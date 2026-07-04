"""Adapter contract for specialized stores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SnapshotRequest:
    source_path: Path
    destination_path: Path


class StoreAdapter:
    name = "base"

    def prepare_snapshot(self, request: SnapshotRequest) -> Path:
        raise NotImplementedError

