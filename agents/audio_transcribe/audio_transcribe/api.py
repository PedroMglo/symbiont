"""FastAPI application for audio_transcribe service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import JSONResponse

from audio_transcribe import __version__
from audio_transcribe.config import get_config
from audio_transcribe.errors import (
    AudioTranscribeError,
    JobNotFoundError,
    PathSecurityError,
    UnsupportedMediaError,
)
from audio_transcribe.gpu import get_gpu_info
from audio_transcribe.jobs import (
    cancel_job,
    cleanup_expired_jobs,
    create_job,
    delete_job,
    find_reusable_active_job,
    find_reusable_completed_job,
    get_active_job_count,
    get_job,
    list_jobs,
    load_persisted_jobs,
    recover_interrupted_jobs,
)
from audio_transcribe.queue import get_queue
from audio_transcribe.query_workflow import execute_audio_query
from audio_transcribe.resource_governor import is_resource_governor_configured
from audio_transcribe.scratch import assert_model_cache_path, assert_scratch_path
from audio_transcribe.security import (
    sanitize_filename,
    validate_extension,
    validate_input_path,
    validate_upload_size,
    verify_api_key,
)
from audio_transcribe.types import (
    AudioQueryRequest,
    AudioQueryResponse,
    JobListItem,
    JobStatus,
    ModelInfo,
    TranscriptionJobRequest,
    TranscriptionJobResponse,
)

logger = logging.getLogger(__name__)


async def _queue_stats(queue: Any | None = None) -> dict[str, Any]:
    """Return queue stats for health and metrics without coupling to a backend."""
    queue = queue or get_queue()
    if hasattr(queue, "stats_async"):
        return await queue.stats_async()
    if hasattr(queue, "stats"):
        return queue.stats()
    return {
        "backend": queue.__class__.__name__,
        "pending": getattr(queue, "pending_count", 0),
        "running": getattr(queue, "is_running", False),
    }


def _resource_governor_status() -> dict[str, str]:
    state = "configured" if is_resource_governor_configured() else "not_configured"
    return {"state": state}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    cfg = get_config()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, cfg.observability.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Ensure scratch directories exist. Persistent publication is delegated to storage_guardian.
    for dir_path in [cfg.paths.input_dir, cfg.paths.output_dir, cfg.paths.tmp_dir]:
        assert_scratch_path(dir_path, label="audio writable root")
        Path(dir_path).mkdir(parents=True, exist_ok=True)
    for dir_path in {cfg.paths.models_dir, cfg.transcription.download_root}:
        assert_model_cache_path(dir_path, label="audio model cache root")
        Path(dir_path).mkdir(parents=True, exist_ok=True)

    # Load persisted jobs and make interrupted work explicit after restarts.
    loaded_jobs = load_persisted_jobs()
    recovered_jobs = recover_interrupted_jobs()

    # Start job queue
    queue = get_queue()
    redis_recovery: dict[str, int] = {"recovered": 0, "dead_lettered": 0}
    if hasattr(queue, "recover_stale_processing"):
        redis_recovery = await queue.recover_stale_processing()
    from audio_transcribe.pipeline import process_job

    queue.set_processor(process_job)
    await queue.start(num_workers=cfg.jobs.max_concurrent_jobs)
    app.state.audio_recovery = {
        "persisted_jobs_loaded": loaded_jobs,
        "jobs_requeued": recovered_jobs,
        "redis_processing_recovered": redis_recovery.get("recovered", 0),
        "redis_processing_dead_lettered": redis_recovery.get("dead_lettered", 0),
    }

    # Detect GPU on startup
    gpu_info = get_gpu_info()
    logger.info(
        f"Audio Transcribe v{__version__} started | "
        f"GPU: {gpu_info.device_name or 'None'} | "
        f"Device: {'cuda' if gpu_info.available else 'cpu'}"
    )

    yield

    # Shutdown
    await queue.stop()
    logger.info("Audio Transcribe shutting down")


app = FastAPI(
    title="Audio Transcribe",
    version=__version__,
    description="Local async audio/video transcription service",
    lifespan=lifespan,
)


# =============================================================================
# Error handlers
# =============================================================================


@app.exception_handler(AudioTranscribeError)
async def audio_transcribe_error_handler(request, exc: AudioTranscribeError):
    if isinstance(exc, JobNotFoundError):
        return JSONResponse(status_code=404, content={"error": exc.message})
    if isinstance(exc, PathSecurityError):
        return JSONResponse(status_code=403, content={"error": exc.message})
    if isinstance(exc, UnsupportedMediaError):
        return JSONResponse(status_code=415, content={"error": exc.message})
    return JSONResponse(status_code=500, content={"error": exc.message})


# =============================================================================
# Endpoints
# =============================================================================


@app.get("/health")
async def health():
    """Health check endpoint."""
    cfg = get_config()
    gpu_info = get_gpu_info(refresh=True)
    queue_stats = await _queue_stats()
    gpu_healthy = bool(
        gpu_info.available
        and (
            gpu_info.vram_total_mb == 0
            or gpu_info.vram_free_mb >= cfg.gpu_policy.min_free_vram_mb
        )
    )
    try:
        from audio_transcribe.transcription import get_transcriber

        transcriber = get_transcriber()
        model_loaded = transcriber.model_loaded
        current_model = transcriber.current_model or None
        current_device = transcriber.current_device or None
        current_compute_type = transcriber.current_compute_type or None
    except Exception:
        model_loaded = False
        current_model = None
        current_device = None
        current_compute_type = None

    status = "ok"
    if queue_stats.get("backend") == "redis" and queue_stats.get("connected") is False:
        status = "degraded"

    return {
        "status": status,
        "service": "audio_transcribe",
        "gpu_available": gpu_info.available,
        "gpu_healthy": gpu_healthy,
        "device": "cuda" if gpu_info.available else "cpu",
        "cuda_device_name": gpu_info.device_name or None,
        "vram_total_mb": gpu_info.vram_total_mb,
        "vram_used_mb": gpu_info.vram_used_mb,
        "vram_free_mb": gpu_info.vram_free_mb,
        "min_free_vram_mb": cfg.gpu_policy.min_free_vram_mb,
        "model_loaded": model_loaded,
        "current_model": current_model,
        "current_device": current_device,
        "current_compute_type": current_compute_type,
        "queue": queue_stats,
        "active_jobs": get_active_job_count(),
        "recovery": getattr(app.state, "audio_recovery", {}),
        "resource_governor": _resource_governor_status(),
        "version": __version__,
    }


@app.post("/transcriptions", status_code=202, dependencies=[Depends(verify_api_key)])
async def create_transcription(request: TranscriptionJobRequest):
    """Create a new transcription job. Returns immediately with job_id."""
    cfg = get_config()

    input_path: Optional[str] = None
    input_filename: Optional[str] = None

    if request.input_path:
        # Validate input path
        validated = validate_input_path(request.input_path)
        input_path = str(validated)
        input_filename = validated.name

    if not input_path:
        raise HTTPException(status_code=400, detail="input_path is required")

    reused = find_reusable_completed_job(input_path, options=request.options) or find_reusable_active_job(
        input_path,
        options=request.options,
    )
    if reused is not None:
        return TranscriptionJobResponse(
            job_id=reused.job_id,
            status=reused.status,
            created_at=reused.created_at,
            status_url=f"/transcriptions/{reused.job_id}",
        )

    # Check queue capacity
    if get_active_job_count() >= cfg.jobs.max_queued_jobs:
        raise HTTPException(status_code=429, detail="Job queue is full")

    # Create job
    record = create_job(
        input_path=input_path,
        input_filename=input_filename,
        options=request.options,
    )

    # Enqueue for processing
    queue = get_queue()
    await queue.enqueue(record.job_id)

    return TranscriptionJobResponse(
        job_id=record.job_id,
        status=record.status,
        created_at=record.created_at,
        status_url=f"/transcriptions/{record.job_id}",
    )


@app.post("/v1/transcribe", response_model=AudioQueryResponse, dependencies=[Depends(verify_api_key)])
async def run_audio_query(request: AudioQueryRequest):
    """Canonical dispatch endpoint for natural-language transcription requests."""
    return await execute_audio_query(request)


@app.post("/transcriptions/upload", status_code=202, dependencies=[Depends(verify_api_key)])
async def create_transcription_upload(
    file: UploadFile = File(...),
    model: str = Query(default="distil-large-v3"),
    language: str = Query(default="auto"),
    diarization: bool = Query(default=False),
    vad: bool = Query(default=True),
    rag_ready: bool = Query(default=True),
):
    """Create a transcription job via file upload."""
    cfg = get_config()

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension
    validate_extension(file.filename)

    # Validate size
    if file.size:
        validate_upload_size(file.size)

    # Save uploaded file with streaming quota enforcement.
    safe_name = sanitize_filename(file.filename)
    upload_path = Path(cfg.paths.input_dir) / safe_name
    assert_scratch_path(upload_path, label="audio upload")

    total_bytes = 0
    try:
        with upload_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                validate_upload_size(total_bytes)
                out.write(chunk)
    except Exception:
        try:
            upload_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    from audio_transcribe.types import TranscriptionOptions

    options = TranscriptionOptions(
        model=model,
        language=language,
        diarization=diarization,
        vad=vad,
        rag_ready=rag_ready,
    )

    reused = find_reusable_completed_job(str(upload_path), options=options) or find_reusable_active_job(
        str(upload_path),
        options=options,
    )
    if reused is not None:
        return TranscriptionJobResponse(
            job_id=reused.job_id,
            status=reused.status,
            created_at=reused.created_at,
            status_url=f"/transcriptions/{reused.job_id}",
        )

    if get_active_job_count() >= cfg.jobs.max_queued_jobs:
        raise HTTPException(status_code=429, detail="Job queue is full")

    record = create_job(
        input_path=str(upload_path),
        input_filename=safe_name,
        options=options,
    )

    queue = get_queue()
    await queue.enqueue(record.job_id)

    return TranscriptionJobResponse(
        job_id=record.job_id,
        status=record.status,
        created_at=record.created_at,
        status_url=f"/transcriptions/{record.job_id}",
    )


@app.get("/transcriptions/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_transcription_status(job_id: str):
    """Get transcription job status."""
    record = get_job(job_id)
    return record.to_status_response()


@app.get("/transcriptions/{job_id}/result", dependencies=[Depends(verify_api_key)])
async def get_transcription_result(job_id: str):
    """Get transcription job result (only available when completed)."""
    record = get_job(job_id)
    if record.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not completed. Current status: {record.status.value}",
        )
    return record.to_result_response()


@app.get("/transcriptions", dependencies=[Depends(verify_api_key)])
async def list_transcriptions(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List transcription jobs with optional filters."""
    jobs = list_jobs(status_filter=status, limit=limit, offset=offset)
    return [
        JobListItem(
            job_id=j.job_id,
            status=j.status,
            stage=j.stage,
            created_at=j.created_at,
            input_path=j.input_filename,
        )
        for j in jobs
    ]


