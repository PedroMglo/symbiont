"""Compressor interfaces and shared tar safety."""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from storage_guardian.types import FileRecord


@dataclass(frozen=True)
class CompressionResult:
    archive_path: Path
    members: tuple[str, ...]


class Compressor:
    backend = "base"
    extension = ".tar"

    def archive(self, files: tuple[FileRecord, ...], output_path: Path, level: int | None = None) -> CompressionResult:
        raise NotImplementedError

    def list_members(self, archive_path: Path) -> tuple[str, ...]:
        raise NotImplementedError

    def extract(self, archive_path: Path, target_dir: Path) -> tuple[Path, ...]:
        raise NotImplementedError

    def read_member_bytes(self, archive_path: Path, member_name: str, max_bytes: int) -> bytes:
        raise NotImplementedError


def add_files_to_tar(tar: tarfile.TarFile, files: tuple[FileRecord, ...]) -> tuple[str, ...]:
    members: list[str] = []
    for record in files:
        arcname = record.relative_path
        tar.add(record.absolute_path, arcname=arcname, recursive=False)
        members.append(arcname)
    return tuple(members)


def safe_extract_tar(tar: tarfile.TarFile, target_dir: Path) -> tuple[Path, ...]:
    extracted: list[Path] = []
    target_root = target_dir.resolve()
    for member in tar:
        member_path = (target_root / member.name).resolve()
        if not member_path.is_relative_to(target_root):
            raise ValueError(f"archive member escapes restore root: {member.name}")
        if member.isdir():
            member_path.mkdir(parents=True, exist_ok=True)
            continue
        member_path.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            continue
        with source, member_path.open("wb") as out:
            shutil.copyfileobj(source, out)
        extracted.append(member_path)
    return tuple(extracted)


def read_tar_member_bytes(tar: tarfile.TarFile, member_name: str, max_bytes: int) -> bytes:
    normalized = Path(member_name).as_posix()
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"unsafe archive member: {member_name}")
    for member in tar:
        if not member.isfile() or member.name != normalized:
            continue
        if member.size > max_bytes:
            raise ValueError(f"archive member exceeds max_bytes: {member.name}")
        source = tar.extractfile(member)
        if source is None:
            raise FileNotFoundError(member_name)
        with source:
            return source.read(max_bytes + 1)
    raise FileNotFoundError(member_name)
