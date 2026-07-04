"""Stable identifiers for Storage Guardian registries."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


def directory_id(service: str | None, store: str | None, relative_path: str) -> str:
    payload = f"{service or 'unknown'}:{store or ''}:{relative_path or '.'}"
    return "dir_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def parent_directory_id(service: str | None, store: str | None, relative: PurePosixPath) -> str | None:
    if str(relative) in {"", "."} or relative.parent == relative:
        return None
    parent = relative.parent.as_posix() or "."
    return directory_id(service, store, parent)


def path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
