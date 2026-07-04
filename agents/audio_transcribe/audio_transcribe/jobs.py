"""Job lifecycle management: create, cancel, delete, status tracking."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import JobNotFoundError, PathSecurityError
from audio_transcribe.storage import (
    create_job_directories,
    delete_job_outputs,
    directory_size,
    get_job_dir,
    load_job_record,
    save_job_record,
)
from audio_transcribe.types import (
    JobRecord,
    JobStage,
    JobStatus,
    TranscriptionOptions,
)
from audio_transcribe.storage_guardian_api import source_reuse_metadata

logger = logging.getLogger(__name__)

# In-memory job registry (survives within process lifetime)
_jobs: dict[str, JobRecord] = {}


def _log_value(value: object, limit: int = 160) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(
    input_path: Optional[str] = None,
    input_filename: Optional[str] = None,
    options: Optional[TranscriptionOptions] = None,
) -> JobRecord:
    """Create a new transcription job."""
    job_id = str(uuid.uuid4())
    options = options or TranscriptionOptions()
    reuse_metadata = source_reuse_metadata(input_path, options=options)
    record = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        input_path=input_path,
        input_filename=input_filename,
        source_content_hash=_string_or_none(reuse_metadata.get("source_content_hash")),
        transcription_options_hash=_string_or_none(reuse_metadata.get("transcription_options_hash")),
        options=options,
        created_at=_now_iso(),
    )
    _jobs[job_id] = record
    create_job_directories(job_id)
    save_job_record(record)
    logger.info("Created job %s", _log_value(job_id))
    return record


def get_job(job_id: str) -> JobRecord:
    """Get a job by ID. Raises JobNotFoundError if not found."""
    if job_id in _jobs:
        return _jobs[job_id]
    # Try loading from disk
    try:
        record = load_job_record(job_id)
    except PathSecurityError as exc:
        if exc.message != "Invalid output root":
            raise
        record = None
    if record:
        _jobs[job_id] = record
        return record
    raise JobNotFoundError(message=f"Job not found: {job_id}")


def update_job(
    job_id: str,
    status: Optional[JobStatus] = None,
    stage: Optional[JobStage] = None,
    progress: Optional[float] = None,
    processed_duration: Optional[float] = None,
    total_duration: Optional[float] = None,
    error: Optional[str] = None,
    warning: Optional[str] = None,
) -> JobRecord:
    """Update job state and persist."""
    record = get_job(job_id)
    if status:
        record.status = status
    if stage:
        record.stage = stage
    if progress is not None:
        record.progress = progress
    if processed_duration is not None:
        record.processed_duration_seconds = processed_duration
    if total_duration is not None:
        record.total_duration_seconds = total_duration
    if error:
        record.error = error
    if warning:
        record.warnings.append(warning)

    # Auto-set timestamps
    if status == JobStatus.RUNNING and not record.started_at:
        record.started_at = _now_iso()
    if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        record.completed_at = _now_iso()

    save_job_record(record)
    return record


def cancel_job(job_id: str) -> JobRecord:
    """Cancel a job if it's still queued or running."""
    record = get_job(job_id)
    if record.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return record  # Already terminal
    record.status = JobStatus.CANCELLED
    record.stage = JobStage.CANCELLED
    record.completed_at = _now_iso()
    save_job_record(record)
    logger.info("Cancelled job %s", _log_value(job_id))
    return record


def delete_job(job_id: str) -> None:
    """Delete job outputs and remove from registry."""
    delete_job_outputs(job_id)
    _jobs.pop(job_id, None)
    logger.info("Deleted job %s", _log_value(job_id))


def list_jobs(
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JobRecord]:
    """List jobs with optional filtering."""
    jobs = list(_jobs.values())
    if status_filter:
        jobs = [j for j in jobs if j.status.value == status_filter]
    # Sort by created_at descending
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs[offset : offset + limit]


def get_active_job_count() -> int:
    """Get count of non-terminal jobs."""
    return sum(
        1 for j in _jobs.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    )


def find_reusable_completed_job(
    input_path: str,
    *,
    options: TranscriptionOptions | None = None,
) -> JobRecord | None:
    """Return a completed compatible job for the same input path, if any."""

    return find_reusable_job(
        input_path,
        options=options,
        statuses={JobStatus.COMPLETED},
        require_outputs_for_completed=True,
    )


