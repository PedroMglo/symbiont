"""Storage management: job directories, checkpoint I/O, output organization."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import JobNotFoundError, PathSecurityError
from audio_transcribe.scratch import ScratchPathError, assert_scratch_path
from audio_transcribe.types import JobRecord, SegmentCheckpoint

logger = logging.getLogger(__name__)

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _safe_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not _SAFE_JOB_ID_RE.fullmatch(value):
        raise PathSecurityError(
            message="Invalid job identifier",
            detail="Job identifiers must be short alphanumeric path components",
        )
    return value


def _output_root() -> Path:
    cfg = get_config()
    try:
        return assert_scratch_path(cfg.paths.output_dir, label="audio output root")
    except ScratchPathError as exc:
        raise PathSecurityError(message="Invalid output root", detail=str(exc)) from exc


def _safe_relative_path(value: str, label: str) -> Path:
    raw = (value or "").strip()
    if not raw or "\x00" in raw:
        raise PathSecurityError(message=f"Invalid {label}", detail=f"{label} must be a relative path")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathSecurityError(message=f"Invalid {label}", detail=f"{label} must stay within the job directory")
    return relative


def _safe_child_path(root: Path, *parts: str) -> Path:
    relative_parts = [_safe_relative_path(part, "path component") for part in parts]
    target = root.joinpath(*relative_parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise PathSecurityError(
            message="Path escaped output directory",
            detail="Resolved output path must stay within the job directory",
        )
    return target


def get_job_dir(job_id: str) -> Path:
    """Get the output directory for a job."""
    root = _output_root()
    return _safe_child_path(root, _safe_job_id(job_id))


def create_job_directories(job_id: str) -> Path:
    """Create the full directory structure for a job."""
    job_dir = get_job_dir(job_id)
    subdirs = [
        "input",
        "processed_audio",
        "segments",
        "checkpoints",
        "transcripts",
        "subtitles",
        "rag_ready",
        "metadata",
        "logs",
    ]
    for subdir in subdirs:
        (job_dir / subdir).mkdir(parents=True, exist_ok=True)
    return job_dir


def save_job_record(record: JobRecord) -> None:
    """Persist job record to job.json."""
    job_dir = get_job_dir(record.job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = job_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    job_file = metadata_dir / "job.json"
    job_file.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def load_job_record(job_id: str) -> Optional[JobRecord]:
    """Load job record from disk."""
    job_file = get_job_dir(job_id) / "metadata" / "job.json"
    if not job_file.exists():
        return None
    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        return JobRecord.model_validate(data)
    except Exception as e:
        logger.warning("Failed to load job record: %s", type(e).__name__)
        return None


def save_checkpoint(job_id: str, checkpoint: SegmentCheckpoint) -> None:
    """Save a segment checkpoint."""
    checkpoint_dir = get_job_dir(job_id) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_dir / f"segment_{checkpoint.index:04d}.json"
    checkpoint_file.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")


def load_checkpoints(job_id: str) -> list[SegmentCheckpoint]:
    """Load all checkpoints for a job, sorted by index."""
    checkpoint_dir = get_job_dir(job_id) / "checkpoints"
    if not checkpoint_dir.exists():
        return []
    checkpoints = []
    for f in sorted(checkpoint_dir.glob("segment_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            checkpoints.append(SegmentCheckpoint.model_validate(data))
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", type(e).__name__)
    return checkpoints


def delete_job_outputs(job_id: str) -> None:
    """Delete all outputs for a job. Only deletes within output_dir."""
    job_dir = get_job_dir(job_id)

    # Security: ensure job_dir is within output_dir
    output_root = _output_root()
    resolved_job_dir = job_dir.resolve()
    if not resolved_job_dir.is_relative_to(output_root):
        raise PathSecurityError(
            message="Attempted to delete outside output directory",
            detail="Job directory must be within the configured output directory",
        )

    if not job_dir.exists():
        raise JobNotFoundError(message=f"Job {_safe_job_id(job_id)} outputs not found")

    shutil.rmtree(job_dir)
    logger.info("Deleted job outputs")


def directory_size(path: Path) -> int:
    """Return recursive file size for an existing directory."""
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def get_job_log_path(job_id: str) -> Path:
    """Get the processing log path for a job."""
    return get_job_dir(job_id) / "logs" / "processing.log"


def write_json_output(job_id: str, subdir: str, filename: str, data: dict | list) -> str:
    """Write JSON data to a job subdirectory. Returns the output path."""
    output_path = _safe_child_path(get_job_dir(job_id), subdir, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(output_path)


def write_text_output(job_id: str, subdir: str, filename: str, content: str) -> str:
    """Write text content to a job subdirectory. Returns the output path."""
    output_path = _safe_child_path(get_job_dir(job_id), subdir, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return str(output_path)


def copy_input_to_job(source_path: Path, job_id: str) -> Path:
    """Copy input file to job's input directory."""
    job_input_dir = get_job_dir(job_id) / "input"
    job_input_dir.mkdir(parents=True, exist_ok=True)
    dest = _safe_child_path(job_input_dir, source_path.name)
    shutil.copy2(source_path, dest)
    return dest
