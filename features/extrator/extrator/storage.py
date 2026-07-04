"""Filesystem layout helpers for extrator outputs."""

from __future__ import annotations

import shutil
from pathlib import Path

from extrator.config import get_config
from extrator.scratch import assert_scratch_path, assert_state_path
from extrator.security import sanitize_filename, validate_output_path


def _writable_roots() -> list[str | Path]:
    cfg = get_config()
    return [
        cfg.paths.data_dir,
        cfg.paths.uploads_dir,
        cfg.paths.bronze_dir,
        cfg.paths.silver_dir,
        cfg.paths.gold_dir,
        cfg.paths.conversions_dir,
        cfg.paths.cache_dir,
        cfg.paths.logs_dir,
        cfg.paths.tmp_dir,
    ]


def ensure_directories() -> None:
    for raw in _writable_roots():
        assert_scratch_path(raw, label="extrator writable root")
        Path(raw).mkdir(parents=True, exist_ok=True)
    manifest_root = Path(get_config().manifest.db_path).parent
    assert_state_path(manifest_root, label="extrator manifest root")
    manifest_root.mkdir(parents=True, exist_ok=True)


def upload_path(filename: str) -> Path:
    ensure_directories()
    return validate_output_path(Path(get_config().paths.uploads_dir) / sanitize_filename(filename), get_config().paths.uploads_dir)


def bronze_doc_dir(doc_id: str) -> Path:
    return Path(get_config().paths.bronze_dir) / doc_id.replace(":", "_")


def silver_doc_dir(doc_id: str) -> Path:
    return Path(get_config().paths.silver_dir) / doc_id.replace(":", "_")


def gold_doc_dir(doc_id: str) -> Path:
    return Path(get_config().paths.gold_dir) / doc_id.replace(":", "_")


def conversion_dir(job_id: str) -> Path:
    return Path(get_config().paths.conversions_dir) / job_id


def copy_or_reference_original(source: Path, doc_id: str) -> str:
    cfg = get_config()
    out_dir = bronze_doc_dir(doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    if cfg.security.preserve_originals:
        target = out_dir / source.name
        shutil.copy2(source, target)
        return str(target)
    ref = out_dir / "source_path.txt"
    ref.write_text(str(source), encoding="utf-8")
    return str(ref)