def find_reusable_active_job(
    input_path: str,
    *,
    options: TranscriptionOptions | None = None,
) -> JobRecord | None:
    """Return a queued/running compatible job for the same input path, if any."""

    return find_reusable_job(
        input_path,
        options=options,
        statuses={JobStatus.QUEUED, JobStatus.RUNNING},
        require_outputs_for_completed=False,
    )


def find_reusable_job(
    input_path: str,
    *,
    options: TranscriptionOptions | None = None,
    statuses: set[JobStatus] | None = None,
    require_outputs_for_completed: bool = True,
) -> JobRecord | None:
    """Return the newest compatible job for a path within the requested states."""

    normalized_input = _normalized_path(input_path)
    requested_metadata = source_reuse_metadata(input_path, options=options)
    requested_source_hash = _string_or_none(requested_metadata.get("source_content_hash"))
    candidates = sorted(_jobs.values(), key=lambda item: item.created_at, reverse=True)
    for record in candidates:
        if statuses is not None and record.status not in statuses:
            continue
        path_matches = bool(record.input_path and _normalized_path(record.input_path) == normalized_input)
        hash_matches = bool(
            requested_source_hash
            and _record_source_content_hash(record) == requested_source_hash
        )
        if not path_matches and not hash_matches:
            continue
        if options is not None and not _options_compatible(record.options, options):
            continue
        if record.status == JobStatus.COMPLETED and require_outputs_for_completed:
            outputs = record.to_result_response().outputs
            if not any(outputs.values()):
                continue
        if record.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            continue
        return record
    return None


def load_persisted_jobs() -> int:
    """Load persisted job records from disk on startup. Returns count loaded."""
    cfg = get_config()
    from pathlib import Path

    output_dir = Path(cfg.paths.output_dir)
    if not output_dir.exists():
        return 0
    count = 0
    for job_dir in output_dir.iterdir():
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        if job_id in _jobs:
            continue
        record = load_job_record(job_id)
        if record:
            _jobs[job_id] = record
            count += 1
    if count:
        logger.info(f"Loaded {count} persisted job(s) from disk")
    return count


def recover_interrupted_jobs() -> int:
    """Move persisted queued/running jobs back to queued after process restart."""
    recovered = 0
    for record in list(_jobs.values()):
        if record.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            continue
        if record.status == JobStatus.RUNNING:
            record.warnings.append("Job recovered after service restart")
        record.status = JobStatus.QUEUED
        record.stage = JobStage.QUEUED
        record.progress = min(record.progress or 0.0, 99.0)
        save_job_record(record)
        recovered += 1
    if recovered:
        logger.info("Recovered %d interrupted audio job(s)", recovered)
    return recovered


def cleanup_expired_jobs(*, dry_run: bool = True) -> dict[str, object]:
    """Delete terminal job outputs older than the configured TTL."""
    cfg = get_config()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.jobs.job_ttl_hours)
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    eligible: list[JobRecord] = []

    for record in list(_jobs.values()):
        if record.status not in terminal:
            continue
        completed_at = _parse_time(record.completed_at or record.created_at)
        if completed_at and completed_at <= cutoff:
            eligible.append(record)

    bytes_total = 0
    deleted_jobs: list[str] = []
    for record in eligible:
        job_dir = get_job_dir(record.job_id)
        size = directory_size(job_dir)
        bytes_total += size
        if dry_run:
            continue
        try:
            delete_job_outputs(record.job_id)
        except JobNotFoundError:
            pass
        _jobs.pop(record.job_id, None)
        deleted_jobs.append(record.job_id)

    return {
        "dry_run": dry_run,
        "ttl_hours": cfg.jobs.job_ttl_hours,
        "eligible_jobs": [record.job_id for record in eligible],
        "deleted_jobs": deleted_jobs,
        "bytes_reclaimable": bytes_total,
        "bytes_freed": 0 if dry_run else bytes_total,
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _normalized_path(value: str) -> str:
    from os import path as os_path

    return os_path.realpath(os_path.abspath(str(value or "").strip()))


def _record_source_content_hash(record: JobRecord) -> str | None:
    if record.source_content_hash:
        return record.source_content_hash
    metadata = source_reuse_metadata(record.input_path, options=record.options)
    return _string_or_none(metadata.get("source_content_hash"))


def _string_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _options_compatible(existing: TranscriptionOptions, requested: TranscriptionOptions) -> bool:
    if requested.language != "auto" and existing.language not in {requested.language, "auto"}:
        return False
    return existing.diarization == requested.diarization and existing.vad == requested.vad


def reset_jobs() -> None:
    """Reset job registry (for testing)."""
    _jobs.clear()
