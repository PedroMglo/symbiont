"""Zstandard tar compressor."""

from __future__ import annotations

import tarfile
from pathlib import Path

from storage_guardian.compressors.base import CompressionResult, Compressor, add_files_to_tar, read_tar_member_bytes, safe_extract_tar
from storage_guardian.types import FileRecord


class ZstdCompressor(Compressor):
    backend = "zstd"
    extension = ".tar.zst"

    def archive(self, files: tuple[FileRecord, ...], output_path: Path, level: int | None = None) -> CompressionResult:
        import zstandard as zstd

        output_path.parent.mkdir(parents=True, exist_ok=True)
        compressor = zstd.ZstdCompressor(level=level or 6)
        with output_path.open("wb") as raw:
            with compressor.stream_writer(raw) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as tar:
                    members = add_files_to_tar(tar, files)
        return CompressionResult(archive_path=output_path, members=members)

    def list_members(self, archive_path: Path) -> tuple[str, ...]:
        import zstandard as zstd

        names: list[str] = []
        with archive_path.open("rb") as raw:
            with zstd.ZstdDecompressor().stream_reader(raw) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    for member in tar:
                        if member.isfile():
                            names.append(member.name)
        return tuple(names)

    def extract(self, archive_path: Path, target_dir: Path) -> tuple[Path, ...]:
        import zstandard as zstd

        with archive_path.open("rb") as raw:
            with zstd.ZstdDecompressor().stream_reader(raw) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    return safe_extract_tar(tar, target_dir)

    def read_member_bytes(self, archive_path: Path, member_name: str, max_bytes: int) -> bytes:
        import zstandard as zstd

        with archive_path.open("rb") as raw:
            with zstd.ZstdDecompressor().stream_reader(raw) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    return read_tar_member_bytes(tar, member_name, max_bytes)
