"""Read archived content for query-time access."""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
from typing import Any

from storage_guardian.compressors import get_compressor
from storage_guardian.path_safety import safe_existing_file_under_roots, safe_relative_path


class ArchiveReader:
    def __init__(self, *, allowed_manifest_roots: Iterable[Path] = (), max_text_bytes: int = 2 * 1024 * 1024) -> None:
        self.allowed_manifest_roots = tuple(allowed_manifest_roots)
        self.max_text_bytes = max_text_bytes

    def manifest(self, manifest_path: str | Path) -> dict[str, Any]:
        path = safe_existing_file_under_roots(
            manifest_path,
            self.allowed_manifest_roots,
            field_name="manifest_path",
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def list_members(self, manifest_path: str | Path) -> list[dict[str, Any]]:
        manifest = self.manifest(manifest_path)
        return [
            {
                "relative_path": item["relative_path"],
                "size_bytes": item["size_bytes"],
                "content_hash": item.get("content_hash"),
                "detected_type": item.get("detected_type"),
            }
            for item in manifest.get("files", [])
        ]

    def read_text_member(self, manifest_path: str | Path, relative_path: str, max_bytes: int | None = None) -> dict[str, Any]:
        manifest = self.manifest(manifest_path)
        safe_member = safe_relative_path(relative_path, field_name="relative_path").as_posix()
        member = self._find_member(manifest, safe_member)
        limit = max_bytes or self.max_text_bytes
        if int(member.get("size_bytes", 0)) > limit:
            raise ValueError(f"member is larger than max_bytes: {safe_member}")
        compressor = get_compressor(str(manifest["compression_backend"]))
        payload = compressor.read_member_bytes(Path(manifest["archive_path"]), safe_member, limit)
        return {
            "archive_id": manifest["archive_id"],
            "relative_path": safe_member,
            "size_bytes": len(payload),
            "content_hash": member.get("content_hash"),
            "text": payload.decode("utf-8", errors="replace"),
        }

    @staticmethod
    def _find_member(manifest: dict[str, Any], relative_path: str) -> dict[str, Any]:
        normalized = safe_relative_path(relative_path, field_name="relative_path").as_posix()
        for item in manifest.get("files", []):
            if item.get("relative_path") == normalized:
                return item
        raise FileNotFoundError(relative_path)
