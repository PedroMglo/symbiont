"""Archive-only tar compressor for already compressed or opaque files."""

from __future__ import annotations

import tarfile
from pathlib import Path

from storage_guardian.compressors.base import CompressionResult, Compressor, add_files_to_tar, read_tar_member_bytes, safe_extract_tar
from storage_guardian.types import FileRecord


class PassthroughCompressor(Compressor):
    backend = "passthrough"
    extension = ".tar"

    def archive(self, files: tuple[FileRecord, ...], output_path: Path, level: int | None = None) -> CompressionResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, mode="w") as tar:
            members = add_files_to_tar(tar, files)
        return CompressionResult(archive_path=output_path, members=members)

    def list_members(self, archive_path: Path) -> tuple[str, ...]:
        with tarfile.open(archive_path, mode="r") as tar:
            return tuple(member.name for member in tar.getmembers() if member.isfile())

    def extract(self, archive_path: Path, target_dir: Path) -> tuple[Path, ...]:
        with tarfile.open(archive_path, mode="r") as tar:
            return safe_extract_tar(tar, target_dir)

    def read_member_bytes(self, archive_path: Path, member_name: str, max_bytes: int) -> bytes:
        with tarfile.open(archive_path, mode="r") as tar:
            return read_tar_member_bytes(tar, member_name, max_bytes)
