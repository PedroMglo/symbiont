"""Backup do vector store — copia a directoria de dados com rotação."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from rag_config import settings

MAX_BACKUPS = 3


def backup_store(dest_dir: Path | None = None) -> Path:
    """Create a timestamped backup of the vector store data directory.

    Keeps only the last MAX_BACKUPS copies, removing older ones.
    Returns the path to the new backup.
    """
    store_dir = settings.paths.data_dir
    if not store_dir.exists():
        raise FileNotFoundError(f"Store directory not found: {store_dir}")

    if dest_dir is None:
        dest_dir = store_dir.parent / "backups"

    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = dest_dir / f"store_backup_{timestamp}"

    shutil.copytree(store_dir, backup_path)

    # Rotate: keep only the newest MAX_BACKUPS
    existing = sorted(dest_dir.glob("store_backup_*"), key=lambda p: p.name)
    while len(existing) > MAX_BACKUPS:
        oldest = existing.pop(0)
        shutil.rmtree(oldest)

    return backup_path