@app.post("/transcriptions/{job_id}/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_transcription(job_id: str):
    """Cancel a transcription job."""
    record = cancel_job(job_id)
    return {"job_id": record.job_id, "status": record.status.value}


@app.delete("/transcriptions/{job_id}", dependencies=[Depends(verify_api_key)])
async def delete_transcription(job_id: str):
    """Delete transcription job outputs."""
    delete_job(job_id)
    return {"job_id": job_id, "deleted": True}


@app.post("/cleanup", dependencies=[Depends(verify_api_key)])
async def cleanup_jobs(dry_run: bool = Query(default=True)):
    """Clean up expired terminal job outputs."""
    return cleanup_expired_jobs(dry_run=dry_run)


@app.get("/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """List supported transcription models."""
    models = [
        ModelInfo(
            name="distil-large-v3",
            description="Distilled Whisper Large V3 — fast, accurate, low VRAM",
            size="~1.5GB",
            languages=["multilingual"],
            recommended_compute_types=["float16", "int8_float16"],
        ),
        ModelInfo(
            name="large-v3",
            description="Whisper Large V3 — highest accuracy",
            size="~3GB",
            languages=["multilingual"],
            recommended_compute_types=["float16", "int8_float16"],
        ),
        ModelInfo(
            name="medium",
            description="Whisper Medium — balanced speed/accuracy",
            size="~1.5GB",
            languages=["multilingual"],
            recommended_compute_types=["float16", "int8"],
        ),
        ModelInfo(
            name="small",
            description="Whisper Small — fast, lower accuracy",
            size="~500MB",
            languages=["multilingual"],
            recommended_compute_types=["float16", "int8", "float32"],
        ),
    ]
    return {"models": [m.model_dump() for m in models]}


@app.get("/config", dependencies=[Depends(verify_api_key)])
async def get_sanitized_config():
    """Return sanitized configuration (no secrets)."""
    cfg = get_config()
    return {
        "server": {"host": cfg.server.host, "port": cfg.server.port},
        "paths": {
            "input_dir": cfg.paths.input_dir,
            "output_dir": cfg.paths.output_dir,
            "models_dir": cfg.paths.models_dir,
        },
        "jobs": {
            "max_concurrent_jobs": cfg.jobs.max_concurrent_jobs,
            "max_queued_jobs": cfg.jobs.max_queued_jobs,
        },
        "performance": {
            "max_concurrent_gpu_transcriptions": cfg.performance.max_concurrent_gpu_transcriptions,
            "batch_size": cfg.performance.batch_size,
            "segment_strategy": cfg.performance.segment_strategy,
        },
        "transcription": {
            "model": cfg.transcription.model,
            "device": cfg.transcription.device,
            "compute_type": cfg.transcription.compute_type,
        },
        "diarization": {"enabled": cfg.diarization.enabled, "provider": cfg.diarization.provider},
        "export": {"formats": cfg.export.formats, "rag_ready": cfg.export.rag_ready},
    }


@app.get("/metrics", dependencies=[Depends(verify_api_key)])
async def get_metrics():
    """Return service metrics."""
    from audio_transcribe.jobs import _jobs

    gpu_info = get_gpu_info()
    queue = get_queue()
    queue_stats = await _queue_stats(queue)

    total = len(_jobs)
    by_status = {}
    for j in _jobs.values():
        by_status[j.status.value] = by_status.get(j.status.value, 0) + 1

    return {
        "service": "audio_transcribe",
        "version": __version__,
        "gpu_available": gpu_info.available,
        "device": "cuda" if gpu_info.available else "cpu",
        "jobs_total": total,
        "jobs_by_status": by_status,
        "queue_pending": queue_stats.get("pending", getattr(queue, "pending_count", 0)),
        "queue": queue_stats,
        "active_jobs": get_active_job_count(),
        "recovery": getattr(app.state, "audio_recovery", {}),
        "upload_rejects": 0,
        "latency_ms": {
            "stt_partial": None,
            "stt_final": None,
        },
        "resource_governor": _resource_governor_status(),
    }
